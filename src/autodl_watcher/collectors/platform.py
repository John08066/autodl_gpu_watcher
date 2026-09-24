from __future__ import annotations  # AutoDL 平台采集器 — 使用 Playwright 控制浏览器与 AutoDL 控制台交互。

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


_MACHINE_PATTERN = re.compile(r"^autodl-(\d+)-\d+$", re.IGNORECASE)  # 匹配 autodl-202-2 → 提取 202（物理主机号）


class PlatformAuthenticationError(RuntimeError):  # 已得到明确认证失败证据：登录页、HTTP 401/403 等。
    pass


class PlatformTransientError(RuntimeError):  # 平台页面/API 暂时不可用，但没有证据表明登录已经失效。
    pass


class OccupancySnapshotMismatchError(RuntimeError):  # 占用弹窗与目标入口/平台空位互相矛盾，拒绝把该轮数据当成事实。
    pass


def canonical_host(machine_name: str) -> str:  # 将 AutoDL 入口名映射到对应的物理主机名。
    match = _MACHINE_PATTERN.match(machine_name.strip())
    if match:
        return f"gpu-{match.group(1)}"
    return machine_name.strip()


def parse_platform_payload(payload: dict[str, Any], aggregation: str = "max") -> list[PlatformHost]:  # 解析 AutoDL 控制台 machine/list API 的响应。
    rows = payload.get("data", {}).get("list", [])
    grouped: dict[str, list[tuple[str, int, int]]] = defaultdict(list)  # 物理主机名映射到多个入口的 (入口名, 空闲数, 总数)。

    for row in rows:
        gpu = row.get("gpu") or {}
        machine_name = str(row.get("machine_name", "")).strip()
        total = int(gpu.get("total", 0))
        idle = int(gpu.get("idle", 0))
        if not machine_name or total <= 0 or idle < 0:
            continue
        idle = min(idle, total)  # 平台空闲数异常偏大时限制到总数，保证模型计数合法。
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
            free_count = max(idle_values)  # 默认按最宽松入口判断是否有空位，避免把共享入口直接累加。
            total_count = max(total_values)

        result.append(
            PlatformHost(
                host=host,
                free_count=min(free_count, total_count),
                total_count=total_count,
                source_names=tuple(entry[0] for entry in sorted(entries)),
                source_slots=tuple(sorted(entries)),  # 保留入口级原始空位，后续选实例不能只看主机聚合值。
            )
        )
    return result


def parse_occupancy_cells(
    cells: list[str],
    *,
    observed_at: datetime,
    host: str,
    machine_name: str,
) -> OccupancyRecord | None:  # 解析 AutoDL "查看占用"弹窗中的一行表格数据。
    values = [str(item).strip() for item in cells]
    if len(values) < 6:  # 新版弹窗为 6 列，旧版为 7 列；不完整行不能生成占用记录。
        return None
    try:
        gpu_index = int(values[0])
    except ValueError:
        return None
    occupied_text = values[3]  # 新版列顺序为 INDEX、UUID、名称、占用、实例、开始时间。
    if occupied_text not in {"是", "否"}:
        return None
    has_user_column = len(values) >= 7  # 旧版在实例与开始时间之间另有用户列。
    return OccupancyRecord(
        observed_at=observed_at,
        host=host,
        machine_name=machine_name,
        gpu_index=gpu_index,
        gpu_uuid=values[1],
        gpu_name=values[2],
        occupied=occupied_text == "是",
        instance_id="" if values[4] == "-" else values[4],  # 把网页占位符转为空值，避免被误当作真实实例 ID。
        user="" if not has_user_column or values[5] == "-" else values[5],
        started_at_text="" if values[6 if has_user_column else 5] == "-" else values[6 if has_user_column else 5],
    )


