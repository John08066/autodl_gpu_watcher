"""
AutoDL 平台采集器 — 使用 Playwright 控制浏览器与 AutoDL 控制台交互。

核心能力：
    1. 采集平台 GPU ID 空位 — 打开/刷新控制台页面，拦截 machine/list API
    2. 发送 API 请求 — 使用浏览器中捕获的 Authorization 令牌调用 power_on 等接口
    3. 采集 GPU 占用详情 — 点击"查看占用"按钮，解析弹窗表格

安全设计：
    - Authorization 令牌从浏览器请求头捕获后仅存内存，永不落盘
    - 登录会话保存在 user_data_dir 中，由 Playwright 管理

异常处理：
    - PlatformAuthenticationError：登录会话失效，需要重新 login
    - PlaywrightTimeoutError：页面加载/API 响应超时
"""
from __future__ import annotations

import os
import re
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from playwright.sync_api import BrowserContext, Page, Playwright, TimeoutError as PlaywrightTimeoutError, sync_playwright

from ..config import PlatformConfig
from ..login import prepare_profile_for_exclusive_use
from ..models import OccupancyRecord, PlatformHost


# 匹配 autodl-202-2 → 提取 202（物理主机号）
_MACHINE_PATTERN = re.compile(r"^autodl-(\d+)-\d+$", re.IGNORECASE)


class PlatformAuthenticationError(RuntimeError):
    """已得到明确认证失败证据：登录页、HTTP 401/403 等。"""
    pass


class PlatformTransientError(RuntimeError):
    """平台页面/API 暂时不可用，但没有证据表明登录已经失效。"""
    pass


class OccupancySnapshotMismatchError(RuntimeError):
    """占用弹窗与目标入口/平台空位互相矛盾，拒绝把该轮数据当成事实。"""
    pass


def canonical_host(machine_name: str) -> str:
    """功能：
        将 AutoDL 入口名映射到对应的物理主机名。

    参数：
        machine_name (str)：AutoDL 平台入口名，例如 `autodl-203-2`；可为 `None` 表示自动选择。

    返回：
        str：规范化物理主机名，例如 `gpu-203`。

    补充说明：
        示例：
            autodl-202-2 和 autodl-202-4 都对应物理主机 gpu-202。
            注意：最后的数字（-2、-4）是入口序号，不是 GPU INDEX。
    """
    match = _MACHINE_PATTERN.match(machine_name.strip())
    if match:
        return f"gpu-{match.group(1)}"
    return machine_name.strip()


def parse_platform_payload(payload: dict[str, Any], aggregation: str = "max") -> list[PlatformHost]:
    """功能：
        解析 AutoDL 控制台 machine/list API 的响应。

    参数：
        payload (dict[str, Any])：待解析的 JSON 字典，或发送给 AutoDL API 的请求体。
        aggregation (str)：同一物理主机多个入口的空位合并方式；实际配置通常为 `max`。

    返回：
        list[PlatformHost]：按物理主机聚合后的 PlatformHost 列表。

    补充说明：
        gpu.total 是该入口暴露的 GPU ID 总数，gpu.idle 是当前可分配的 GPU ID 数。
        同一物理主机的多个入口（如 autodl-202-2 和 autodl-202-4）按 canonical_host 归并，
        然后根据 aggregation 策略（max/min/sum）聚合。

        注意：
            - 多个入口的统计数据不能简单相加
            - platform 层面的 idle 只是入口门控，实际容量由 telemetry 显存数据决定
            - SSH/容器任务可能不被平台计入 idel/total，所以两层数据必须交叉验证
    """
    rows = payload.get("data", {}).get("list", [])
    grouped: dict[str, list[tuple[str, int, int]]] = defaultdict(list)

    for row in rows:
        gpu = row.get("gpu") or {}
        machine_name = str(row.get("machine_name", "")).strip()
        total = int(gpu.get("total", 0))
        idle = int(gpu.get("idle", 0))
        if not machine_name or total <= 0 or idle < 0:
            continue
        idle = min(idle, total)
        grouped[canonical_host(machine_name)].append((machine_name, idle, total))

    aggregation = aggregation.lower()
    if aggregation == "any":
        aggregation = "max"

    result: list[PlatformHost] = []
    for host, entries in sorted(grouped.items()):
        idle_values = [entry[1] for entry in entries]
        total_values = [entry[2] for entry in entries]
        if aggregation == "sum":
            free_count = sum(idle_values)
            total_count = sum(total_values)
        elif aggregation == "min":
            free_count = min(idle_values)
            total_count = max(total_values)
        else:
            free_count = max(idle_values)
            total_count = max(total_values)

        result.append(
            PlatformHost(
                host=host,
                free_count=min(free_count, total_count),
                total_count=total_count,
                source_names=tuple(entry[0] for entry in sorted(entries)),
                source_slots=tuple(sorted(entries)),
            )
        )
    return result


