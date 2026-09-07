"""
配置加载模块 — 解析 config.yaml，提供所有类型安全的配置 dataclass。

设计原则：
    - 所有配置类均为 frozen=True（不可变），确保运行时无意外篡改
    - 使用 slots=True 减少内存开销
    - 路径字段自动相对 config.yaml 所在目录解析，支持 ~ 扩展
    - 提供详细的校验（free_count 范围、aggregation 选项等）
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


# ═══════════════════════════════════════════════════════════════
# 各子配置 dataclass
# ═══════════════════════════════════════════════════════════════

@dataclass(frozen=True, slots=True)
class MonitorConfig:
    """监控循环参数。"""
    poll_seconds: int           # 每轮循环间隔（秒）
    confirmation_seconds: int   # GPU 需连续达标多少秒后才触发
    min_idle_samples: int       # 最少需要多少条连续空闲采样
    max_sample_gap_seconds: int # 连续采样之间允许的最大时间间隔（超出则重置）
    stale_after_seconds: int    # 采样超过此秒数则视为过期
    reset_busy_samples: int     # 连续忙碌多少次后重置 alerted 状态


@dataclass(frozen=True, slots=True)
class IdleThresholds:
    """GPU 空闲判定阈值。

    历史命名（IdleThresholds）保持兼容，但 v0.3.1 起仅以显存为硬条件。
    GPU Util 可关闭，关闭后仅记录、不参与拦截。
    """

    gpu_util_check_enabled: bool  # 是否启用 GPU Util 硬门控
    gpu_util_max_pct: float       # GPU Util 上限（启用时生效）
    memory_free_min_mb: int       # 最低可用显存（MB）
    memory_free_min_ratio: float  # 最低可用显存比例（相对于总显存）


@dataclass(frozen=True, slots=True)
class PlatformConfig:
    """AutoDL 平台采集器配置（Playwright 浏览器自动化）。"""
    page_url: str                 # AutoDL 控制台页面 URL
    api_path_contains: str        # 匹配 API 请求路径中的特征字符串
    api_base_url: str             # API 基础地址
    browser_channel: str          # 浏览器通道（msedge / chrome）
    user_data_dir: Path           # 浏览器用户数据目录（保存登录会话）
    headless: bool                # 是否无头模式
    response_timeout_seconds: int # 等待 API 响应的超时秒数
    max_attempts: int              # 主机列表 API 单轮最多尝试次数
    retry_delay_seconds: float     # 主机列表 API 瞬时失败后的重试等待秒数
    aggregation: str              # 多入口聚合策略：max / min / sum


@dataclass(frozen=True, slots=True)
class TelemetryConfig:
    """Telemetry API 采集器配置（HTTP GET 拉取 GPU 快照）。"""
    endpoint: str              # API 地址
    timeout_seconds: int       # 每次 HTTP 请求超时秒数
    max_attempts: int          # 单轮最多请求次数（含第一次）
    retry_delay_seconds: float # 两次请求之间等待秒数
    only_online: bool          # 是否只接受 host_status=online 的样本


@dataclass(frozen=True, slots=True)
class NotificationConfig:
    """通知配置。"""
    console_enabled: bool   # 是否启用控制台通知
    email_enabled: bool     # 是否启用邮件通知
    subject_prefix: str     # 邮件主题前缀


@dataclass(frozen=True, slots=True)
class AutoStartTarget:
    """一个 AutoDL 固定实例（对应一个 machine_name/入口）。"""
    host: str               # 所属物理主机（如 gpu-203）
    machine_name: str       # 入口名（如 autodl-203-1）
    instance_uuid: str      # 实例 UUID（power_on 时发送）
    start_mode: str         # 开机模式（如 "gpu"）
    priority: int           # 优先级（数字越小优先级越高）
    enabled: bool           # 是否启用


@dataclass(frozen=True, slots=True)
class AutoStartConfig:
    """自动开机配置。"""
    enabled: bool                   # 是否启用自动开机
    default_host: str               # 默认目标主机
    dry_run: bool                   # 默认是否 dry-run（只判定不开机）
    verify_before_start: bool       # 开机前是否二次确认
    recheck_delay_seconds: float    # 二次确认前等待秒数
    post_start_check_seconds: float # 开机后等待秒数，用于验证结果
    max_starts_per_event: int       # 每轮最多开机次数
    targets: tuple[AutoStartTarget, ...]  # 所有可开机的固定实例列表


@dataclass(frozen=True, slots=True)
class UsageTrackingConfig:
    """GPU 占用追踪配置。"""
    enabled: bool             # 是否启用
    interval_seconds: int     # 采集间隔（秒）
    database_path: Path       # SQLite 数据库路径
    export_dir: Path          # CSV 导出目录
    self_user: str            # 当前账号在“查看占用”页面显示的用户标识


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    """运行时文件路径。"""
    state_file: Path  # evaluator 状态持久化文件（JSON）
    log_file: Path    # 监控日志文件


@dataclass(frozen=True, slots=True)
class AppConfig:
    """顶层应用配置，包含所有子配置。"""
    monitor: MonitorConfig
    idle_thresholds: IdleThresholds
    platform: PlatformConfig
    telemetry: TelemetryConfig
    notification: NotificationConfig
    auto_start: AutoStartConfig
    usage_tracking: UsageTrackingConfig
    runtime: RuntimeConfig


def _required(mapping: dict[str, Any], key: str) -> Any:
    """功能：
        从配置字典读取必填字段；字段缺失时立即给出明确错误。

    参数：
        mapping (dict[str, Any])：待读取的配置字典。
        key (str)：需要读取的配置字段名。

    返回：
        Any：指定键对应的配置值。
    """
    if key not in mapping:
        raise KeyError(f"Missing config key: {key}")
    return mapping[key]


def _resolve_path(base_dir: Path, value: str | Path) -> Path:
    """功能：
        将配置中的相对路径解析为相对于配置文件目录的绝对路径。

    参数：
        base_dir (Path)：相对路径解析所依据的配置文件目录。
        value (str | Path)：待规范化、解析或转换的输入值。

    返回：
        Path：规范化后的绝对 Path。
    """
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return (base_dir / path).resolve()


def load_config(path: str | Path = "config.yaml") -> AppConfig:
    """功能：
        读取 YAML 配置，完成类型转换、路径解析与 dataclass 配置对象组装。

    参数：
        path (str | Path)：文件路径、API 相对路径或目标输出路径，具体含义由函数上下文决定。

    返回：
        AppConfig：包含全部子配置的 AppConfig。
    """
    config_path = Path(path).resolve()
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    base_dir = config_path.parent

    monitor = _required(raw, "monitor")
    thresholds = _required(raw, "idle_thresholds")
    platform = _required(raw, "platform")
    telemetry = _required(raw, "telemetry")
    notification = _required(raw, "notification")
    auto_start = raw.get("auto_start", {})
    usage_tracking = raw.get("usage_tracking", {})
    runtime = _required(raw, "runtime")

    aggregation = str(platform.get("aggregation", "max")).lower()
    # v0.3.2 曾使用 any 表示“任一入口 idle>0 即通过”。该语义与 max 在
    # 是否允许开机这一点上完全一致，因此保留 any 作为兼容别名。
    if aggregation == "any":
        aggregation = "max"
    if aggregation not in {"max", "min", "sum"}:
        raise ValueError("platform.aggregation must be one of: max, min, sum")

    targets = tuple(
        AutoStartTarget(
            host=str(item["host"]),
            machine_name=str(item.get("machine_name", "")),
            instance_uuid=str(item["instance_uuid"]),
            start_mode=str(item.get("start_mode", "gpu")),
            priority=int(item.get("priority", 100)),
            enabled=bool(item.get("enabled", True)),
        )
        for item in auto_start.get("targets", [])
    )

    idle_thresholds = IdleThresholds(
        gpu_util_check_enabled=bool(thresholds.get("gpu_util_check_enabled", False)),
        gpu_util_max_pct=float(thresholds.get("gpu_util_max_pct", 100.0)),
        memory_free_min_mb=int(thresholds.get("memory_free_min_mb", 8192)),
        memory_free_min_ratio=float(thresholds.get("memory_free_min_ratio", 0.25)),
    )
    if idle_thresholds.memory_free_min_mb < 0:
        raise ValueError("idle_thresholds.memory_free_min_mb must be >= 0")
    if not 0 <= idle_thresholds.memory_free_min_ratio <= 1:
        raise ValueError("idle_thresholds.memory_free_min_ratio must be in [0, 1]")

    telemetry_timeout = int(telemetry.get("timeout_seconds", 20))
    telemetry_attempts = int(telemetry.get("max_attempts", 2))
    telemetry_retry_delay = float(telemetry.get("retry_delay_seconds", 2.0))
    if telemetry_timeout <= 0:
        raise ValueError("telemetry.timeout_seconds must be > 0")
    if telemetry_attempts < 1:
        raise ValueError("telemetry.max_attempts must be >= 1")
    if telemetry_retry_delay < 0:
        raise ValueError("telemetry.retry_delay_seconds must be >= 0")

    return AppConfig(
        monitor=MonitorConfig(**monitor),
        idle_thresholds=idle_thresholds,
        platform=PlatformConfig(
            page_url=str(platform["page_url"]),
            api_path_contains=str(platform["api_path_contains"]),
            api_base_url=str(platform.get("api_base_url", "https://private.autodl.com")),
            browser_channel=str(platform.get("browser_channel", "msedge")),
            user_data_dir=_resolve_path(base_dir, platform["user_data_dir"]),
            headless=bool(platform.get("headless", True)),
            response_timeout_seconds=int(platform.get("response_timeout_seconds", 20)),
            max_attempts=max(1, int(platform.get("max_attempts", 2))),
            retry_delay_seconds=max(0.0, float(platform.get("retry_delay_seconds", 2.0))),
            aggregation=aggregation,
        ),
        telemetry=TelemetryConfig(
            endpoint=str(telemetry["endpoint"]),
            timeout_seconds=telemetry_timeout,
            max_attempts=telemetry_attempts,
            retry_delay_seconds=telemetry_retry_delay,
            only_online=bool(telemetry.get("only_online", True)),
        ),
        notification=NotificationConfig(**notification),
        auto_start=AutoStartConfig(
            enabled=bool(auto_start.get("enabled", False)),
            default_host=str(auto_start.get("default_host", "gpu-203")),
            dry_run=bool(auto_start.get("dry_run", True)),
            verify_before_start=bool(auto_start.get("verify_before_start", True)),
            recheck_delay_seconds=float(auto_start.get("recheck_delay_seconds", 1.0)),
            post_start_check_seconds=float(auto_start.get("post_start_check_seconds", 5.0)),
            max_starts_per_event=int(auto_start.get("max_starts_per_event", 1)),
            targets=targets,
        ),
        usage_tracking=UsageTrackingConfig(
            enabled=bool(usage_tracking.get("enabled", True)),
            interval_seconds=int(usage_tracking.get("interval_seconds", 60)),
            database_path=_resolve_path(
                base_dir,
                usage_tracking.get(
                    "database_path",
                    "../autodl_watcher_runtime/usage/occupancy.db",
                ),
            ),
            export_dir=_resolve_path(
                base_dir,
                usage_tracking.get(
                    "export_dir",
                    "../autodl_watcher_runtime/usage/exports",
                ),
            ),
            self_user=str(usage_tracking.get("self_user", "")).strip(),
        ),
        runtime=RuntimeConfig(
            state_file=_resolve_path(base_dir, runtime["state_file"]),
            log_file=_resolve_path(base_dir, runtime["log_file"]),
        ),
    )