def validate_occupancy_snapshot(
    records: list[OccupancyRecord],
    *,
    machine_name: str,
    expected_idle: int | None = None,
    expected_total: int | None = None,
    expected_gpu_indices: set[int] | None = None,
) -> list[OccupancyRecord]:  # 校验一次占用弹窗是否真的属于目标入口且与 machine/list 一致。
    if not records:
        raise OccupancySnapshotMismatchError( f"{machine_name} 占用弹窗没有解析到 GPU 行" )
    if any(item.machine_name != machine_name for item in records):
        raise OccupancySnapshotMismatchError( f"{machine_name} 占用记录混入了其他入口" )

    indexes = [item.gpu_index for item in records]
    if len(indexes) != len(set(indexes)):  # 同一入口出现重复物理索引说明读取结果有歧义。
        raise OccupancySnapshotMismatchError( f"{machine_name} 占用弹窗出现重复 GPU INDEX：{indexes}" )

    if expected_total is not None and len(records) > expected_total and expected_gpu_indices is not None:
        extra = [item for item in records if item.gpu_index not in expected_gpu_indices]
        if (len(expected_gpu_indices) == expected_total and expected_gpu_indices.issubset(indexes)
                and all(not item.occupied and not item.instance_id and not item.user for item in extra)):
            records = [item for item in records if item.gpu_index in expected_gpu_indices]  # 仅排除新鲜 Telemetry 索引之外、且无任何占用证据的额外空行。

    if expected_total is not None:
        if expected_total <= 0:
            raise OccupancySnapshotMismatchError( f"{machine_name} 平台 total={expected_total} 无效" )
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
        expected_occupied = expected_total - expected_idle  # 用主机列表的计数交叉验证弹窗，防止读取到旧弹窗或半加载内容。
        if actual_occupied != expected_occupied:
            raise OccupancySnapshotMismatchError(
                f"{machine_name} 占用弹窗显示 {actual_occupied} 张被占用，"
                f"但平台 idle/total={expected_idle}/{expected_total}，"
                f"应为 {expected_occupied} 张；本轮拒绝采用"
            )

    return records