def parse_occupancy_cells(
    cells: list[str],
    *,
    observed_at: datetime,
    host: str,
    machine_name: str,
) -> OccupancyRecord | None:
    """功能：
        解析 AutoDL "查看占用"弹窗中的一行表格数据。

    参数：
        cells (list[str])：占用详情表中某一行按列提取出的文本列表。
        observed_at (datetime)：占用详情或 GPU 样本被采集的时间。
        host (str)：规范化物理主机名，例如 `gpu-203`。
        machine_name (str)：AutoDL 平台入口名，例如 `autodl-203-2`；可为 `None` 表示自动选择。

    返回：
        OccupancyRecord | None：解析成功的 OccupancyRecord；无效行返回 `None`。

    补充说明：
        预期的列顺序：[GPU INDEX, GPU UUID, GPU 名称, 是否被占用, 实例 ID, 用户, 启动时间]
        如果列数不足 7 或 GPU INDEX 无法解析，返回 None。
    """
    values = [str(item).strip() for item in cells]
    if len(values) < 7:
        return None
    try:
        gpu_index = int(values[0])
    except ValueError:
        return None
    occupied_text = values[3]
    if occupied_text not in {"是", "否"}:
        return None
    return OccupancyRecord(
        observed_at=observed_at,
        host=host,
        machine_name=machine_name,
        gpu_index=gpu_index,
        gpu_uuid=values[1],
        gpu_name=values[2],
        occupied=occupied_text == "是",
        instance_id="" if values[4] == "-" else values[4],
        user="" if values[5] == "-" else values[5],
        started_at_text="" if values[6] == "-" else values[6],
    )


def validate_occupancy_snapshot(
    records: list[OccupancyRecord],
    *,
    machine_name: str,
    expected_idle: int | None = None,
    expected_total: int | None = None,
) -> list[OccupancyRecord]:
    """校验一次占用弹窗是否真的属于目标入口且与 machine/list 一致。

    v0.5.4 的原则是“宁可判采集失败，也不能把另一个入口的数据贴过来”。
    machine/list 与占用弹窗在切换瞬间若短暂不一致，也按瞬时采集失败处理，
    下一轮重试，不允许借此触发本人下机或自动开机。
    """
    if not records:
        raise OccupancySnapshotMismatchError(
            f"{machine_name} 占用弹窗没有解析到 GPU 行"
        )
    if any(item.machine_name != machine_name for item in records):
        raise OccupancySnapshotMismatchError(
            f"{machine_name} 占用记录混入了其他入口"
        )

    indexes = [item.gpu_index for item in records]
    if len(indexes) != len(set(indexes)):
        raise OccupancySnapshotMismatchError(
            f"{machine_name} 占用弹窗出现重复 GPU INDEX：{indexes}"
        )

    if expected_total is not None:
        if expected_total <= 0:
            raise OccupancySnapshotMismatchError(
                f"{machine_name} 平台 total={expected_total} 无效"
            )
        if len(records) != expected_total:
            raise OccupancySnapshotMismatchError(
                f"{machine_name} 占用行数={len(records)}，但平台 total={expected_total}"
            )

    if expected_idle is not None and expected_total is not None:
        if not 0 <= expected_idle <= expected_total:
            raise OccupancySnapshotMismatchError(
                f"{machine_name} 平台 idle/total={expected_idle}/{expected_total} 无效"
            )
        actual_occupied = sum(1 for item in records if item.occupied)
        expected_occupied = expected_total - expected_idle
        if actual_occupied != expected_occupied:
            raise OccupancySnapshotMismatchError(
                f"{machine_name} 占用弹窗显示 {actual_occupied} 张被占用，"
                f"但平台 idle/total={expected_idle}/{expected_total}，"
                f"应为 {expected_occupied} 张；本轮拒绝采用"
            )

    return records


