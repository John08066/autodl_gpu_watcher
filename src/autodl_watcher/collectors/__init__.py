"""
数据采集器 — 从两个数据源采集原始数据。

数据源 1：PlatformBrowserCollector (platform.py)
    - 使用 Playwright 控制 Edge/Chrome 浏览器
    - 打开 AutoDL 控制台，拦截 /api/v2/machine/list 的响应
    - 从中提取平台 GPU ID 空位和 Authorization 令牌
    - 也可用于发送 POST power_on 请求和采集 GPU 占用详情

数据源 2：TelemetryApiCollector (telemetry.py)
    - 通过 HTTP GET 请求自建的 Telemetry API
    - 获取物理 GPU 的实时显存/利用率快照

两层数据在 main.py 中通过 filter_samples_to_platform_candidates 交叉验证。
"""
from .platform import (
    PlatformAuthenticationError,
    PlatformBrowserCollector,
    PlatformTransientError,
    parse_platform_payload,
)
from .telemetry import (
    TelemetryApiCollector,
    filter_samples_to_authorized_hosts,
    filter_samples_to_platform_candidates,
    parse_telemetry_payload,
)

__all__ = [
    "PlatformAuthenticationError",
    "PlatformBrowserCollector",
    "PlatformTransientError",
    "TelemetryApiCollector",
    "filter_samples_to_authorized_hosts",
    "filter_samples_to_platform_candidates",
    "parse_platform_payload",
    "parse_telemetry_payload",
]