class PlatformBrowserCollector:  # AutoDL 平台数据采集器 — 通过 Playwright 控制浏览器与 AutoDL 交互。

    def __init__(self, config: PlatformConfig) -> None:  # 初始化 AutoDL 平台浏览器采集器，但暂不启动浏览器进程。
        self.config = config
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None   # 持久化浏览器上下文
        self._page: Page | None = None                # 当前页面
        self._authorization: str | None = None        # 内存中的 API 令牌
        self._machine_list_payload: dict[str, Any] | None = None
        self._instance_tenant_uuid: str | None = None  # 仅在本次浏览器会话内缓存实例列表页面的租户标识。

    def start(self) -> None:  # 启动（或复用）Playwright 浏览器实例。
        if self._context is not None:  # 复用同一持久化会话，不为每次采样重新启动浏览器。
            return
        self.config.user_data_dir.mkdir(parents=True, exist_ok=True)

        prepare_profile_for_exclusive_use(self.config.user_data_dir)  # v0.5.1：启动后台监控 Edge 前先清理上次异常退出遗留的 Edge/锁文件。 这样登录结束后不需要手工执行菜单 7 才能重新启动监控。

        playwright = None  # 首次启动失败时必须把 sync_playwright() 完整释放。 旧版若 browser_profile 被 Edge 锁住，launch 失败后 _playwright 会残留， 下一轮便持续报“Sync API inside the asyncio loop”。
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
                bypass_arg = self.config.proxy_bypass_list.strip()  # v0.5.4：AutoDL 控制面在该 PC 上经 Clash 系统代理明显变慢。 监控专用 Edge 对 AutoDL 域名直连；其余流量仍可走本机 Clash。
                launch_kwargs["args"] = [f"--proxy-bypass-list={bypass_arg}"]

                proxy_server = ( os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or "" ).strip()
                if proxy_server:
                    bypass = ",".join(  # Playwright proxy.bypass 使用逗号分隔。给 BrowserContext 与 其关联 APIRequestContext 同一代理策略，避免浏览器直连、API 又绕韩国节点。
                        item
                        for item in bypass_arg.replace("<local>", "localhost;127.0.0.1").split(";")
                        if item
                    )
                    launch_kwargs["proxy"] = { "server": proxy_server, "bypass": bypass, }
            context = playwright.chromium.launch_persistent_context(**launch_kwargs)  # 专用用户目录持久保存登录状态，供后续采集请求复用。
            self._playwright = playwright
            self._context = context
            self._page = context.pages[0] if context.pages else context.new_page()
            context.on("request", self._capture_platform_request)  # v0.5.2：请求一发出就捕获 Authorization 与 machine/list 请求体。 即使页面响应很慢、expect_response 超时，后续也能直接走 API 重试， 不必每 10 秒整页 reload，更不会仅凭超时就误判“登录失效”。
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
            self._instance_tenant_uuid = None
            raise

    def close(self) -> None:  # 关闭浏览器并释放所有资源。
        context = self._context
        playwright = self._playwright
        self._page = None
        self._context = None
        self._playwright = None
        self._authorization = None
        self._machine_list_payload = None
        self._instance_tenant_uuid = None
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
    def _url_is_login(url: str) -> bool:  # 只根据明确 URL 证据判断是否已进入登录页。
        value = (url or "").lower()
        return "/login" in value and "autodl.com" in value

    def _capture_platform_request(self, request: Any) -> None:  # 捕获 machine/list 请求的 Authorization 和请求体，仅保存在内存。
        try:
            if (
                self.config.api_path_contains not in request.url or request.method.upper() != "POST"
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
            return  # 监听器绝不能反向破坏浏览器主流程。

    def _parse_machine_list_response(self, response: Any) -> list[PlatformHost]:  # 统一解析 machine/list 响应并区分认证失败与普通 HTTP 故障。
        if response.status in {401, 403}:
            raise PlatformAuthenticationError( f"AutoDL machine/list 返回 HTTP {response.status}；会话认证已失效。" )
        if response.status == 429 or response.status >= 500:
            raise PlatformTransientError( f"AutoDL machine/list 暂时不可用：HTTP {response.status}" )
        if response.status != 200:
            raise RuntimeError(f"AutoDL machine/list returned HTTP {response.status}")

        request = getattr(response, "request", None)  # Browser ``Response`` exposes ``response.request``; Playwright's ``APIResponse`` (returned by ``context.request.post``) does not. The direct-API path already has Authorization and request payload in memory, so request metadata is optional here.  Never assume the response object owns a ``request`` attribute.
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

    def _collect_machine_list_direct(self) -> list[PlatformHost]:  # 使用已捕获 token/请求体直接调用 machine/list，避免每轮整页刷新。
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
        raise PlatformTransientError( "AutoDL 主机列表 API 本轮超时/暂时不可用；已登录状态不会因此被判定为失效。" ) from last_error

    def _collect_machine_list_via_browser(self) -> list[PlatformHost]:  # 通过页面触发 machine/list；只有明确登录页/401/403 才判定认证失效。
        self.start()
        assert self._page is not None

        def is_target(response: Any) -> bool:
            return (
                self.config.api_path_contains in response.url and response.request.method.upper() == "POST"
            )

        try:
            with self._page.expect_response( is_target, timeout=self.config.response_timeout_seconds * 1000, ) as response_info:
                if self._page.url == "about:blank":
                    self._page.goto(self.config.page_url, wait_until="domcontentloaded")
                else:
                    self._page.reload(wait_until="domcontentloaded")
            return self._parse_machine_list_response(response_info.value)
        except PlaywrightTimeoutError as exc:
            current_url = self._page.url
            if self._url_is_login(current_url):
                raise PlatformAuthenticationError( f"AutoDL 已跳转到登录页：{current_url}" ) from exc

            if self._authorization and self._machine_list_payload is not None:  # request 监听器可能已经拿到 token/请求体，只是网页响应过慢。 此时直接 API 重试一次链路，而不是误弹登录窗口。
                try:
                    return self._collect_machine_list_direct()
                except PlatformAuthenticationError:
                    raise
                except PlatformTransientError as direct_exc:
                    raise PlatformTransientError(
                        f"AutoDL 控制台仍在 {current_url}，但主机列表接口本轮超时；"
                        "这是数据采集故障，不是登录失效。"
                    ) from direct_exc

            raise PlatformTransientError( f"AutoDL 控制台仍在 {current_url}，但本轮未捕获到主机列表响应；" "没有登录失效证据，本轮仅跳过开机。" ) from exc

    def post_api_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:  # 使用已保存的浏览器会话发送同源 POST API 请求。
        self.start()
        assert self._context is not None

        if self._authorization is None:
            self.collect()  # Refreshing the machine list also refreshes/captures the current token.
        if self._authorization is None:
            raise PlatformTransientError( "当前页面未捕获到 Authorization；没有 401/403 或 /login 证据，" "本轮不按登录失效处理。" )

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
            raise PlatformAuthenticationError( f"AutoDL API 返回 HTTP {response.status}；登录会话可能已失效。" )
        if not response.ok:
            raise RuntimeError(f"AutoDL API {path} returned HTTP {response.status}")
        payload_json = response.json()
        if not isinstance(payload_json, dict):
            raise RuntimeError(f"AutoDL API {path} returned non-object JSON")
        return payload_json

    def get_account_instances(self) -> list[dict[str, Any]]:  # 读取当前账号完整实例列表；只有全部分页一致且 UUID 唯一才返回。
        self.start()
        assert self._context is not None
        if self._instance_tenant_uuid is None:
            page = self._context.new_page()  # 独立页面采集真实租户请求体，不影响主机占用弹窗。
            try:
                def is_instance_list(request: Any) -> bool:
                    return request.url.startswith(f"{self.config.api_base_url.rstrip('/')}/api/v2/instance/list") and request.method.upper() == "POST"

                try:
                    with page.expect_request(
                        is_instance_list,
                        timeout=self.config.response_timeout_seconds * 1000,
                    ) as request_info:
                        page.goto(
                            f"{self.config.api_base_url.rstrip('/')}/console/instance",
                            wait_until="domcontentloaded",
                        )
                    request_payload = request_info.value.post_data_json
                except PlaywrightTimeoutError as exc:
                    if self._url_is_login(page.url):
                        raise PlatformAuthenticationError("AutoDL 实例列表已跳转登录页") from exc
                    raise PlatformTransientError("未捕获到 AutoDL 实例列表请求") from exc
            finally:
                page.close()

            tenant_uuid = request_payload.get("tenant_uuid") if isinstance(request_payload, dict) else None
            if not isinstance(tenant_uuid, str) or not tenant_uuid.strip():
                raise PlatformTransientError("实例列表请求缺少 tenant_uuid")
            self._instance_tenant_uuid = tenant_uuid

        page_size = 10  # 与官网实例列表默认分页一致；逐页核对后才允许认定目标状态。
        total: int | None = None
        all_rows: list[dict[str, Any]] = []
        seen_uuids: set[str] = set()  # 翻页期间若出现重复行，可能漏掉其他有卡实例，整份列表作废。
        page_index = 1
        while total is None or (page_index - 1) * page_size < total:
            response = self.post_api_json(
                "/api/v2/instance/list",
                {"tenant_uuid": self._instance_tenant_uuid, "page_index": page_index, "page_size": page_size},
            )
            data = response.get("data")
            if response.get("code") != "Success" or not isinstance(data, dict):
                raise PlatformTransientError("实例列表 API 未返回成功数据")
            rows = data.get("list")
            count = data.get("result_total")
            if not isinstance(rows, list) or type(count) is not int or count < 0:
                raise PlatformTransientError("实例列表缺少可靠的 list/result_total")
            if total is None:
                total = count
            expected_rows = min(page_size, max(0, total - (page_index - 1) * page_size))
            if count != total or len(rows) != expected_rows:
                raise PlatformTransientError("实例列表分页结果不完整或发生变化")
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("instance_uuid"), str):
                    raise PlatformTransientError("实例列表包含无法识别的实例")
                row_uuid = row["instance_uuid"]
                if not row_uuid.strip() or row_uuid in seen_uuids:
                    raise PlatformTransientError("实例列表分页有空或重复 UUID，无法确认全部实例")
                seen_uuids.add(row_uuid)
                all_rows.append({key: row.get(key) for key in ("instance_uuid", "machine_name", "status", "start_mode")})
            page_index += 1
        return all_rows

    def get_instance_state(self, instance_uuid: str, machine_name: str) -> dict[str, Any]:  # 双重匹配实例，并核对同主机账号有卡占用。
        if not instance_uuid.strip() or not _MACHINE_PATTERN.fullmatch(machine_name.strip()):
            raise ValueError("instance_uuid 或 machine_name 格式无效")
        all_rows = self.get_account_instances()
        matches = [row for row in all_rows if row["instance_uuid"] == instance_uuid]
        if len(matches) != 1:
            raise PlatformTransientError("实例列表中目标 UUID 缺失或重复")
        if matches[0].get("machine_name") != machine_name:
            raise PlatformTransientError("目标实例 UUID 与配置入口名不匹配")
        status = matches[0].get("status")
        start_mode = matches[0].get("start_mode")
        if not isinstance(status, str) or not status.strip() or not isinstance(start_mode, str):
            raise PlatformTransientError("目标实例缺少 status/start_mode")
        target_host = canonical_host(machine_name)  # 覆盖配置外的同物理主机入口。
        host_account_gpu_clear = True
        for row in all_rows:
            name, row_status, mode = row.get("machine_name"), row.get("status"), row.get("start_mode")
            if not isinstance(name, str) or not _MACHINE_PATTERN.fullmatch(name.strip()):
                if row_status != "shutdown":  # 活跃实例无法归属主机时不能证明本主机无占用。
                    host_account_gpu_clear = False
                continue
            if canonical_host(name) != target_host:
                continue
            if row_status == "shutdown" or (row_status == "running" and mode == "non_gpu"):
                continue
            host_account_gpu_clear = False  # 有卡、启动中、关机中或未知状态均不视为 GPU 已空闲。
        return {"status": status, "start_mode": start_mode, "host_account_gpu_clear": host_account_gpu_clear}

    def _find_occupancy_action(self, machine_name: str):  # 兼容表格/卡片两种页面结构，定位指定入口的“查看占用”。
        assert self._page is not None

        row = self._page.locator("tr").filter(has_text=machine_name).first  # 旧页面：入口位于 <tr> 中。
        if row.count() > 0:
            action = row.get_by_text("查看占用", exact=True)
            if action.count() > 0:
                return action.first

        anchor = self._page.get_by_text(machine_name, exact=True).first  # 新页面可能改为 card/div。先找到入口名文本，再逐级向父容器查找按钮。
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

    def _find_visible_occupancy_modal(self, machine_name: str):  # 找到标题和主机名都匹配的最小可见占用弹窗容器。
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
                        "占用详情" in text and machine_name in text and "GPU INDEX" in text and "是否被占用" in text
                    ):
                        return node
                    node = node.locator("xpath=..")
            self._page.wait_for_timeout(100)
        raise RuntimeError( f"已点击 {machine_name} 查看占用，但没有出现与该入口匹配的占用详情弹窗" )

    def _close_occupancy_modal(self, modal) -> None:  # 关闭当前占用弹窗，并等待 DOM 真正隐藏，避免下一入口读到旧弹窗。
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
            pass  # 后续入口仍会做标题+machine_name 双重校验，因此这里不强行报错。

    def collect_occupancy(
        self,
        machine_name: str,
        *,
        expected_idle: int | None = None,
        expected_total: int | None = None,
        expected_gpu_indices: set[int] | None = None,
    ) -> list[OccupancyRecord]:  # 读取一个入口的“占用详情”，并做入口身份及空位一致性校验。
        self.collect()
        assert self._page is not None

        try:  # 先尽力关掉上轮异常残留的弹窗。即使未关干净，后续 machine_name 校验 也会阻止读取旧入口。
            self._page.keyboard.press("Escape")
            self._page.wait_for_timeout(150)
        except Exception:
            pass

        action = self._find_occupancy_action(machine_name)
        action.click()
        modal = None
        try:
            modal = self._find_visible_occupancy_modal(machine_name)
            deadline = time.monotonic() + self.config.response_timeout_seconds  # 表头先出现，GPU 行可能异步加载。
            host = canonical_host(machine_name)
            while True:
                observed_at = datetime.now()
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
                try:
                    return validate_occupancy_snapshot(
                        result,
                        machine_name=machine_name,
                        expected_idle=expected_idle,
                        expected_total=expected_total,
                        expected_gpu_indices=expected_gpu_indices,
                    )
                except OccupancySnapshotMismatchError:  # 足额行也可能尚未更新占用状态；只接受完整一致的快照。
                    if time.monotonic() >= deadline:
                        raise
                    self._page.wait_for_timeout(100)
        finally:
            if modal is not None:
                self._close_occupancy_modal(modal)
            else:
                try:
                    self._page.keyboard.press("Escape")
                except Exception:
                    pass

    def collect(self) -> list[PlatformHost]:  # 采集平台主机列表。
        self.start()

        if self._authorization and self._machine_list_payload is not None:
            try:
                return self._collect_machine_list_direct()
            except PlatformAuthenticationError:
                self._authorization = None  # token 可能刚过期；先通过浏览器刷新一次，Cookie 仍有效时可自动拿到新 token。
                self._machine_list_payload = None
                return self._collect_machine_list_via_browser()
            except PlatformTransientError:
                raise  # 纯网络/API 抖动不整页刷新，避免给 AutoDL 页面和本机 Edge 增加额外压力。

        return self._collect_machine_list_via_browser()