class PlatformBrowserCollector:
    """AutoDL 平台数据采集器 — 通过 Playwright 控制浏览器与 AutoDL 交互。

    生命周期：
        1. 首次调用 collect() 或 post_api_json() 时自动 start() 启动浏览器
        2. 主循环结束时由 main.py 调用 close() 关闭浏览器
        3. 登录会话保存在 user_data_dir，下次启动自动复用

    安全性：
        - Authorization 令牌从 API 响应头中捕获，仅存内存
        - 不做任何持久化（不写 config、log、磁盘文件）
    """

    def __init__(self, config: PlatformConfig) -> None:
        """功能：
            初始化 AutoDL 平台浏览器采集器，但暂不启动浏览器进程。

        参数：
            config (PlatformConfig)：当前模块对应的强类型配置对象。

        返回：
            None：函数通过副作用完成初始化、输出、持久化或资源管理。
        """
        self.config = config
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None   # 持久化浏览器上下文
        self._page: Page | None = None                # 当前页面
        self._authorization: str | None = None        # 内存中的 API 令牌
        self._machine_list_payload: dict[str, Any] | None = None

    def start(self) -> None:
        """功能：
            启动（或复用）Playwright 浏览器实例。

        参数：
            无。

        返回：
            None：函数通过副作用完成初始化、输出、持久化或资源管理。

        补充说明：
            使用 launch_persistent_context 以保持登录会话。
        """
        if self._context is not None:
            return
        self.config.user_data_dir.mkdir(parents=True, exist_ok=True)

        # v0.5.1：启动后台监控 Edge 前先清理上次异常退出遗留的 Edge/锁文件。
        # 这样登录结束后不需要手工执行菜单 7 才能重新启动监控。
        prepare_profile_for_exclusive_use(self.config.user_data_dir)

        # 首次启动失败时必须把 sync_playwright() 完整释放。
        # 旧版若 browser_profile 被 Edge 锁住，launch 失败后 _playwright 会残留，
        # 下一轮便持续报“Sync API inside the asyncio loop”。
        playwright = None
        context = None
        try:
            playwright = sync_playwright().start()
            launch_kwargs: dict[str, Any] = {
                "user_data_dir": str(self.config.user_data_dir),
                "channel": self.config.browser_channel,
                "headless": self.config.headless,
                "chromium_sandbox": True,
            }
            if self.config.autodl_direct and self.config.proxy_bypass_list.strip():
                # v0.5.4：AutoDL 控制面在该 PC 上经 Clash 系统代理明显变慢。
                # 监控专用 Edge 对 AutoDL 域名直连；其余流量仍可走本机 Clash。
                bypass_arg = self.config.proxy_bypass_list.strip()
                launch_kwargs["args"] = [f"--proxy-bypass-list={bypass_arg}"]

                proxy_server = (
                    os.environ.get("HTTPS_PROXY")
                    or os.environ.get("HTTP_PROXY")
                    or ""
                ).strip()
                if proxy_server:
                    # Playwright proxy.bypass 使用逗号分隔。给 BrowserContext 与
                    # 其关联 APIRequestContext 同一代理策略，避免浏览器直连、API 又绕韩国节点。
                    bypass = ",".join(
                        item
                        for item in bypass_arg.replace("<local>", "localhost;127.0.0.1").split(";")
                        if item
                    )
                    launch_kwargs["proxy"] = {
                        "server": proxy_server,
                        "bypass": bypass,
                    }
            context = playwright.chromium.launch_persistent_context(**launch_kwargs)
            self._playwright = playwright
            self._context = context
            self._page = context.pages[0] if context.pages else context.new_page()
            # v0.5.2：请求一发出就捕获 Authorization 与 machine/list 请求体。
            # 即使页面响应很慢、expect_response 超时，后续也能直接走 API 重试，
            # 不必每 10 秒整页 reload，更不会仅凭超时就误判“登录失效”。
            context.on("request", self._capture_platform_request)
        except Exception:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass
            if playwright is not None:
                try:
                    playwright.stop()
                except Exception:
                    pass
            self._page = None
            self._context = None
            self._playwright = None
            self._authorization = None
            self._machine_list_payload = None
            raise

    def close(self) -> None:
        """功能：
            关闭浏览器并释放所有资源。

        参数：
            无。

        返回：
            None：函数通过副作用完成初始化、输出、持久化或资源管理。
        """
        context = self._context
        playwright = self._playwright
        self._page = None
        self._context = None
        self._playwright = None
        self._authorization = None
        self._machine_list_payload = None
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        if playwright is not None:
            try:
                playwright.stop()
            except Exception:
                pass

    @staticmethod
    def _url_is_login(url: str) -> bool:
        """只根据明确 URL 证据判断是否已进入登录页。"""
        value = (url or "").lower()
        return "/login" in value and "autodl.com" in value

    def _capture_platform_request(self, request: Any) -> None:
        """捕获 machine/list 请求的 Authorization 和请求体，仅保存在内存。"""
        try:
            if (
                self.config.api_path_contains not in request.url
                or request.method.upper() != "POST"
            ):
                return
            try:
                headers = request.all_headers()
            except Exception:
                headers = request.headers
            authorization = headers.get("authorization") or headers.get("Authorization")
            if authorization:
                self._authorization = authorization

            payload = None
            try:
                payload = request.post_data_json
            except Exception:
                payload = None
            if isinstance(payload, dict):
                self._machine_list_payload = payload
        except Exception:
            # 监听器绝不能反向破坏浏览器主流程。
            return

    def _parse_machine_list_response(self, response: Any) -> list[PlatformHost]:
        """统一解析 machine/list 响应并区分认证失败与普通 HTTP 故障。"""
        if response.status in {401, 403}:
            raise PlatformAuthenticationError(
                f"AutoDL machine/list 返回 HTTP {response.status}；会话认证已失效。"
            )
        if response.status == 429 or response.status >= 500:
            raise PlatformTransientError(
                f"AutoDL machine/list 暂时不可用：HTTP {response.status}"
            )
        if response.status != 200:
            raise RuntimeError(f"AutoDL machine/list returned HTTP {response.status}")

        # Browser ``Response`` exposes ``response.request``; Playwright's
        # ``APIResponse`` (returned by ``context.request.post``) does not.
        # The direct-API path already has Authorization and request payload in
        # memory, so request metadata is optional here.  Never assume the
        # response object owns a ``request`` attribute.
        request = getattr(response, "request", None)
        if request is not None:
            try:
                headers = request.all_headers()
            except Exception:
                try:
                    headers = request.headers
                except Exception:
                    headers = {}
            authorization = headers.get("authorization") or headers.get("Authorization")
            if authorization:
                self._authorization = authorization
            try:
                request_payload = request.post_data_json
            except Exception:
                request_payload = None
            if isinstance(request_payload, dict):
                self._machine_list_payload = request_payload

        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("AutoDL machine/list returned non-object JSON")
        if payload.get("code") != "Success":
            raise RuntimeError(f"AutoDL machine/list failed: {payload.get('msg', payload)}")
        return parse_platform_payload(payload, aggregation=self.config.aggregation)

    def _collect_machine_list_direct(self) -> list[PlatformHost]:
        """使用已捕获 token/请求体直接调用 machine/list，避免每轮整页刷新。"""
        self.start()
        assert self._context is not None
        if not self._authorization or self._machine_list_payload is None:
            raise PlatformTransientError("尚未捕获到可复用的 machine/list 请求上下文")

        url = f"{self.config.api_base_url.rstrip('/')}/{self.config.api_path_contains.lstrip('/')}"
        last_error: Exception | None = None
        for attempt in range(1, self.config.max_attempts + 1):
            try:
                response = self._context.request.post(
                    url,
                    data=self._machine_list_payload,
                    headers={
                        "Accept": "application/json, text/plain, */*",
                        "Authorization": self._authorization,
                        "Origin": self.config.api_base_url.rstrip('/'),
                        "Referer": self.config.page_url,
                    },
                    timeout=self.config.response_timeout_seconds * 1000,
                )
                return self._parse_machine_list_response(response)
            except PlatformAuthenticationError:
                raise
            except (PlatformTransientError, PlaywrightTimeoutError) as exc:
                last_error = exc
                if attempt < self.config.max_attempts:
                    time.sleep(self.config.retry_delay_seconds)
                    continue
                break
        raise PlatformTransientError(
            "AutoDL 主机列表 API 本轮超时/暂时不可用；已登录状态不会因此被判定为失效。"
        ) from last_error

    def _collect_machine_list_via_browser(self) -> list[PlatformHost]:
        """通过页面触发 machine/list；只有明确登录页/401/403 才判定认证失效。"""
        self.start()
        assert self._page is not None

        def is_target(response: Any) -> bool:
            return (
                self.config.api_path_contains in response.url
                and response.request.method.upper() == "POST"
            )

        try:
            with self._page.expect_response(
                is_target,
                timeout=self.config.response_timeout_seconds * 1000,
            ) as response_info:
                if self._page.url == "about:blank":
                    self._page.goto(self.config.page_url, wait_until="domcontentloaded")
                else:
                    self._page.reload(wait_until="domcontentloaded")
            return self._parse_machine_list_response(response_info.value)
        except PlaywrightTimeoutError as exc:
            current_url = self._page.url
            if self._url_is_login(current_url):
                raise PlatformAuthenticationError(
                    f"AutoDL 已跳转到登录页：{current_url}"
                ) from exc

            # request 监听器可能已经拿到 token/请求体，只是网页响应过慢。
            # 此时直接 API 重试一次链路，而不是误弹登录窗口。
            if self._authorization and self._machine_list_payload is not None:
                try:
                    return self._collect_machine_list_direct()
                except PlatformAuthenticationError:
                    raise
                except PlatformTransientError as direct_exc:
                    raise PlatformTransientError(
                        f"AutoDL 控制台仍在 {current_url}，但主机列表接口本轮超时；"
                        "这是数据采集故障，不是登录失效。"
                    ) from direct_exc

            raise PlatformTransientError(
                f"AutoDL 控制台仍在 {current_url}，但本轮未捕获到主机列表响应；"
                "没有登录失效证据，本轮仅跳过开机。"
            ) from exc

    def post_api_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """功能：
            使用已保存的浏览器会话发送同源 POST API 请求。

        参数：
            path (str)：文件路径、API 相对路径或目标输出路径，具体含义由函数上下文决定。
            payload (dict[str, Any])：待解析的 JSON 字典，或发送给 AutoDL API 的请求体。

        返回：
            dict[str, Any]：AutoDL API 返回的 JSON 字典。

        补充说明：
            如果尚未捕获 Authorization，先调用 collect() 刷新并获取。
            令牌仅存内存，永不落盘。
        """
        self.start()
        assert self._context is not None

        if self._authorization is None:
            # Refreshing the machine list also refreshes/captures the current token.
            self.collect()
        if self._authorization is None:
            raise PlatformTransientError(
                "当前页面未捕获到 Authorization；没有 401/403 或 /login 证据，"
                "本轮不按登录失效处理。"
            )

        url = f"{self.config.api_base_url.rstrip('/')}/{path.lstrip('/')}"
        response = self._context.request.post(
            url,
            data=payload,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Authorization": self._authorization,
                "Origin": self.config.api_base_url.rstrip('/'),
                "Referer": self.config.page_url,
            },
            timeout=self.config.response_timeout_seconds * 1000,
        )
        if response.status == 401 or response.status == 403:
            raise PlatformAuthenticationError(
                f"AutoDL API 返回 HTTP {response.status}；登录会话可能已失效。"
            )
        if not response.ok:
            raise RuntimeError(f"AutoDL API {path} returned HTTP {response.status}")
        payload_json = response.json()
        if not isinstance(payload_json, dict):
            raise RuntimeError(f"AutoDL API {path} returned non-object JSON")
        return payload_json

    def _find_occupancy_action(self, machine_name: str):
        """兼容表格/卡片两种页面结构，定位指定入口的“查看占用”。"""
        assert self._page is not None

        # 旧页面：入口位于 <tr> 中。
        row = self._page.locator("tr").filter(has_text=machine_name).first
        if row.count() > 0:
            action = row.get_by_text("查看占用", exact=True)
            if action.count() > 0:
                return action.first

        # 新页面可能改为 card/div。先找到入口名文本，再逐级向父容器查找按钮。
        anchor = self._page.get_by_text(machine_name, exact=True).first
        if anchor.count() == 0:
            anchor = self._page.get_by_text(machine_name, exact=False).first
        if anchor.count() == 0:
            raise RuntimeError(f"页面中找不到机器入口：{machine_name}")

        container = anchor
        for _depth in range(10):
            action = container.get_by_text("查看占用", exact=True)
            if action.count() > 0:
                return action.first
            container = container.locator("xpath=..")

        raise RuntimeError(f"{machine_name} 附近找不到‘查看占用’按钮；页面结构可能变化")

    def _find_visible_occupancy_modal(self, machine_name: str):
        """找到标题和主机名都匹配的最小可见占用弹窗容器。"""
        assert self._page is not None
        deadline = time.monotonic() + self.config.response_timeout_seconds
        while time.monotonic() < deadline:
            titles = self._page.get_by_text("占用详情", exact=False)
            for idx in range(titles.count()):
                title = titles.nth(idx)
                try:
                    if not title.is_visible():
                        continue
                except Exception:
                    continue
                node = title
                for _depth in range(10):
                    try:
                        text = node.inner_text(timeout=500)
                    except Exception:
                        text = ""
                    if (
                        "占用详情" in text
                        and machine_name in text
                        and "GPU INDEX" in text
                        and "是否被占用" in text
                    ):
                        return node
                    node = node.locator("xpath=..")
            self._page.wait_for_timeout(100)
        raise RuntimeError(
            f"已点击 {machine_name} 查看占用，但没有出现与该入口匹配的占用详情弹窗"
        )

    def _close_occupancy_modal(self, modal) -> None:
        """关闭当前占用弹窗，并等待 DOM 真正隐藏，避免下一入口读到旧弹窗。"""
        assert self._page is not None
        try:
            close_button = modal.get_by_text("关闭", exact=True)
            if close_button.count() > 0:
                close_button.last.click(timeout=2000)
            else:
                self._page.keyboard.press("Escape")
        except Exception:
            try:
                self._page.keyboard.press("Escape")
            except Exception:
                return
        try:
            modal.wait_for(state="hidden", timeout=3000)
        except Exception:
            # 后续入口仍会做标题+machine_name 双重校验，因此这里不强行报错。
            pass

    def collect_occupancy(
        self,
        machine_name: str,
        *,
        expected_idle: int | None = None,
        expected_total: int | None = None,
    ) -> list[OccupancyRecord]:
        """读取一个入口的“占用详情”，并做入口身份及空位一致性校验。

        v0.5.4 不再扫描整个页面的 ``tr:visible``。只有标题容器中明确包含
        当前 machine_name 的弹窗才会被解析；若 machine/list 的 idle/total 与
        弹窗占用数矛盾，本轮直接标记采集失败，绝不拿另一个入口的数据补齐。
        """
        self.collect()
        assert self._page is not None

        # 先尽力关掉上轮异常残留的弹窗。即使未关干净，后续 machine_name 校验
        # 也会阻止读取旧入口。
        try:
            self._page.keyboard.press("Escape")
            self._page.wait_for_timeout(150)
        except Exception:
            pass

        action = self._find_occupancy_action(machine_name)
        action.click()
        modal = None
        try:
            modal = self._find_visible_occupancy_modal(machine_name)
            self._page.wait_for_timeout(200)

            observed_at = datetime.now()
            host = canonical_host(machine_name)
            result: list[OccupancyRecord] = []
            rows = modal.locator("tr")
            for index in range(rows.count()):
                cells = rows.nth(index).locator("td").all_inner_texts()
                record = parse_occupancy_cells(
                    cells,
                    observed_at=observed_at,
                    host=host,
                    machine_name=machine_name,
                )
                if record is not None:
                    result.append(record)

            result = sorted(result, key=lambda item: item.gpu_index)
            return validate_occupancy_snapshot(
                result,
                machine_name=machine_name,
                expected_idle=expected_idle,
                expected_total=expected_total,
            )
        finally:
            if modal is not None:
                self._close_occupancy_modal(modal)
            else:
                try:
                    self._page.keyboard.press("Escape")
                except Exception:
                    pass

    def collect(self) -> list[PlatformHost]:
        """采集平台主机列表。

        v0.5.4 状态机：
            1. 已捕获 token/请求体时优先直接 API，请求稳定且不刷新整个网页。
            2. 首次启动或 token 失效时才刷新控制台页面重新捕获请求上下文。
            3. 页面/API 超时属于 TRANSIENT，不触发登录。
            4. 只有明确进入 /login 或收到 401/403 才抛 PlatformAuthenticationError。
        """
        self.start()

        if self._authorization and self._machine_list_payload is not None:
            try:
                return self._collect_machine_list_direct()
            except PlatformAuthenticationError:
                # token 可能刚过期；先通过浏览器刷新一次，Cookie 仍有效时可自动拿到新 token。
                self._authorization = None
                self._machine_list_payload = None
                return self._collect_machine_list_via_browser()
            except PlatformTransientError:
                # 纯网络/API 抖动不整页刷新，避免给 AutoDL 页面和本机 Edge 增加额外压力。
                raise

        return self._collect_machine_list_via_browser()

