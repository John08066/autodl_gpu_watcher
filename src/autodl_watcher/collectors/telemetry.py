"""
Telemetry API 采集器 — 通过 HTTP GET 请求自建 API 获取物理 GPU 显存快照。

数据源：
    自建的 Telemetry 服务（https://watchgpu.vpms-lab.com/api/latest），
    该服务从各物理机收集 GPU 信息（通过 nvidia-smi 等工具），
    返回每张 GPU 的 host、gpu_index、显存用量、利用率等实时数据。

设计要点：
    - 使用 requests.Session 复用 HTTP 连接
    - 时间戳处理兼容 "Z" 结尾的 ISO 格式和带时区信息的时间
    - 可配置是否只接受 host_status=online 的样本
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

import requests

from ..config import TelemetryConfig
from ..models import GpuSample, PlatformHost


_LOGGER = logging.getLogger("autodl_watcher")


def _parse_received_at(value: str) -> datetime:
    """功能：
        解析 Telemetry API 返回的时间戳字符串。

    参数：
        value (str)：待规范化、解析或转换的输入值。

    返回：
        datetime：去除时区后的本地 datetime。

    补充说明：
        兼容格式：
            - "2026-07-20T08:00:53Z" → 转为 UTC
            - "2026-07-20T08:00:53+08:00" → 转为本地时间后去掉 tzinfo
    """
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    aware = datetime.fromisoformat(text)
    if aware.tzinfo is None:
        return aware
    return aware.astimezone().replace(tzinfo=None)


def parse_telemetry_payload(payload: dict[str, Any], only_online: bool = True) -> list[GpuSample]:
    """功能：
        解析 Telemetry API 的 JSON 响应，返回 GpuSample 列表。

    参数：
        payload (dict[str, Any])：待解析的 JSON 字典，或发送给 AutoDL API 的请求体。
        only_online (bool)：是否仅保留 Telemetry 标记为 online 的主机样本。

    返回：
        list[GpuSample]：成功解析的物理 GPU 样本列表。

    补充说明：
        过滤规则：
            - only_online=True 时，只接受 host_status="online" 的样本
            - 解析失败的单条记录会静默跳过（不中断整体解析）
    """
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
                    observed_at=_parse_received_at(str(item["received_at"])),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue  # 单条解析失败不影响整体
    return result


def filter_samples_to_platform_candidates(
    samples: list[GpuSample],
    platform_hosts: list[PlatformHost],
) -> tuple[list[GpuSample], tuple[str, ...], tuple[str, ...]]:
    """功能：
        两层门控过滤 — 这是整个系统最关键的交叉验证步骤。

    参数：
        samples (list[GpuSample])：物理 GPU 采样对象列表。
        platform_hosts (list[PlatformHost])：当前账号可见的 AutoDL 平台主机聚合状态列表。

    返回：
        tuple[list[GpuSample], tuple[str, ...], tuple[str, ...]]：三元组：通过门控的样本、平台满载主机、当前账号无权限主机。

    补充说明：
        Stage 1 — 平台可见性门控：
            只保留在 platform_hosts 中出现的主机（当前账号在 AutoDL 上能看到的主机）。
            其中 free_count > 0 的才是候选主机；free_count = 0 的主机被标记为 no_slot。

        Stage 2 — 物理采样过滤：
            只保留候选主机的 telemetry 样本，其他主机的样本被标记为 unauthorized。

        返回值：
            (accepted_samples, no_platform_slot_hosts, unauthorized_hosts)
            - accepted: 通过两层门控的样本
            - no_slot: 平台可见但无空闲 GPU ID 的主机
            - unauthorized: 当前账号不可申请的主机
    """
    visible = {item.host for item in platform_hosts}
    candidates = {item.host for item in platform_hosts if item.free_count > 0}
    accepted = [sample for sample in samples if sample.host in candidates]
    no_slot = tuple(sorted(visible - candidates))
    unauthorized = tuple(sorted({sample.host for sample in samples if sample.host not in visible}))
    return accepted, no_slot, unauthorized


def filter_samples_to_authorized_hosts(
    samples: list[GpuSample],
    platform_hosts: list[PlatformHost],
) -> tuple[list[GpuSample], tuple[str, ...]]:
    """功能：
        向后兼容的封装。新代码请直接使用 filter_samples_to_platform_candidates。

    参数：
        samples (list[GpuSample])：物理 GPU 采样对象列表。
        platform_hosts (list[PlatformHost])：当前账号可见的 AutoDL 平台主机聚合状态列表。

    返回：
        tuple[list[GpuSample], tuple[str, ...]]：二元组：通过门控的样本与无权限主机。
    """
    accepted, _no_slot, unauthorized = filter_samples_to_platform_candidates(
        samples, platform_hosts
    )
    return accepted, unauthorized


class TelemetryApiCollector:
    """Telemetry API 采集器 — 通过 HTTP GET 拉取物理 GPU 实时数据。"""

    def __init__(self, config: TelemetryConfig) -> None:
        """功能：
            初始化 Telemetry API 采集器和可复用的 HTTP 会话。

        参数：
            config (TelemetryConfig)：当前模块对应的强类型配置对象。

        返回：
            None：函数通过副作用完成初始化、输出、持久化或资源管理。
        """
        self.config = config
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Accept": "application/json",
                "Cache-Control": "no-cache",
                "User-Agent": "autodl-gpu-watcher/0.5.0",
            }
        )

    @staticmethod
    def _is_retryable(exc: requests.RequestException) -> bool:
        """判断一次 HTTP 失败是否属于适合立即重试的瞬时故障。

        可重试：读取/连接超时、连接中断、HTTP 429、HTTP 5xx。
        不重试：其他 4xx，例如请求参数错误或权限错误。
        """
        if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
            return True
        if isinstance(exc, requests.HTTPError) and exc.response is not None:
            status = int(exc.response.status_code)
            return status == 429 or status >= 500
        return False

    def collect(self) -> list[GpuSample]:
        """功能：
            拉取 Telemetry GPU 快照，并对瞬时网络故障进行有限重试。

        参数：
            无。

        返回：
            list[GpuSample]：本轮采集得到的在线物理 GPU 样本。

        重试规则：
            - 每次请求最多等待 timeout_seconds；
            - 瞬时故障后等待 retry_delay_seconds；
            - 最多请求 max_attempts 次；
            - 全部失败后才把最后一个异常交给主循环。
        """
        last_error: requests.RequestException | None = None

        for attempt in range(1, self.config.max_attempts + 1):
            try:
                response = self._session.get(
                    self.config.endpoint,
                    params={"_t": int(time.time() * 1000)},  # 时间戳参数防止缓存
                    timeout=self.config.timeout_seconds,
                )
                response.raise_for_status()
                samples = parse_telemetry_payload(
                    response.json(),
                    only_online=self.config.only_online,
                )
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
                    attempt < self.config.max_attempts
                    and self._is_retryable(exc)
                )
                if not can_retry:
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

        # 理论上循环只会通过 return 或 raise 离开；保留防御式分支。
        if last_error is not None:
            raise last_error
        raise RuntimeError("telemetry collection ended without response")
