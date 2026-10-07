from __future__ import annotations  # Telemetry API 采集器 — 通过 HTTP GET 请求自建 API 获取物理 GPU 显存快照。

import logging
import math
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
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


class TelemetryUnavailable(RuntimeError):
    """GPU HTTP 快照不可用或处于退避期，不能用于开机判断。"""


class TelemetryApiCollector:  # Telemetry API 采集器 — 通过 HTTP GET 拉取物理 GPU 实时数据。

    def __init__(self, config: TelemetryConfig) -> None:  # 初始化 Telemetry API 采集器和可复用的 HTTP 会话。
        self.config = config
        self._failed_rounds = 0
        self._retry_at = 0.0
        self._session = requests.Session()  # 复用 HTTP 连接；默认仍遵循此进程的代理环境。
        self._session.headers.update(
            {
                "Accept": "application/json",
                "Cache-Control": "no-cache",
                "User-Agent": "autodl-gpu-watcher/0.5.1",
            }
        )

    def close(self) -> None:
        self._session.close()

    @staticmethod
    def _retry_after(response: requests.Response | None) -> float:
        value = response.headers.get("Retry-After") if response is not None else None
        if not isinstance(value, str):
            return 0.0
        if value.strip().isdigit():
            return float(value.strip())
        try:
            return max(0.0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return 0.0

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
            return True
        return (isinstance(exc, requests.HTTPError) and exc.response is not None
                and exc.response.status_code >= 500)

    def collect(self) -> list[GpuSample]:
        remaining = self._retry_at - time.monotonic()
        if remaining > 0:
            raise TelemetryUnavailable(
                f"Telemetry HTTP 冷却中，至少 {math.ceil(remaining)} 秒后重试；本轮未发送请求")
        attempts = min(self.config.max_attempts, 2)
        for attempt in range(1, attempts + 1):
            response = None
            try:
                try:
                    response = self._session.get(
                        self.config.endpoint,
                        params={"_t": int(time.time() * 1000)},
                        timeout=self.config.timeout_seconds,
                    )
                    response.raise_for_status()
                    samples = parse_telemetry_payload(response.json(), only_online=self.config.only_online)
                finally:
                    if response is not None:
                        response.close()
                if attempt > 1 or self._failed_rounds:
                    _LOGGER.info("telemetry recovered attempt=%d/%d failed_rounds=%d",
                                 attempt, attempts, self._failed_rounds)
                self._failed_rounds = 0
                self._retry_at = 0.0
                return samples
            except (requests.RequestException, ValueError) as exc:
                retry_after = self._retry_after(response)
                if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
                    # 仅清除此采集器的连接池；Session仍可重用，不影响SSH或平台会话。
                    self._session.close()
                if attempt < attempts and self._is_retryable(exc) and not retry_after:
                    _LOGGER.warning("telemetry transient failure; retrying attempt=%d/%d delay=%.1fs error=%s",
                                    attempt, attempts, self.config.retry_delay_seconds, exc)
                    if self.config.retry_delay_seconds > 0:
                        time.sleep(self.config.retry_delay_seconds)
                    continue
                self._failed_rounds += 1
                delay = max(min(60 * 2 ** min(self._failed_rounds - 1, 3), 300), retry_after)
                self._retry_at = time.monotonic() + delay
                status = f"HTTP {response.status_code}：{exc}" if response is not None else str(exc)
                raise TelemetryUnavailable(
                    f"Telemetry HTTP 采集失败（本轮 {attempt} 次请求，连续 {self._failed_rounds} 轮失败；"
                    f"至少 {math.ceil(delay)} 秒后重试）：{status}") from exc
