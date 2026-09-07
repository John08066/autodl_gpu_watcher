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
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.config.user_data_dir),
                channel=self.config.browser_channel,
                headless=self.config.headless,
                chromium_sandbox=True,
            )
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

        try:
            headers = response.request.all_headers()
        except Exception:
            headers = response.request.headers
        authorization = headers.get("authorization") or headers.get("Authorization")
        if authorization:
            self._authorization = authorization
        try:
            request_payload = response.request.post_data_json
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

    def collect_occupancy(self, machine_name: str) -> list[OccupancyRecord]:
        """功能：
            Read the visible ``查看占用`` modal for one AutoDL machine entry.。

        参数：
            machine_name (str)：AutoDL 平台入口名，例如 `autodl-203-2`；可为 `None` 表示自动选择。

        返回：
            list[OccupancyRecord]：指定入口当前所有 GPU INDEX 的占用记录。

        补充说明：
            This intentionally uses the page DOM because the occupancy API has not
            yet been identified.  The method is isolated from the critical auto-
            start loop and is used by ``usage_monitor`` in a separate process.
        """
        self.collect()
        assert self._page is not None

        action = self._find_occupancy_action(machine_name)
        action.click()
        try:
            self._page.get_by_text("占用详情", exact=False).last.wait_for(
                state="visible",
                timeout=self.config.response_timeout_seconds * 1000,
            )
            self._page.wait_for_timeout(300)

            observed_at = datetime.now()
            host = canonical_host(machine_name)
            result: list[OccupancyRecord] = []
            rows = self._page.locator("tr:visible")
            for index in range(rows.count()):
                cells = rows.nth(index).locator("td:visible").all_inner_texts()
                record = parse_occupancy_cells(
                    cells,
                    observed_at=observed_at,
                    host=host,
                    machine_name=machine_name,
                )
                if record is not None:
                    result.append(record)
            if not result:
                raise RuntimeError(
                    f"已打开 {machine_name} 占用详情，但没有解析到 GPU 行；页面结构可能变化。"
                )
            return sorted(result, key=lambda item: item.gpu_index)
        finally:
            close_button = self._page.get_by_text("关闭", exact=True)
            if close_button.count() > 0:
                try:
                    close_button.last.click(timeout=2000)
                except Exception:
                    pass

    def collect(self) -> list[PlatformHost]:
        """采集平台主机列表。

        v0.5.2 状态机：
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

