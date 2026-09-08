from __future__ import annotations  # Telemetry API 采集器 — 通过 HTTP GET 请求自建 API 获取物理 GPU 显存快照。

import logging
import time
from datetime import datetime
from typing import Any

import requests

from ..config import TelemetryConfig
from ..models import GpuSample, PlatformHost


_LOGGER = logging.getLogger("autodl_watcher")


def _parse_received_at(value: str) -> datetime:  # 解析 Telemetry API 返回的时间戳字符串。
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    aware = datetime.fromisoformat(text)
    if aware.tzinfo is None:
        return aware
    return aware.astimezone().replace(tzinfo=None)  # 统一为本地无时区时间，与主循环 datetime.now() 比较新鲜度。


def parse_telemetry_payload(payload: dict[str, Any], only_online: bool = True) -> list[GpuSample]:  # 解析 Telemetry API 的 JSON 响应，返回 GpuSample 列表。
    result: list[GpuSample] = []
    for item in payload.get("items", []):
        host_status = str(item.get("host_status", "")).lower()
        if only_online and host_status != "online":
            continue
        try:
            result.append(
                GpuSample(
                    host=str(item["host"]),
                    gpu_index=int(item["gpu_index"]),
                    gpu_name=str(item["name"]),
                    util_pct=float(item["util_gpu"]),
                    memory_used_mb=int(round(float(item["mem_used_mb"]))),
                    memory_total_mb=int(round(float(item["mem_total_mb"]))),
                    observed_at=_parse_received_at(str(item["received_at"])),  # 采用服务端记录的接收时间，不能用本次拉取时间掩盖旧数据。
                )
            )
        except (KeyError, TypeError, ValueError):
            continue  # 单条解析失败不影响整体
    return result


def filter_samples_to_platform_candidates(
    samples: list[GpuSample],
    platform_hosts: list[PlatformHost],
) -> tuple[list[GpuSample], tuple[str, ...], tuple[str, ...]]:  # 两层门控过滤 — 这是整个系统最关键的交叉验证步骤。
    visible = {item.host for item in platform_hosts}  # 以当前账号平台实际可见的主机作为访问范围。
    candidates = {item.host for item in platform_hosts if item.free_count > 0}  # 物理显存空闲还不够，平台必须同时存在可分配入口空位。
    accepted = [sample for sample in samples if sample.host in candidates]
    no_slot = tuple(sorted(visible - candidates))  # 区分平台可见但没空位，与平台根本不可见这两种排除原因。
    unauthorized = tuple(sorted({sample.host for sample in samples if sample.host not in visible}))
    return accepted, no_slot, unauthorized
class TelemetryApiCollector:  # Telemetry API 采集器 — 通过 HTTP GET 拉取物理 GPU 实时数据。

    def __init__(self, config: TelemetryConfig) -> None:  # 初始化 Telemetry API 采集器和可复用的 HTTP 会话。
        self.config = config
        self._session = requests.Session()  # 复用 HTTP 连接；默认仍遵循此进程的代理环境。
        self._session.headers.update(
            {
                "Accept": "application/json",
                "Cache-Control": "no-cache",
                "User-Agent": "autodl-gpu-watcher/0.5.1",
            }
        )

    @staticmethod
    def _is_retryable(exc: requests.RequestException) -> bool:  # 判断一次 HTTP 失败是否属于适合立即重试的瞬时故障。
        if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
            return True
        if isinstance(exc, requests.HTTPError) and exc.response is not None:
            status = int(exc.response.status_code)
            return status == 429 or status >= 500  # 限流和服务端故障可重试，其他 HTTP 错误交回主循环处理。
        return False

    def collect(self) -> list[GpuSample]:  # 拉取 Telemetry GPU 快照，并对瞬时网络故障进行有限重试。
        last_error: requests.RequestException | None = None

        for attempt in range(1, self.config.max_attempts + 1):
            try:
                response = self._session.get(
                    self.config.endpoint,
                    params={"_t": int(time.time() * 1000)},  # 时间戳参数防止缓存
                    timeout=self.config.timeout_seconds,
                )
                response.raise_for_status()  # 先检查 HTTP 状态，再解析 JSON，避免把错误页当作空快照。
                samples = parse_telemetry_payload( response.json(), only_online=self.config.only_online, )
                if attempt > 1:
                    _LOGGER.info(
                        "telemetry recovered attempt=%d/%d endpoint=%s",
                        attempt,
                        self.config.max_attempts,
                        self.config.endpoint,
                    )
                return samples
            except requests.RequestException as exc:
                last_error = exc
                can_retry = (
                    attempt < self.config.max_attempts and self._is_retryable(exc)
                )
                if not can_retry:  # 次数耗尽或错误不可重试时抛出，不能返回伪造的空闲结果。
                    raise

                _LOGGER.warning(
                    "telemetry transient failure; retrying attempt=%d/%d delay=%.1fs error=%s",
                    attempt,
                    self.config.max_attempts,
                    self.config.retry_delay_seconds,
                    exc,
                )
                if self.config.retry_delay_seconds > 0:
                    time.sleep(self.config.retry_delay_seconds)

        if last_error is not None:  # 理论上循环只会通过 return 或 raise 离开；保留防御式分支。
            raise last_error
        raise RuntimeError("telemetry collection ended without response")
