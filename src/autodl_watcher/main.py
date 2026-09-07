"""
主循环模块 — 程序的"心脏"。

职责：
    编排整个监控循环的各个阶段，是唯一同时接触所有子模块的顶层协调者。

循环流程（每 poll_seconds 秒一轮）：
    1. 采集 — 从 AutoDL 平台（Playwright）和 Telemetry API（HTTP）拉取数据
    2. 过滤 — 两层门控：平台必须有空位，物理 GPU 显存必须达标
    3. 评估 — evaluator 判断哪些 GPU 已连续满足条件，生成 AvailabilityAlert
    4. 占用快照 — 每隔 interval_seconds 采集一次各入口的 GPU 占用详情，写入 SQLite
    5. 通知 — 如有 EmailNotifier 配置，发送邮件告警
    6. 开机 — AutoStartCoordinator 执行二次确认 + power_on POST
    7. 持久化 — 将 evaluator 状态写入 state.json，异常退出后恢复
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv

from . import __version__
from .autostart import AutoStartCoordinator, format_start_result, select_target
from .cli import normalize_entry, normalize_host, select_cli_targets
from .collectors import (
    PlatformAuthenticationError,
    PlatformBrowserCollector,
    TelemetryApiCollector,
    filter_samples_to_platform_candidates,
)
from .config import load_config
from .evaluator import AvailabilityEvaluator, sample_meets_capacity
from .login import interactive_login
from .notifiers import EmailNotifier
from .state_store import JsonStateStore
from .usage import UsageSqliteLogger, aggregate_gpu_occupants, merge_duplicate_instances


_ANSI_BRIGHT_RED = "\033[91m"
_ANSI_BRIGHT_GREEN = "\033[92m"
_ANSI_RESET = "\033[0m"


def _enable_ansi_colors() -> None:
    """功能：
        在 Windows 控制台中启用 ANSI 转义序列，使开机触发行能够显示颜色。

    参数：
        无。

    返回：
        None：无返回值。
    """
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        stdout_handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(stdout_handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(stdout_handle, mode.value | 0x0004)
    except Exception:
        # 颜色只是辅助显示；启用失败不能影响监控和自动开机主链路。
        return


def _red_terminal_text(text: str) -> str:
    """功能：
        仅在交互式终端中为文本包裹亮红色 ANSI 控制码。

    参数：
        text (str)：待解析或转换的文本。

    返回：
        str：可能带亮红色 ANSI 控制码的文本；非交互输出保持原文。
    """
    if not sys.stdout.isatty():
        return text
    return f"{_ANSI_BRIGHT_RED}{text}{_ANSI_RESET}"


def _green_terminal_text(text: str) -> str:
    """功能：
        仅在交互式终端中为文本包裹亮绿色 ANSI 控制码。

    参数：
        text (str)：待着色的终端文本。

    返回：
        str：可能带亮绿色 ANSI 控制码的文本；非交互输出保持原文。
    """
    if not sys.stdout.isatty():
        return text
    return f"{_ANSI_BRIGHT_GREEN}{text}{_ANSI_RESET}"


@dataclass(frozen=True, slots=True)
class _OwnedInstance:
    """当前账号本人已经占用的一个实例摘要。"""

    machine_name: str
    gpu_index: int
    instance_id: str


def _owned_instances_from_records(
    records,
    self_user: str,
    host: str,
) -> list[_OwnedInstance]:
    """功能：
        从本轮占用明细中筛出当前账号本人正在运行的实例。

    参数：
        records (未显式标注)：平台“查看占用”解析得到的 OccupancyRecord 列表。
        self_user (str)：当前账号在占用页面显示的用户标识。
        host (str)：当前监控的物理主机，例如 `gpu-203`。

    返回：
        list[_OwnedInstance]：按入口和 GPU INDEX 排序的本人活跃实例摘要。
    """
    if not self_user:
        return []

    latest_by_instance = {}
    for item in records:
        if (
            not item.occupied
            or item.host != host
            or item.user.strip() != self_user
            or not item.instance_id
        ):
            continue
        previous = latest_by_instance.get(item.instance_id)
        if previous is None or item.observed_at >= previous.observed_at:
            latest_by_instance[item.instance_id] = item

    return sorted(
        (
            _OwnedInstance(
                machine_name=item.machine_name,
                gpu_index=item.gpu_index,
                instance_id=item.instance_id,
            )
            for item in latest_by_instance.values()
        ),
        key=lambda item: (item.machine_name, item.gpu_index, item.instance_id),
    )


def _owned_instances_from_rows(
    rows,
    self_user: str,
    host: str,
) -> list[_OwnedInstance]:
    """功能：
        从 SQLite 的 current_instances 行恢复本人当前占用状态。

    参数：
        rows (未显式标注)：UsageSqliteLogger.current_instances() 返回的字典行。
        self_user (str)：当前账号在占用页面显示的用户标识。
        host (str)：当前监控的物理主机，例如 `gpu-203`。

    返回：
        list[_OwnedInstance]：恢复出的本人活跃实例摘要。
    """
    if not self_user:
        return []

    result = []
    for row in rows:
        if str(row.get("host", "")) != host:
            continue
        if str(row.get("user", "")).strip() != self_user:
            continue
        result.append(
            _OwnedInstance(
                machine_name=str(row.get("machine_name", "")),
                gpu_index=int(row.get("gpu_index", -1)),
                instance_id=str(row.get("instance_id", "")),
            )
        )
    return sorted(
        result,
        key=lambda item: (item.machine_name, item.gpu_index, item.instance_id),
    )


def _format_owned_entry(instances: list[_OwnedInstance]) -> str:
    """功能：
        格式化本人已占用入口，例如 `已占用203-1`。

    参数：
        instances (list[_OwnedInstance])：本人当前活跃实例列表。

    返回：
        str：本人已占用入口的紧凑文本。
    """
    names = []
    for item in instances:
        for name in item.machine_name.split("|"):
            short_name = name.removeprefix("autodl-")
            if short_name and short_name not in names:
                names.append(short_name)
    return "已占用" + ",".join(names) if names else "已开机"


def _format_owned_indices(
    instances: list[_OwnedInstance],
    samples,
) -> str:
    """功能：
        按本人实际占用的 GPU INDEX 显示当前剩余显存。

    参数：
        instances (list[_OwnedInstance])：本人当前活跃实例列表。
        samples (未显式标注)：当前物理主机的全部 GpuSample 列表。

    返回：
        str：例如 `#0(20342MB)`；Telemetry 缺失时退化为 `#0`。
    """
    sample_by_index = {item.gpu_index: item for item in samples}
    parts = []
    for gpu_index in sorted({item.gpu_index for item in instances if item.gpu_index >= 0}):
        sample = sample_by_index.get(gpu_index)
        if sample is None:
            parts.append(f"#{gpu_index}")
        else:
            parts.append(f"#{gpu_index}({sample.memory_free_mb}MB)")
    return ",".join(parts) or "无"


def _setup_logging(log_file: str) -> logging.Logger:
    """功能：
        配置滚动日志文件，最大 2MB，保留 3 个备份。

    参数：
        log_file (str)：滚动日志文件路径字符串。

    返回：
        logging.Logger：配置完成的项目 Logger。
    """
    logger = logging.getLogger("autodl_watcher")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger  # 避免重复添加 handler

    handler = RotatingFileHandler(
        log_file,
        maxBytes=2 * 1024 * 1024,  # 单文件上限 2MB
        backupCount=3,              # 保留 3 个备份
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    logger.addHandler(handler)
    return logger


def _build_parser(default_host: str) -> argparse.ArgumentParser:
    """功能：
        构建 CLI 参数解析器。

    参数：
        default_host (str)：CLI 未显式指定时使用的默认物理主机。

    返回：
        argparse.ArgumentParser：可直接解析命令行参数的 ArgumentParser。

    补充说明：
        主要参数：
            --host     目标物理服务器（203 / gpu-203）
            --entry    入口选择策略（auto / 1 / 2 / 203-2）
            --dry-run  仅判定不开机（覆盖配置）
            --live     强制真实开机（覆盖配置）
    """
    parser = argparse.ArgumentParser(
        description="监控指定物理服务器，并按实时入口空位自动启动对应固定实例。"
    )
    parser.add_argument(
        "--host",
        default=default_host,
        help="目标物理服务器。可写 203、gpu-203；默认 203。",
    )
    parser.add_argument(
        "--entry",
        default="auto",
        help="入口选择。默认 auto；也可强制写 1、2、203-2。",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="只判定，不真正开机。")
    mode.add_argument("--live", action="store_true", help="覆盖配置，真实发送开机请求。")
    return parser


def _format_entry_slots(platform_host) -> str:
    """功能：
        格式化各入口的 GPU ID 空位，用于控制台展示，如 '203-1=0/2,203-2=1/2'。

    参数：
        platform_host (未显式标注)：一个物理主机在 AutoDL 平台上的聚合状态；可为 `None`。

    返回：
        str：用于终端显示的入口空位文本。
    """
    if platform_host is None or not platform_host.source_slots:
        return "无"
    return ",".join(
        f"{name.removeprefix('autodl-')}={idle}/{total}"
        for name, idle, total in platform_host.source_slots
    )


def _ready_samples(samples, thresholds):
    """功能：
        筛选出满足显存容量条件的 GPU 样本，按 INDEX 排序。

    参数：
        samples (未显式标注)：物理 GPU 采样对象列表。
        thresholds (未显式标注)：显存容量与可选 GPU 利用率阈值配置。

    返回：
        未显式标注：按 GPU INDEX 排序的显存达标样本列表。
    """
    return [
        sample
        for sample in sorted(samples, key=lambda item: item.gpu_index)
        if sample_meets_capacity(sample, thresholds)
    ]


def _format_ready_indices(samples, thresholds) -> str:
    """功能：
        格式化达标 GPU 索引，用于控制台展示，如 '#0(21728MB),#1(21736MB)'。

    参数：
        samples (未显式标注)：物理 GPU 采样对象列表。
        thresholds (未显式标注)：显存容量与可选 GPU 利用率阈值配置。

    返回：
        str：显存达标 GPU INDEX 及剩余显存的紧凑文本。
    """
    ready = _ready_samples(samples, thresholds)
    if not ready:
        return "无"
    return ",".join(
        f"#{sample.gpu_index}({sample.memory_free_mb}MB)" for sample in ready
    )



_STATE_RESTORE_MAX_AGE_SECONDS = 300
_OWNERSHIP_HINT_MAX_AGE_SECONDS = 180


def _parse_local_iso(value: object) -> datetime | None:
    """解析 state/SQLite 中的本地 ISO 时间；无效值返回 None。"""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _persisted_state_is_fresh(
    persisted: dict,
    now: datetime,
    max_age_seconds: int = _STATE_RESTORE_MAX_AGE_SECONDS,
) -> bool:
    """仅恢复很近的 evaluator 状态，避免数小时/数天前的 alerted 永久锁死。"""
    saved_at = _parse_local_iso(persisted.get("saved_at"))
    if saved_at is None:
        return False
    age = (now - saved_at).total_seconds()
    return -5 <= age <= max_age_seconds


def _fresh_owned_instances_from_rows(
    rows,
    self_user: str,
    host: str,
    now: datetime,
    max_age_seconds: int = _OWNERSHIP_HINT_MAX_AGE_SECONDS,
) -> list[_OwnedInstance]:
    """SQLite 只能作为短时“启动保护提示”，不能直接证明本人现在仍在占用。"""
    fresh_rows = []
    for row in rows:
        if str(row.get("host", "")) != host:
            continue
        if str(row.get("user", "")).strip() != self_user:
            continue
        last_seen = _parse_local_iso(row.get("last_seen_at"))
        if last_seen is None:
            continue
        age = (now - last_seen).total_seconds()
        if -5 <= age <= max_age_seconds:
            fresh_rows.append(row)
    return _owned_instances_from_rows(fresh_rows, self_user, host)


def main() -> None:
    """功能：
        主入口：初始化各组件后进入无限监控循环。

    参数：
        无。

    返回：
        None：函数通过副作用完成初始化、输出、持久化或资源管理。

    补充说明：
        整个函数分为三个阶段：
        1. 初始化 — 加载配置、解析 CLI、创建采集器/评估器/日志器
        2. 启动信息 — 打印当前配置摘要
        3. 主循环 — while True 周期性执行采集→评估→开机→持久化
    """
    # ── 阶段 0：加载环境变量与配置 ──
    _enable_ansi_colors()
    load_dotenv()
    config_path = Path("config.yaml").resolve()
    config = load_config(config_path)
    parser = _build_parser(config.auto_start.default_host)
    args = parser.parse_args()

    # ── 解析并归一化 CLI 参数 ──
    try:
        selected_host = normalize_host(args.host)       # "203" → "gpu-203"
        selected_entry = normalize_entry(args.entry, selected_host)  # "auto" / "2" / "203-2"
        selected_targets = select_cli_targets(
            config.auto_start,
            selected_host,
            selected_entry,
        )
    except ValueError as exc:
        parser.error(str(exc))

    # ── dry-run 模式优先级：CLI 参数 > 配置文件 ──
    dry_run = config.auto_start.dry_run
    if args.dry_run:
        dry_run = True
    elif args.live:
        dry_run = False

    # 用选中的 targets 替换配置中的全量 targets
    selected_auto_start = replace(
        config.auto_start,
        dry_run=dry_run,
        targets=selected_targets,
    )

    # ── 初始化日志系统 ──
    config.runtime.log_file.parent.mkdir(parents=True, exist_ok=True)
    logger = _setup_logging(str(config.runtime.log_file))

    # ── 初始化 evaluator 并尝试恢复上次状态 ──
    evaluator = AvailabilityEvaluator(config.monitor, config.idle_thresholds)
    state_store = JsonStateStore(config.runtime.state_file)
    persisted = state_store.load()
    target_signature = ",".join(item.machine_name for item in selected_targets)
    # capacity_fingerprint 是配置的哈希摘要，用于检测配置是否变更
    capacity_fingerprint = (
        f"v5|host={selected_host}|targets={target_signature}|"
        f"util={config.idle_thresholds.gpu_util_check_enabled}:"
        f"{config.idle_thresholds.gpu_util_max_pct}|"
        f"free={config.idle_thresholds.memory_free_min_mb}:"
        f"{config.idle_thresholds.memory_free_min_ratio}|"
        f"confirm={config.monitor.confirmation_seconds}:"
        f"{config.monitor.min_idle_samples}|dry_run={dry_run}"
    )
    state_now = datetime.now()
    state_fresh = _persisted_state_is_fresh(persisted, state_now)
    if (
        persisted.get("capacity_fingerprint") == capacity_fingerprint
        and state_fresh
    ):
        # 只恢复 5 分钟内的 evaluator 状态。旧版会把几天前的 alerted=True
        # 一并恢复，造成“条件明明满足但永远不再开机”。
        evaluator.import_state(persisted.get("evaluator", {}))
    elif persisted:
        logger.info(
            "skip stale evaluator state saved_at=%s fingerprint_match=%s",
            persisted.get("saved_at"),
            persisted.get("capacity_fingerprint") == capacity_fingerprint,
        )

    # ── 创建各采集器和通知器 ──
    email_notifier = None
    if config.notification.email_enabled:
        email_notifier = EmailNotifier(config.notification.subject_prefix)

    platform_collector = PlatformBrowserCollector(config.platform)   # 浏览器 → AutoDL 平台
    telemetry_collector = TelemetryApiCollector(config.telemetry)    # HTTP   → Telemetry API
    usage_logger = UsageSqliteLogger(config.usage_tracking.database_path)  # SQLite 占用日志
    self_user = config.usage_tracking.self_user.strip()
    current_rows = usage_logger.current_instances()
    # v0.5.0：SQLite current_instances 是“上次看见”，不是实时真值。
    # 仅把 3 分钟内的记录当作短时启动保护提示，绝不直接显示绿色“已开机”。
    restored_owned_hint = _fresh_owned_instances_from_rows(
        current_rows,
        self_user,
        selected_host,
        datetime.now(),
    )
    owned_instances: list[_OwnedInstance] = []
    self_occupancy_known = False
    startup_ownership_hold_until = (
        time.monotonic() + _OWNERSHIP_HINT_MAX_AGE_SECONDS
        if restored_owned_hint
        else 0.0
    )
    startup_hint_rearmed = False
    usage_targets = sorted(
        (
            item
            for item in config.auto_start.targets
            if item.enabled and item.host == selected_host
        ),
        key=lambda item: (item.priority, item.machine_name, item.instance_uuid),
    )
    next_usage_capture = 0.0  # 下次采集占用快照的时间戳（monotonic）

    # ── 创建自动开机协调器 ──
    starter = AutoStartCoordinator(
        selected_auto_start,
        config.monitor,
        config.idle_thresholds,
        platform_collector,
        telemetry_collector,
    )

    # ── 打印启动摘要信息 ──
    auto_mode = "关闭"
    if selected_auto_start.enabled:
        auto_mode = "DRY-RUN" if selected_auto_start.dry_run else "真实开机"

    entry_mode = (
        "自动选择[" + ", ".join(item.machine_name for item in selected_targets) + "]"
        if selected_entry is None
        else f"固定 {selected_targets[0].machine_name}"
    )

    print(f"AutoDL GPU Watcher v{__version__}")
    print(f"代码路径：{Path(__file__).resolve().parent}")
    print(f"配置路径：{config_path}")
    print(f"目标主机：{selected_host}")
    print(f"入口策略：{entry_mode}")
    print(f"模式：{auto_mode}；每 {config.monitor.poll_seconds} 秒采样；Ctrl+C 安全停止。")
    print(
        "显存门槛：可用显存 >= "
        f"max({config.idle_thresholds.memory_free_min_mb} MB, "
        f"总显存×{config.idle_thresholds.memory_free_min_ratio:.0%})；"
        "GPU Util 仅记录。"
    )
    print(
        "Telemetry容错：单次超时 "
        f"{config.telemetry.timeout_seconds} 秒；瞬时失败后等待 "
        f"{config.telemetry.retry_delay_seconds:g} 秒重试；"
        f"每轮最多 {config.telemetry.max_attempts} 次请求。"
    )
    print(f"监控日志：{config.runtime.log_file}")
    if config.usage_tracking.enabled:
        usage_entry_text = ", ".join(item.machine_name for item in usage_targets) or "无"
        print(
            "占用日志：每 "
            f"{config.usage_tracking.interval_seconds} 秒轮询全部入口[{usage_entry_text}]；"
            f"SQLite主库={config.usage_tracking.database_path}；"
            f"CSV导出目录={config.usage_tracking.export_dir}；"
            f"本人用户={self_user or '未配置'}"
        )
    if restored_owned_hint:
        print(
            "启动保护：SQLite 中存在近期本人占用记录，但仅视为待确认提示；"
            "不会直接显示‘已开机’。"
        )

    # ── 阶段 1：无限监控循环 ──
    try:
        while True:
            cycle_start = time.monotonic()  # 本轮开始时间（用于控制循环间隔）

            try:
                # ── Step 1: 采集两层数据 ──
                # 1a. 从 AutoDL 控制台采集平台级 GPU ID 空位（Playwright 控制浏览器）
                all_platform_hosts = platform_collector.collect()
                platform_hosts = [
                    item for item in all_platform_hosts if item.host == selected_host
                ]
                # 1b. 从 Telemetry API 采集物理 GPU 显存快照（HTTP GET）
                all_gpu_samples = telemetry_collector.collect()
                selected_gpu_samples = [
                    item for item in all_gpu_samples if item.host == selected_host
                ]
                # 1c. 两层门控过滤：平台 free>0 + 已在平台可见 → 才进入评估
                gpu_samples, _blocked_hosts, _unauthorized_hosts = (
                    filter_samples_to_platform_candidates(
                        selected_gpu_samples,
                        platform_hosts,
                    )
                )

                # ── Step 2: 评估 GPU 容量连续性 ──
                now = datetime.now()
                # 如果启动时只有“近期 SQLite 提示”但 3 分钟始终无法得到占用确认，
                # 不允许旧 alerted 永久锁死自动开机；保护期到期后重新武装一次。
                if (
                    restored_owned_hint
                    and not self_occupancy_known
                    and not startup_hint_rearmed
                    and time.monotonic() >= startup_ownership_hold_until
                ):
                    reset_count = evaluator.rearm_host(selected_host)
                    startup_hint_rearmed = True
                    logger.warning(
                        "startup ownership hint expired; evaluator rearmed host=%s reset_alerted=%d",
                        selected_host,
                        reset_count,
                    )

                # evaluator 内部维护 idle_samples 队列，判断哪些 GPU 已连续达标
                trigger_events = evaluator.evaluate(platform_hosts, gpu_samples, now)

                # ── Step 3: 构建控制台日志所需的各种摘要信息 ──
                platform_host = platform_hosts[0] if platform_hosts else None
                entry_slots = _format_entry_slots(platform_host)              # 各入口空闲 GPU ID 数
                ready_samples = _ready_samples(gpu_samples, config.idle_thresholds)  # 显存达标的 GPU
                ready_indices = _format_ready_indices(gpu_samples, config.idle_thresholds)
                platform_text = (
                    "不可见"
                    if platform_host is None
                    else f"{platform_host.free_count}/{platform_host.total_count}"
                )

                # 只有“本进程中从占用页面明确确认”的实例才算本人已开机。
                # SQLite 历史记录绝不直接进入绿色状态。
                self_active = self_occupancy_known and bool(owned_instances)
                ownership_hold = bool(
                    restored_owned_hint
                    and not self_occupancy_known
                    and time.monotonic() < startup_ownership_hold_until
                )
                if self_active or ownership_hold:
                    trigger_events = []

                # 根据实时空位，预判会被选中的入口。
                planned_target = None
                if platform_host is not None and not self_active and not ownership_hold:
                    planned_target = select_target(
                        selected_auto_start,
                        selected_host,
                        platform_slots=platform_host.source_slots,
                    )

                start_ready = bool(
                    platform_host is not None
                    and platform_host.free_count > 0       # 平台有空位
                    and ready_samples                       # 物理显存达标
                    and planned_target is not None          # 有可匹配的固定实例
                )

                if self_active:
                    # 绿色“已开机”只来自本进程实时占用快照的正向证据。
                    ready_indices = _format_owned_indices(
                        owned_instances,
                        selected_gpu_samples,
                    )
                    planned_text = _format_owned_entry(owned_instances)
                    start_ready_text = "已开机"
                    action_text = "已开机，继续监控"
                elif ownership_hold:
                    planned_text = "历史占用待确认"
                    start_ready_text = "待确认"
                    action_text = "暂缓开机，等待占用确认"
                else:
                    start_ready_text = "是" if start_ready else "否"
                    if trigger_events and selected_auto_start.enabled:
                        action_text = "触发自动开机"
                    elif start_ready:
                        action_text = "条件已处理，继续监控"
                    else:
                        action_text = "继续监控"

                    if planned_target is not None:
                        planned_text = planned_target.machine_name.removeprefix("autodl-")
                    elif platform_host is None:
                        planned_text = "不可见"
                    elif platform_host.free_count <= 0:
                        planned_text = "等待平台空位"
                    else:
                        planned_text = "无匹配实例"

                # ── Step 4: 控制台输出本轮状态摘要 ──
                status_line = (
                    f"[{now:%H:%M:%S}] {selected_host} | "
                    f"平台[{entry_slots}] | "
                    f"物理INDEX={len(selected_gpu_samples)} | "
                    f"显存达标={ready_indices} | "
                    f"预选入口={planned_text} | "
                    f"开机达标={start_ready_text} | "
                    f"动作={action_text}"
                )
                if self_active:
                    status_line = _green_terminal_text(status_line)
                elif (
                    trigger_events
                    and selected_auto_start.enabled
                    and not selected_auto_start.dry_run
                ):
                    status_line = _red_terminal_text(status_line)
                print(status_line, flush=True)
                # 结构化日志（用于事后分析）
                logger.info(
                    "target_host=%s entry_mode=%s platform_gpu_ids=%s entries=%s physical_indices=%d ready_indices=%s planned_entry=%s start_ready=%s self_active=%s self_user=%s trigger_events=%d",
                    selected_host,
                    "auto" if selected_entry is None else selected_targets[0].machine_name,
                    platform_text,
                    entry_slots,
                    len(selected_gpu_samples),
                    ready_indices,
                    planned_target.machine_name if planned_target else "none",
                    start_ready_text,
                    self_active,
                    self_user,
                    len(trigger_events),
                )

                # ── Step 5: GPU 占用快照（每 interval_seconds 秒一次） ──
                if (
                    config.usage_tracking.enabled
                    and time.monotonic() >= next_usage_capture
                    and usage_targets
                ):
                    raw_occupancy = []
                    entry_summaries: list[str] = []
                    failed_entries: list[str] = []
                    # 逐个入口点击"查看占用"按钮，解析弹窗中的 GPU 占用明细
                    for usage_target in usage_targets:
                        entry_name = usage_target.machine_name
                        short_name = entry_name.removeprefix("autodl-")
                        try:
                            entry_records = platform_collector.collect_occupancy(entry_name)
                            raw_occupancy.extend(entry_records)
                            occupied_text = ",".join(
                                f"#{item.gpu_index}:{item.user or '-'}"
                                for item in entry_records
                                if item.occupied
                            ) or "无"
                            entry_summaries.append(f"{short_name}[{occupied_text}]")
                        except Exception as entry_exc:
                            failed_entries.append(f"{short_name}:{entry_exc}")
                            entry_summaries.append(f"{short_name}[采集失败]")
                            logger.exception(
                                "occupancy entry capture failed host=%s entry=%s",
                                selected_host,
                                entry_name,
                            )

                    # 将本次采集的所有入口占用记录写入 SQLite
                    try:
                        complete_occupancy_snapshot = not failed_entries
                        usage_events = usage_logger.record(
                            raw_occupancy,
                            complete_snapshot=complete_occupancy_snapshot,
                            snapshot_hosts={selected_host},
                        )
                        instance_records = merge_duplicate_instances(raw_occupancy)
                        gpu_rows = aggregate_gpu_occupants(raw_occupancy)

                        captured_owned = _owned_instances_from_records(
                            raw_occupancy,
                            self_user,
                            selected_host,
                        )
                        previous_active = self_occupancy_known and bool(owned_instances)
                        had_startup_hint = bool(restored_owned_hint)

                        if captured_owned:
                            # 正向证据即使来自部分入口也可信：明确看到了本人实例。
                            owned_instances = captured_owned
                            self_occupancy_known = True
                            restored_owned_hint = []
                            startup_ownership_hold_until = 0.0
                            startup_hint_rearmed = False
                        elif complete_occupancy_snapshot:
                            # 只有“所有入口都成功且均未看到本人”才确认本人不再占用。
                            owned_instances = []
                            self_occupancy_known = True
                            restored_owned_hint = []
                            startup_ownership_hold_until = 0.0
                            startup_hint_rearmed = False

                            if previous_active or had_startup_hint:
                                rearmed_gpu_count = evaluator.rearm_host(selected_host)
                                rearm_line = (
                                    f"[{datetime.now():%H:%M:%S}] 本人占用已确认结束 | "
                                    f"主机={selected_host} | "
                                    f"已清除alerted={rearmed_gpu_count} | "
                                    "自动开机=重新武装"
                                )
                                print(rearm_line, flush=True)
                                logger.warning(
                                    "self occupancy ended/cleared; evaluator rearmed host=%s reset_alerted=%d user=%s",
                                    selected_host,
                                    rearmed_gpu_count,
                                    self_user,
                                )
                        instance_text = ",".join(
                            f"#{item.gpu_index}:{item.user or '-'}({item.instance_id})"
                            for item in instance_records
                        ) or "无"
                        gpu_text = ",".join(
                            f"#{row['gpu_index']}:{row['occupant_count']}人"
                            for row in gpu_rows
                        ) or "无"
                        summary_text = "; ".join(entry_summaries) or "无数据"
                        occupancy_line = (
                            f"[{datetime.now():%H:%M:%S}] 占用快照 | "
                            f"{summary_text} | "
                            f"实例保留={instance_text} | "
                            f"物理GPU并发={gpu_text} | "
                            f"新增事件={len(usage_events)}"
                        )
                        if self_occupancy_known and owned_instances:
                            occupancy_line = _green_terminal_text(occupancy_line)
                        print(occupancy_line, flush=True)
                        logger.info(
                            "occupancy_snapshot host=%s entries=%s raw_records=%d instances=%d gpu_rows=%d events=%d failures=%s",
                            selected_host,
                            ",".join(item.machine_name for item in usage_targets),
                            len(raw_occupancy),
                            len(instance_records),
                            len(gpu_rows),
                            len(usage_events),
                            " | ".join(failed_entries),
                        )
                    except Exception as usage_exc:
                        print(
                            f"[{datetime.now():%H:%M:%S}] 占用日志写入失败：{usage_exc}",
                            flush=True,
                        )
                        logger.exception("occupancy log write failed")
                    finally:
                        # 无论写入成功与否，重置计时器
                        next_usage_capture = (
                            time.monotonic() + config.usage_tracking.interval_seconds
                        )

                # 若本轮占用快照确认本人已经开机，则取消本轮残留触发事件，
                # 防止同一账号在另一入口重复开机。
                if self_occupancy_known and owned_instances:
                    trigger_events = []

                # ── Step 6: 发送邮件通知（若配置了 SMTP） ──
                if email_notifier is not None:
                    for event in trigger_events:
                        email_notifier.send(event)

                # ── Step 7: 执行自动开机（最多 max_starts_per_event 次） ──
                if selected_auto_start.enabled:
                    for event in trigger_events[: selected_auto_start.max_starts_per_event]:
                        result = starter.attempt(event)
                        result_text = format_start_result(result)
                        if result.status == "request_accepted":
                            # 红色表示“正在触发”，绿色表示“平台已受理成功”。
                            result_text = _green_terminal_text(result_text)
                            # power_on=Success 只表示“请求被平台受理”，不等于实例已经
                            # 真正占用 GPU。必须等占用快照看到 self_user 后才进入绿色状态。
                            logger.info(
                                "power_on accepted; waiting occupancy confirmation host=%s machine=%s instance=%s",
                                result.host,
                                result.machine_name,
                                result.instance_uuid,
                            )
                        print(result_text, flush=True)
                        logger.info(
                            "auto_start status=%s host=%s machine=%s instance=%s before=%s/%s after=%s/%s code=%s msg=%s",
                            result.status,
                            result.host,
                            result.machine_name,
                            result.instance_uuid,
                            result.platform_free_before,
                            result.platform_total_before,
                            result.platform_free_after,
                            result.platform_total_after,
                            result.api_code,
                            result.api_msg,
                        )

                # ── Step 8: 持久化 evaluator 状态 ──
                state_store.save(
                    {
                        "saved_at": now.isoformat(),
                        "capacity_fingerprint": capacity_fingerprint,
                        "evaluator": evaluator.export_state(),
                    }
                )
            except PlatformAuthenticationError as exc:
                # v0.5.0：会话过期时不再每 10 秒刷 /login 错误。关闭 Playwright，
                # 自动弹出普通 Edge；用户完成验证码后，当前监控进程直接恢复。
                now = datetime.now()
                print(f"[{now:%H:%M:%S}] 登录会话失效：{exc}", flush=True)
                logger.warning("platform authentication expired: %s", exc)
                platform_collector.close()
                try:
                    interactive_login(config, reason="expired")
                    print(
                        f"[{datetime.now():%H:%M:%S}] 登录恢复完成，自动继续监控。",
                        flush=True,
                    )
                    self_occupancy_known = False
                    owned_instances = []
                    restored_owned_hint = []
                    startup_ownership_hold_until = 0.0
                    startup_hint_rearmed = False
                    next_usage_capture = 0.0
                except Exception as login_exc:
                    print(
                        f"[{datetime.now():%H:%M:%S}] 登录恢复失败：{login_exc}",
                        flush=True,
                    )
                    logger.exception("interactive login recovery failed")
            except Exception as exc:
                # 本轮任意环节抛异常 → 打日志，不中断循环
                now = datetime.now()
                print(f"[{now:%H:%M:%S}] 本轮失败：{exc}", flush=True)
                logger.exception("collection/auto-start cycle failed")

            # 控制循环间隔：睡眠到距离上次开始正好 poll_seconds
            elapsed = time.monotonic() - cycle_start
            time.sleep(max(0.0, config.monitor.poll_seconds - elapsed))
    except KeyboardInterrupt:
        # Ctrl+C → 保存状态后优雅退出
        print("\n收到 Ctrl+C，正在保存状态并退出……")
        state_store.save(
            {
                "saved_at": datetime.now().isoformat(),
                "capacity_fingerprint": capacity_fingerprint,
                "evaluator": evaluator.export_state(),
            }
        )
    finally:
        platform_collector.close()
        print("监控已安全停止。")


if __name__ == "__main__":
    main()
