from __future__ import annotations  # 主循环模块 — 程序的"心脏"。

import argparse
import logging
import math
import os
import re
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
    PlatformTransientError,
    TelemetryApiCollector,
    filter_samples_to_platform_candidates,
)
from .config import AutoStartTarget, load_config
from .evaluator import AvailabilityEvaluator, sample_meets_capacity
from .login import interactive_login
from .notifiers import EmailNotifier
from .state_store import JsonStateStore
from .session import emit_session
from .usage import UsageSqliteLogger, aggregate_gpu_occupants, merge_duplicate_instances


_ANSI_BRIGHT_RED = "\033[91m"
_ANSI_BRIGHT_GREEN = "\033[92m"
_ANSI_RESET = "\033[0m"


def _enable_ansi_colors() -> None:  # 在 Windows 控制台中启用 ANSI 转义序列，使开机触发行能够显示颜色。
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
        return  # 颜色只是辅助显示；启用失败不能影响监控和自动开机主链路。


def _red_terminal_text(text: str) -> str:  # 仅在交互式终端中为文本包裹亮红色 ANSI 控制码。
    if not sys.stdout.isatty():
        return text
    return f"{_ANSI_BRIGHT_RED}{text}{_ANSI_RESET}"


def _green_terminal_text(text: str) -> str:  # 仅在交互式终端中为文本包裹亮绿色 ANSI 控制码。
    if not sys.stdout.isatty():
        return text
    return f"{_ANSI_BRIGHT_GREEN}{text}{_ANSI_RESET}"


_INSTANCE_UUID = re.compile(r"[0-9a-fA-F]{10}-[0-9a-fA-F]{8}")
_OCCUPANCY_ID_LINE = re.compile(
    r"([0-9a-fA-F]{10}-[0-9a-fA-F]{8})(?:[ \t]*(?:\((.*)\)|（(.*)）))?")


def _instance_bindings_from_cell(value: str) -> tuple[list[tuple[str, str]], bool]:  # 名字仅用于显示，归属仍核验完整 UUID。
    if not isinstance(value, str):
        return [], False
    bindings: list[tuple[str, str]] = []
    complete = True
    for line in value.splitlines():
        if not line.strip():
            continue
        match = _OCCUPANCY_ID_LINE.fullmatch(line.strip())
        if match is None:
            complete = False  # 畸形或截断 ID 不能证明本人缺席。
        else:
            name = (match.group(2) or match.group(3) or "").strip()
            bindings.append((match.group(1).lower(), "" if name in {"-", "—"} else name))
    return bindings, complete and bool(bindings)


def _occupancy_display_name(item) -> str:  # 昵称与 user 身份字段分开，缺名时不把 UUID 填回界面。
    return item.display_name or "用户名未知"


@dataclass(frozen=True, slots=True)
class _OwnedInstance:  # 当前账号本人已经占用的一个实例摘要。

    machine_name: str
    gpu_index: int
    instance_id: str


def _owned_instances_from_records(
    records,
    self_user: str,
    host: str,
) -> list[_OwnedInstance]:  # 从本轮占用明细中筛出当前账号本人正在运行的实例。
    if not self_user:
        return []

    latest_by_instance = {}  # 同一实例可能出现在多个入口，按实例 ID 保留最新观察。
    for item in records:
        if (
            not item.occupied or item.host != host or item.user.strip() != self_user or not item.instance_id
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


def _advance_absence_confirmation(
    *,
    previous_known: bool,
    previous_active: bool,
    complete_snapshot: bool,
    captured_owned: bool,
    current_streak: int,
    required: int,
) -> tuple[int, bool, bool]:  # 推进“本人已消失”的连续确认状态。
    required = max(1, int(required))
    if captured_owned:
        return 0, False, False
    if not complete_snapshot:  # 缺入口或采集失败不能作为本人下机的证据，并打断连续确认。
        return 0, False, False
    if previous_known and not previous_active:
        return max(current_streak, required), True, False
    streak = current_streak + 1
    confirmed = streak >= required
    return streak, confirmed, not confirmed  # 返回连续次数、是否确认缺席、是否需要加快复核。

def _format_owned_entry(instances: list[_OwnedInstance]) -> str:  # 格式化本人已占用入口，例如 `已占用203-1`。
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
) -> str:  # 按本人实际占用的 GPU INDEX 显示当前剩余显存。
    sample_by_index = {item.gpu_index: item for item in samples}
    parts = []
    for gpu_index in sorted({item.gpu_index for item in instances if item.gpu_index >= 0}):
        sample = sample_by_index.get(gpu_index)
        if sample is None:
            parts.append(f"#{gpu_index}")
        else:
            parts.append(f"#{gpu_index}({sample.memory_free_mb}MB)")
    return ",".join(parts) or "无"


def _setup_logging(log_file: str) -> logging.Logger:  # 配置滚动日志文件，最大 2MB，保留 3 个备份。
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


def _build_parser(default_host: str) -> argparse.ArgumentParser:  # 构建 CLI 参数解析器。
    parser = argparse.ArgumentParser( description="监控指定物理服务器，并按实时入口空位自动启动对应固定实例。" )
    parser.add_argument( "--host", default=default_host, help="目标物理服务器。可写 203、gpu-203；默认 203。", )
    parser.add_argument( "--entry", default="auto", help="入口选择。默认 auto；也可强制写 1、2、203-2。", )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="只判定，不真正开机。")
    mode.add_argument("--live", action="store_true", help="覆盖配置，真实发送开机请求。")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    parser.add_argument("--user", help="占用列表中的本人用户名")
    parser.add_argument("--poll-seconds", type=positive_seconds, help="采样间隔（秒）")
    parser.add_argument("--usage-seconds", type=positive_seconds, help="占用采集间隔（秒）")
    parser.add_argument("--stop-file", type=Path, help="UI 的安全停止信号文件")
    parser.add_argument("--max-cycles", type=int, default=0, help="限定轮数，0 为持续运行")
    parser.add_argument("--no-login", action="store_true", help="登录失效时退出，由 UI 完成登录")
    parser.add_argument("--convert-no-gpu", action="store_true", help="目标达标时关闭当前账号全部无卡实例，再有卡开机；需固定 --entry 和 --live。")
    parser.add_argument("--no-usage-report", action="store_true", help="关闭历史占用统计和报表写入，仍保留开机必需的占用核验。")
    parser.add_argument("--runtime-dir", type=Path, help="独立运行数据目录")
    return parser


def positive_seconds(value: str) -> float:  # 拒绝零、负数和非有限数。
    try:
        seconds = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("间隔必须为正数") from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("间隔必须为有限正数")
    return seconds


def apply_monitor_options(config, args):  # UI 与 CLI 共用配置转换。
    poll = args.poll_seconds if args.poll_seconds is not None else config.monitor.poll_seconds
    usage = args.usage_seconds if args.usage_seconds is not None else config.usage_tracking.interval_seconds
    positive_seconds(str(poll))
    positive_seconds(str(usage))
    config = replace(config,  # 配置对象不可变；用副本承接本次 UI/CLI 覆盖值。
        monitor=replace(config.monitor, poll_seconds=poll,
                        max_sample_gap_seconds=max(config.monitor.max_sample_gap_seconds, poll * 2)),
        usage_tracking=replace(config.usage_tracking, interval_seconds=usage,
            self_user=args.user.strip() if args.user is not None else config.usage_tracking.self_user))
    if args.runtime_dir:  # 测试可隔离日志、状态与数据库，避免混入日常监控数据。
        root = args.runtime_dir.resolve()
        config = replace(config, runtime=replace(config.runtime,
            state_file=root / "state.json", log_file=root / "watcher.log"),
            usage_tracking=replace(config.usage_tracking, database_path=root / "occupancy.db",
                                   export_dir=root / "exports"))
    if args.max_cycles < 0:
        raise ValueError("max-cycles 不能为负数")
    return config


def _format_entry_slots(platform_host) -> str:  # 格式化各入口的 GPU ID 空位，用于控制台展示，如 '203-1=0/2,203-2=1/2'。
    if platform_host is None or not platform_host.source_slots:
        return "无"
    return ",".join(
        f"{name.removeprefix('autodl-')}={idle}/{total}"
        for name, idle, total in platform_host.source_slots
    )


def _ready_samples(samples, thresholds):  # 筛选出满足显存容量条件的 GPU 样本，按 INDEX 排序。
    return [
        sample
        for sample in sorted(samples, key=lambda item: item.gpu_index)
        if sample_meets_capacity(sample, thresholds)
    ]


def _fresh_gpu_indices(samples, now, stale_after_seconds):  # 只有本轮全部样本新鲜时，才允许辅助校验占用表中的额外空行。
    if not samples or any(not -5 <= (now - item.observed_at).total_seconds() <= stale_after_seconds for item in samples):
        return None
    indices = {item.gpu_index for item in samples}
    return indices if len(indices) == len(samples) else None  # 重复 INDEX 表明快照不可靠，不能据此删掉占用表的额外行。


def _format_ready_indices(samples, thresholds) -> str:  # 格式化达标 GPU 索引，用于控制台展示，如 '#0(21728MB),#1(21736MB)'。
    ready = _ready_samples(samples, thresholds)
    if not ready:
        return "无"
    return ",".join( f"#{sample.gpu_index}({sample.memory_free_mb}MB)" for sample in ready )


_STATE_RESTORE_MAX_AGE_SECONDS = 300  # 只续接五分钟内的采样历史，过旧记录重新积累。
_POWER_ON_CONFIRM_GRACE_SECONDS = 120  # 请求受理后等待实时占用证据的最长宽限期。


def _parse_local_iso(value: object) -> datetime | None:  # 解析 state/SQLite 中的本地 ISO 时间；无效值返回 None。
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
) -> bool:  # 仅恢复很近的 evaluator 状态，避免数小时/数天前的 alerted 永久锁死。
    saved_at = _parse_local_iso(persisted.get("saved_at"))
    if saved_at is None:
        return False
    age = (now - saved_at).total_seconds()
    return -5 <= age <= max_age_seconds


def _restore_recent_evaluator_state(
    evaluator: AvailabilityEvaluator,
    persisted: dict,
    capacity_fingerprint: str,
    now: datetime,
    host: str,
) -> tuple[bool, int]:  # 恢复近期连续采样，但无条件清除跨进程 alerted 锁。
    if persisted.get("capacity_fingerprint") != capacity_fingerprint:  # 用户、目标或阈值变了，旧连续达标记录就不能沿用。
        return False, 0
    if not _persisted_state_is_fresh(persisted, now):
        return False, 0
    evaluator.import_state(persisted.get("evaluator", {}))
    return True, evaluator.rearm_host(host)  # 保留近期采样连续性，但清除上次进程的已触发锁。


def main(argv=None) -> None:  # 主入口：初始化各组件后进入无限监控循环。
    _enable_ansi_colors()
    load_dotenv()
    preliminary = argparse.ArgumentParser(add_help=False)
    preliminary.add_argument("--config", default="config.yaml")
    config_path = Path(preliminary.parse_known_args(argv)[0].config).resolve()  # 先取配置路径，再用文件内容构造完整参数默认值。
    config = load_config(config_path)
    parser = _build_parser(config.auto_start.default_host)
    args = parser.parse_args(argv)
    try:
        config = apply_monitor_options(config, args)
    except (ValueError, argparse.ArgumentTypeError) as exc:
        parser.error(str(exc))

    run_monitor(config, args, config_path, parser)  # 所有启动方式最终复用这一条监控业务链。


def run_monitor(config, args, config_path, parser):  # 独立监控入口，保留 CLI 和 UI 共用的业务链路。

    try:
        selected_host = normalize_host(args.host)       # "203" → "gpu-203"
        selected_entry = normalize_entry(args.entry, selected_host)  # "auto" / "2" / "203-2"
        try:
            selected_targets = select_cli_targets(config.auto_start, selected_host, selected_entry)
        except ValueError:
            if not args.dry_run or selected_entry is None:
                raise
            target = AutoStartTarget(selected_host, selected_entry, "", "gpu", 100, True)
            selected_targets = (target,)  # 未配置实例的实时入口只允许只读监控。
            config = replace(config, auto_start=replace(config.auto_start,
                targets=(*config.auto_start.targets, target)))
    except ValueError as exc:
        parser.error(str(exc))

    if args.convert_no_gpu and (selected_entry is None or not args.live):
        parser.error("无卡转有卡须同时指定固定 --entry 和 --live")
    if args.convert_no_gpu and (len(selected_targets) != 1 or selected_targets[0].start_mode != "gpu"
                                or not config.auto_start.verify_before_start):
        parser.error("无卡转有卡仅支持一个有卡固定实例，并须启用开机前二次确认")
    dry_run = config.auto_start.dry_run
    if args.dry_run:
        dry_run = True
    elif args.live:
        dry_run = False

    selected_auto_start = replace(  # 用选中的 targets 替换配置中的全量 targets
        config.auto_start,
        dry_run=dry_run,
        targets=selected_targets,
    )

    config.runtime.log_file.parent.mkdir(parents=True, exist_ok=True)
    logger = _setup_logging(str(config.runtime.log_file))

    evaluator = AvailabilityEvaluator(config.monitor, config.idle_thresholds)
    state_store = JsonStateStore(config.runtime.state_file)
    persisted = state_store.load()
    target_signature = ",".join(item.machine_name for item in selected_targets)
    capacity_fingerprint = (  # 拼接关键配置形成签名字符串，用于判断历史状态是否仍适用；不是哈希值。
        f"v0.6|user={config.usage_tracking.self_user}|host={selected_host}|targets={target_signature}|"
        f"util={config.idle_thresholds.gpu_util_check_enabled}:"
        f"{config.idle_thresholds.gpu_util_max_pct}|"
        f"free={config.idle_thresholds.memory_free_min_mb}:"
        f"{config.idle_thresholds.memory_free_min_ratio}|"
        f"confirm={config.monitor.confirmation_seconds}:"
        f"{config.monitor.min_idle_samples}|dry_run={dry_run}"
    )
    state_now = datetime.now()
    state_restored, restored_alerted = _restore_recent_evaluator_state(
        evaluator,
        persisted,
        capacity_fingerprint,
        state_now,
        selected_host,
    )
    if state_restored:
        if restored_alerted:
            logger.info(
                "restored evaluator continuity but reset stale alerted host=%s count=%d",
                selected_host,
                restored_alerted,
            )
    elif persisted:
        logger.info(
            "skip stale evaluator state saved_at=%s fingerprint_match=%s",
            persisted.get("saved_at"),
            persisted.get("capacity_fingerprint") == capacity_fingerprint,
        )

    email_notifier = None
    if config.notification.email_enabled:
        email_notifier = EmailNotifier(config.notification.subject_prefix)

    platform_collector = PlatformBrowserCollector(config.platform)   # 浏览器 → AutoDL 平台
    telemetry_collector = TelemetryApiCollector(config.telemetry)    # HTTP   → Telemetry API
    usage_report_enabled = config.usage_tracking.enabled and not args.no_usage_report
    usage_logger = UsageSqliteLogger(config.usage_tracking.database_path) if usage_report_enabled else None  # 历史统计与电源安全核验独立。
    self_user = config.usage_tracking.self_user.strip()

    owned_instances: list[_OwnedInstance] = []  # v0.5.1：SQLite 只用于历史统计，绝不参与“本人现在是否已开机”的判定。 每次启动都从 UNKNOWN 开始，必须等本进程成功采集“查看占用”后， 才能进入 ACTIVE / ABSENT。这样彻底消除数据库陈旧记录导致的假绿色。
    self_occupancy_known = False  # 新进程从未知状态开始，不能用历史数据库推断当前已开机。

    pending_start_until = 0.0  # power_on=Success 只代表请求被受理。给实例最多 120 秒启动并出现在 “查看占用”中；这段时间不重复发开机请求，也绝不显示“已开机”。
    pending_start_machine = ""
    usage_targets = sorted(  # 占用核验覆盖该物理主机的所有已启用入口，避免漏掉本人实例。
        (
            item
            for item in config.auto_start.targets
            if item.enabled and item.host == selected_host
        ),
        key=lambda item: (item.priority, item.machine_name, item.instance_uuid),
    )
    next_usage_capture = 0.0  # 下次采集占用快照的时间戳（monotonic）
    absence_confirmations = 0  # v0.5.4：空占用快照需要连续确认，避免一次 DOM 空读就把本人误判为关机。
    login_validation_pending = False
    previous_account_clear = False  # 实例列表确认状态变化时重新武装 GPU 空闲事件。
    account_occupancy_conflict = False  # 新鲜弹窗有卡却被列表判无卡时，保持阻断直到可靠空快照。
    last_occupancy_failed = False  # 将弹窗采集故障与实例状态分别显示。
    occupancy_identity_unresolved = False  # 无用户名占用行未获账号实例列表核实时持续阻断开机。

    starter = AutoStartCoordinator(
        selected_auto_start,
        config.monitor,
        config.idle_thresholds,
        platform_collector,
        telemetry_collector,
        convert_no_gpu=args.convert_no_gpu,
    )

    auto_mode = "关闭"
    if selected_auto_start.enabled:
        auto_mode = "DRY-RUN" if selected_auto_start.dry_run else "真实开机"
        if args.convert_no_gpu:
            auto_mode += "；目标达标时关闭本账号全部无卡实例释放额度"

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
    if config.platform.autodl_direct:
        print( "AutoDL网络：watcher 控制面直连，绕过系统代理；" "Telemetry 仍可使用本机 HTTP_PROXY。" )
    print(f"监控日志：{config.runtime.log_file}")
    if usage_report_enabled:
        usage_entry_text = ", ".join(item.machine_name for item in usage_targets) or "无"
        print(
            "占用日志：每 "
            f"{config.usage_tracking.interval_seconds} 秒轮询全部入口[{usage_entry_text}]；"
            f"SQLite主库={config.usage_tracking.database_path}；"
            f"CSV导出目录={config.usage_tracking.export_dir}；"
            f"本人用户={self_user or '未配置'}"
        )

    if not usage_report_enabled:
        print("占用统计与报表已关闭；开机必需的实时占用核验继续运行。")

    cycles = 0
    stop_requested = lambda: args.stop_file is not None and args.stop_file.exists()
    try:
        while not stop_requested():
            cycle_start = time.monotonic()  # 本轮开始时间（用于控制循环间隔）

            platform_session_verified = False
            try:
                all_platform_hosts = platform_collector.collect()  # 1a. 从 AutoDL 控制台采集平台级 GPU ID 空位（Playwright 控制浏览器）
                if stop_requested():
                    break
                platform_session_verified = True
                emit_session("valid", "登录会话有效，平台主机接口已核验")
                if login_validation_pending:
                    print( f"[{datetime.now():%H:%M:%S}] 登录验证成功，平台主机接口已恢复，继续监控。", flush=True, )
                    logger.info("platform login recovery verified by machine/list success")
                    login_validation_pending = False
                platform_hosts = [
                    item for item in all_platform_hosts if item.host == selected_host
                ]
                if not platform_hosts or (selected_entry and selected_entry not in platform_hosts[0].source_names):
                    raise PlatformTransientError("所选主机或入口不在当前账号的实时主机列表中")
                selected_instance_state = None
                selected_instance_error = False
                selected_instance_missing = False
                if args.convert_no_gpu:  # 占用弹窗失败也要独立查询完整实例列表，确认所选无卡实例及同账号其他入口。
                    target = selected_targets[0]
                    try:
                        selected_instance_state = platform_collector.get_instance_state(
                            target.instance_uuid, target.machine_name)
                    except PlatformAuthenticationError:
                        raise
                    except Exception as exc:
                        selected_instance_error = True
                        selected_instance_missing = (
                            isinstance(exc, PlatformTransientError)
                            and str(exc) == "实例列表中目标 UUID 缺失或重复"
                        )
                        logger.warning("selected instance state query failed host=%s entry=%s: %s",
                                       selected_host, target.machine_name, exc)
                selected_account_clear = bool(
                    isinstance(selected_instance_state, dict)
                    and selected_instance_state.get("host_account_gpu_clear") is True
                    and (selected_instance_state.get("status") == "shutdown" or (
                        selected_instance_state.get("status") == "running"
                        and selected_instance_state.get("start_mode") == "non_gpu"))
                )
                if selected_account_clear and not previous_account_clear:
                    evaluator.rearm_host(selected_host)  # 先前被未知占用抑制的达标事件可再次触发。
                previous_account_clear = selected_account_clear
                if selected_instance_error:
                    selected_instance_text = (
                        "配置实例不存在或ID已过期" if selected_instance_missing else "查询失败"
                    )
                elif selected_instance_state is None:
                    selected_instance_text = "未查询"
                else:
                    status, mode = selected_instance_state.get("status"), selected_instance_state.get("start_mode")
                    selected_instance_text = (
                        "无卡运行" if (status, mode) == ("running", "non_gpu")
                        else "有卡运行" if (status, mode) == ("running", "gpu")
                        else "已关机" if status == "shutdown" else f"{status}/{mode}"
                    )
                all_gpu_samples = telemetry_collector.collect()  # 1b. 从 Telemetry API 采集物理 GPU 显存快照（HTTP GET）
                if stop_requested():
                    break
                selected_gpu_samples = [
                    item for item in all_gpu_samples if item.host == selected_host
                ]
                gpu_samples = filter_samples_to_platform_candidates(  # 1c. 平台有入口空位且当前账号可见的物理 GPU 才进入评估。
                    selected_gpu_samples, platform_hosts,
                )[0]

                now = datetime.now()
                trigger_events = evaluator.evaluate(platform_hosts, gpu_samples, now)  # evaluator 内部维护 idle_samples 队列，判断哪些 GPU 已连续达标

                platform_host = platform_hosts[0] if platform_hosts else None
                entry_slots = _format_entry_slots(platform_host)              # 各入口空闲 GPU ID 数
                ready_samples = _ready_samples(gpu_samples, config.idle_thresholds)  # 显存达标的 GPU
                ready_indices = _format_ready_indices(gpu_samples, config.idle_thresholds)
                platform_text = (
                    "不可见"
                    if platform_host is None
                    else f"{platform_host.free_count}/{platform_host.total_count}"
                )

                self_active = self_occupancy_known and bool(owned_instances) and not selected_account_clear  # 新鲜完整的账号列表可覆盖旧弹窗记录；本轮正向弹窗仍在发送前阻断。
                occupancy_unknown = (
                    config.usage_tracking.enabled and bool(usage_targets) and not self_occupancy_known
                )
                occupancy_blocked = (
                    (occupancy_unknown and not selected_account_clear)
                    or account_occupancy_conflict or occupancy_identity_unresolved
                )  # 未核实身份的有卡占用行不允许被实例列表的旧判断覆盖。
                pending_start = (
                    pending_start_until > 0 and time.monotonic() < pending_start_until
                )
                if pending_start_until > 0 and not pending_start:
                    reset_count = evaluator.rearm_host(selected_host)  # 超过确认宽限期仍未看到本人实例：认为开机没有真正落地，重新武装。
                    logger.warning(
                        "power_on confirmation expired; rearmed host=%s machine=%s reset_alerted=%d",
                        selected_host,
                        pending_start_machine or "unknown",
                        reset_count,
                    )
                    pending_start_until = 0.0
                    pending_start_machine = ""

                if self_active or occupancy_blocked or pending_start or starter.pending_switch or starter.quota_blocked:  # 有卡占用、证据不足或切换中均阻止重复开机。
                    trigger_events = []

                planned_target = None  # 根据实时空位预判入口。UNKNOWN 时仍显示预选入口，但不会真正开机。
                if platform_host is not None and not self_active:
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

                if trigger_events and planned_target is None and not self_active:  # v0.5.4：evaluator 看到的是物理主机聚合空位。固定 203-2 时， 203-1 有空位也可能生成 trigger；如果当前选定 targets 实际无可用实例， 必须在这里吃掉事件，不能每 10 秒刷“没有可用固定实例”。
                    evaluator.rearm_host(selected_host)
                    trigger_events = []

                if starter.quota_blocked is not None:
                    planned_text = (starter.quota_blocked.machine_name or "未知入口").removeprefix("autodl-")
                    start_ready_text = "额度不足"
                    action_text = "暂停自动开机，请处理租户额度后重新开始监控"
                elif self_active:
                    ready_indices = _format_owned_indices(  # 绿色“已开机”只来自本进程实时占用快照的正向证据。
                        owned_instances,
                        selected_gpu_samples,
                    )
                    planned_text = _format_owned_entry(owned_instances)
                    start_ready_text = "已开机"
                    action_text = "已开机，继续监控"
                elif pending_start:
                    planned_text = (
                        pending_start_machine.removeprefix("autodl-")
                        if pending_start_machine
                        else (planned_target.machine_name.removeprefix("autodl-") if planned_target else "等待确认")
                    )
                    start_ready_text = "请求已受理"
                    action_text = "等待实例出现在占用详情"
                elif starter.pending_switch:
                    planned_text = starter.pending_switch.machine_name.removeprefix("autodl-")
                    start_ready_text = "等待无卡关机"
                    action_text = "确认关机后复核 GPU 再开机"
                elif occupancy_identity_unresolved:
                    planned_text = selected_targets[0].machine_name.removeprefix("autodl-")
                    start_ready_text = "占用身份未确认"
                    action_text = "暂缓切换，等待账号实例列表核实占用"
                elif account_occupancy_conflict:
                    planned_text = selected_targets[0].machine_name.removeprefix("autodl-")
                    start_ready_text = "占用证据冲突"
                    action_text = "暂停切换，等待占用弹窗确认无卡"
                elif occupancy_unknown and selected_account_clear:
                    planned_text = selected_targets[0].machine_name.removeprefix("autodl-")
                    start_ready_text = "是" if start_ready else "否"
                    action_text = ("空闲达标，核对占用" if trigger_events and selected_auto_start.enabled
                                   else "等待平台空位和显存达标" if not start_ready else "继续监控")
                elif occupancy_unknown:
                    planned_text = (
                        planned_target.machine_name.removeprefix("autodl-")
                        if planned_target is not None
                        else "待确认"
                    )
                    start_ready_text = ("实例查询失败" if selected_instance_error else "同主机账号状态未排除"
                                        if args.convert_no_gpu else "本人状态未知")
                    action_text = ("暂缓切换，先核对实例列表" if args.convert_no_gpu
                                   else "暂缓开机，先确认本人占用")
                else:
                    start_ready_text = "是" if start_ready else "否"
                    if trigger_events and selected_auto_start.enabled:
                        action_text = "触发自动开机"
                    elif start_ready:
                        action_text = "条件已处理，继续监控"
                    elif platform_host is not None and planned_target is None:
                        action_text = "固定入口当前无可用实例，继续等待"
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

                status_line = (
                    f"[{now:%H:%M:%S}] {selected_host} | "
                    f"平台[{entry_slots}] | "
                    f"物理INDEX={len(selected_gpu_samples)} | "
                    f"显存达标={ready_indices} | "
                    f"预选入口={planned_text} | "
                    f"开机达标={start_ready_text} | "
                    f"动作={action_text}"
                )
                if args.convert_no_gpu:
                    status_line += f" | 所选实例={selected_instance_text}"
                if starter.quota_blocked is not None:
                    status_line = _red_terminal_text(status_line)
                elif self_active:
                    status_line = _green_terminal_text(status_line)
                print(status_line, flush=True)
                logger.info(  # 结构化日志（用于事后分析）
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

                fresh_occupancy_positive = False  # 当前轮弹窗若直接看到本人有卡，优先于实例列表的相反结果。
                if (
                    config.usage_tracking.enabled and time.monotonic() >= next_usage_capture and usage_targets
                ):
                    raw_occupancy = []  # 每轮重新收集原始入口视图，不把上一轮记录当作本轮证据。
                    entry_summaries: dict[str, str] = {}
                    failed_entries: list[str] = []
                    fast_absence_recheck = False
                    slot_map = (
                        {name: (idle, total) for name, idle, total in platform_host.source_slots}
                        if platform_host is not None
                        else {}
                    )
                    for usage_target in usage_targets:  # 逐个入口点击“查看占用”。v0.5.4 会同时校验弹窗标题和 machine/list 的 idle/total，宁可采集失败也不允许 203-2 串到 203-1。
                        if stop_requested():
                            break
                        entry_name = usage_target.machine_name
                        expected_idle = None
                        expected_total = None
                        if entry_name in slot_map:
                            expected_idle, expected_total = slot_map[entry_name]
                        try:
                            entry_records = platform_collector.collect_occupancy(
                                entry_name,
                                expected_idle=expected_idle,
                                expected_total=expected_total,
                                expected_gpu_indices=_fresh_gpu_indices(
                                    selected_gpu_samples, datetime.now(), config.monitor.stale_after_seconds),
                            )
                            raw_occupancy.extend(replace(item, display_name=item.display_name or item.user)
                                                 for item in entry_records)  # 旧七列用户名保留为展示名。
                            entry_summaries[entry_name] = ""  # 拆分多实例后再生成带名字的入口摘要。
                        except Exception as entry_exc:
                            failed_entries.append(f"{entry_name.removeprefix('autodl-')}:{entry_exc}")
                            entry_summaries[entry_name] = "采集失败"
                            logger.exception(
                                "occupancy entry capture failed host=%s entry=%s",
                                selected_host,
                                entry_name,
                            )

                    if stop_requested():
                        break  # 停止时丢弃不完整快照，不写入伪下机事件。
                    last_occupancy_failed = bool(failed_entries)
                    unknown_rows = [item for item in raw_occupancy if item.occupied and not item.user.strip()]
                    identity_unresolved_now = False
                    if unknown_rows:  # 六列括号名不是身份证据，仍查询完整账号 UUID 列表。
                        account_ids: set[str] = set()
                        try:
                            account_rows = platform_collector.get_account_instances()
                            if not isinstance(account_rows, list):
                                raise PlatformTransientError("账号实例列表不完整")
                            for row in account_rows:
                                instance_id = row.get("instance_uuid") if isinstance(row, dict) else None
                                if not isinstance(instance_id, str) or not _INSTANCE_UUID.fullmatch(instance_id):
                                    raise PlatformTransientError("账号实例列表含无法识别的 UUID")
                                if instance_id.lower() in account_ids:
                                    raise PlatformTransientError("账号实例列表有重复 UUID")
                                account_ids.add(instance_id.lower())
                        except PlatformAuthenticationError:
                            raise
                        except Exception as exc:
                            account_ids.clear()  # 部分列表不参与本人归属判断。
                            identity_unresolved_now = True
                            logger.warning("occupancy identity query failed host=%s: %s", selected_host, exc)
                        identity_label = self_user or "当前账号"
                        resolved_occupancy = []
                        for item in raw_occupancy:
                            if not item.occupied or item.user.strip():  # 旧版七列的明确用户名继续沿用。
                                resolved_occupancy.append(item)
                                continue
                            bindings, complete_ids = _instance_bindings_from_cell(item.instance_id)
                            for instance_id, display_name in bindings:
                                resolved_occupancy.append(replace(
                                    item, instance_id=instance_id, display_name=display_name,
                                    user=identity_label if instance_id in account_ids else ""))
                            if not complete_ids:  # 原始畸形单元格保留供核验，但不向终端输出 ID。
                                identity_unresolved_now = True
                                resolved_occupancy.append(item)
                        raw_occupancy = resolved_occupancy
                    if identity_unresolved_now:
                        occupancy_identity_unresolved = True
                    try:  # 先判定本人状态，再决定该轮是否允许 SQLite 生成 END_SEEN。 第一次可靠空快照只是“疑似结束”，不会立即关掉 current_instances。
                        complete_occupancy_snapshot = not failed_entries and not identity_unresolved_now
                        captured_owned = _owned_instances_from_records(
                            raw_occupancy,
                            self_user or "当前账号",
                            selected_host,
                        )
                        previous_known = self_occupancy_known
                        previous_active = self_occupancy_known and bool(owned_instances)
                        absence_confirmations, confirmed_absence, fast_absence_recheck = (
                            _advance_absence_confirmation(
                                previous_known=previous_known,
                                previous_active=previous_active,
                                complete_snapshot=complete_occupancy_snapshot,
                                captured_owned=bool(captured_owned),
                                current_streak=absence_confirmations,
                                required=config.usage_tracking.absent_confirmations_required,
                            )
                        )

                        if complete_occupancy_snapshot and (captured_owned or confirmed_absence):
                            if occupancy_identity_unresolved:
                                evaluator.rearm_host(selected_host)
                            occupancy_identity_unresolved = False
                        if captured_owned:
                            fresh_occupancy_positive = True
                            if selected_account_clear:
                                account_occupancy_conflict = True  # 同轮两份正向/负向证据冲突，不能在下一次弹窗失败后贸然关机。
                            owned_instances = captured_owned  # 实时看到本人实例就是正向证据，无需等待多轮缺席确认。
                            self_occupancy_known = True
                            pending_start_until = 0.0
                            pending_start_machine = ""
                        elif complete_occupancy_snapshot:
                            if fast_absence_recheck:
                                entry_summaries["本人状态"] = (
                                    f"疑似结束 {absence_confirmations}/"
                                    f"{config.usage_tracking.absent_confirmations_required}，待复核")

                            if confirmed_absence:  # 连续可靠空快照达到门槛后，才能解除本人占用状态。
                                if account_occupancy_conflict:
                                    account_occupancy_conflict = False
                                    evaluator.rearm_host(selected_host)  # 冲突消除后下一轮重新评估先前被拦截的空闲事件。
                                owned_instances = []
                                self_occupancy_known = True
                                pending_still_valid = (
                                    pending_start_until > 0 and time.monotonic() < pending_start_until
                                )
                                if previous_active:
                                    rearmed_gpu_count = evaluator.rearm_host(selected_host)
                                    pending_start_until = 0.0
                                    pending_start_machine = ""
                                    rearm_line = (
                                        f"[{datetime.now():%H:%M:%S}] 本人占用已连续确认结束 | "
                                        f"主机={selected_host} | "
                                        f"确认={absence_confirmations}/{config.usage_tracking.absent_confirmations_required} | "
                                        f"已清除alerted={rearmed_gpu_count} | "
                                        "自动开机=重新武装"
                                    )
                                    print(rearm_line, flush=True)
                                    logger.warning(
                                        "self occupancy ended after repeated confirmation; evaluator rearmed host=%s reset_alerted=%d user=%s confirmations=%d",
                                        selected_host,
                                        rearmed_gpu_count,
                                        self_user,
                                        absence_confirmations,
                                    )
                                elif not previous_known and not pending_still_valid:
                                    evaluator.rearm_host(selected_host)

                        complete_for_db = bool(  # 疑似下机的第一次空快照不允许 SQLite 生成 END_SEEN。
                            complete_occupancy_snapshot
                            and (
                                captured_owned
                                or confirmed_absence
                                or (previous_known and not previous_active)
                            )
                        )
                        usage_events = usage_logger.record(
                            raw_occupancy,
                            complete_snapshot=complete_for_db,
                            snapshot_hosts={selected_host},  # 数据库结束事件仅作用于本轮覆盖的主机，其他主机历史不受影响。
                        ) if usage_logger is not None else []
                        instance_records = merge_duplicate_instances(raw_occupancy)
                        gpu_rows = aggregate_gpu_occupants(raw_occupancy)

                        instance_text = ",".join(
                            f"{item.machine_name.replace('autodl-', '')}#{item.gpu_index}:{_occupancy_display_name(item)}"
                            for item in instance_records
                        ) or "无"
                        gpu_text = ",".join(
                            f"#{row['gpu_index']}:{row['occupant_count']}人"
                            for row in gpu_rows
                        ) or "无"
                        for entry, error in entry_summaries.items():
                            if not error:
                                entry_summaries[entry] = ",".join(
                                    f"#{item.gpu_index}:{_occupancy_display_name(item)}"
                                    for item in raw_occupancy if item.machine_name == entry and item.occupied) or "无"
                        summary_text = "; ".join(
                            f"{entry.removeprefix('autodl-')}[{text}]"
                            for entry, text in entry_summaries.items()) or "无数据"
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
                        print( f"[{datetime.now():%H:%M:%S}] 占用日志写入失败：{usage_exc}", flush=True, )
                        logger.exception("occupancy log write failed")
                    finally:
                        if fast_absence_recheck:  # 疑似下机时下一轮主循环立即复核；正常情况仍按 60 秒采集。
                            next_usage_capture = (
                                time.monotonic()
                                + config.usage_tracking.absence_recheck_seconds
                            )
                        else:
                            next_usage_capture = (
                                time.monotonic() + config.usage_tracking.interval_seconds
                            )

                pending_start = (  # 占用状态发生变化后再次做安全门控。首次 UNKNOWN、本人 ACTIVE、 或 power_on 等待确认期间都不允许发送新的开机请求。
                    pending_start_until > 0 and time.monotonic() < pending_start_until
                )
                if (
                    (config.usage_tracking.enabled and usage_targets and not self_occupancy_known
                     and not selected_account_clear)
                    or fresh_occupancy_positive
                    or account_occupancy_conflict
                    or occupancy_identity_unresolved
                    or (self_occupancy_known and owned_instances and not selected_account_clear)
                    or pending_start
                    or starter.pending_switch
                    or starter.quota_blocked
                ):
                    trigger_events = []

                if email_notifier is not None:
                    for event in trigger_events:
                        email_notifier.send(event)

                if selected_auto_start.enabled and not stop_requested():
                    results = []
                    if starter.pending_switch:  # 关机后的状态推进不依赖 evaluator 再次触发。
                        if not account_occupancy_conflict and not occupancy_identity_unresolved and not fresh_occupancy_positive and not (owned_instances and not selected_account_clear) and not pending_start:  # 旧记录可覆盖，证据冲突仍阻断。
                            result = starter.continue_switch(stop_requested)
                            if result is not None:
                                results.append(result)
                    else:
                        results = [starter.attempt(event, stop_requested) for event in
                                   trigger_events[: selected_auto_start.max_starts_per_event]]
                    for result in results:
                        result_text = format_start_result(result)
                        if result.status in {"request_accepted", "gpu_start_observed"}:
                            if result.status == "gpu_start_observed":
                                result_text = _green_terminal_text(result_text)  # 受理只表示请求进入队列；实际有卡运行才显示成功色。
                            pending_start_until = (
                                time.monotonic() + _POWER_ON_CONFIRM_GRACE_SECONDS
                            )
                            pending_start_machine = result.machine_name or ""
                            next_usage_capture = 0.0  # 开机请求受理后尽快读取占用详情，核实是否真正出现实例。
                            logger.info(
                                "gpu start pending occupancy confirmation status=%s host=%s machine=%s instance=%s grace=%ss",
                                result.status,
                                result.host,
                                result.machine_name,
                                result.instance_uuid,
                                _POWER_ON_CONFIRM_GRACE_SECONDS,
                            )
                        elif (
                            not selected_auto_start.dry_run and not starter.pending_switch
                            and result.status in {"request_failed", "recheck_failed", "no_target", "shutdown_failed", "instance_state_blocked"}
                        ):
                            evaluator.rearm_host(selected_host)  # 请求根本没有成功落地时，不能让 evaluator 的 alerted 锁住后续重试。
                        if result.status in {"request_failed", "recheck_failed", "shutdown_failed", "switch_failed", "instance_state_blocked", "quota_blocked"}:
                            result_text = _red_terminal_text(result_text)
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

                state_store.save(
                    {
                        "saved_at": now.isoformat(),
                        "capacity_fingerprint": capacity_fingerprint,
                        "evaluator": evaluator.export_state(),
                    }
                )
                self_gpu_text = ('有（本轮弹窗）' if fresh_occupancy_positive
                                 else '待复核（占用证据冲突）' if account_occupancy_conflict
                                 else '待确认（占用身份未确认）' if occupancy_identity_unresolved
                                 else '无（实例列表）' if selected_account_clear
                                 else '有' if owned_instances else '无' if self_occupancy_known else '待确认')
                detail = (f" · 所选实例 {selected_instance_text}" if args.convert_no_gpu else "")
                if starter.quota_blocked is not None:
                    detail += " · 额度不足，暂停自动开机"  # 保留实时监控心跳，同时明确电源操作已被阻断。
                if last_occupancy_failed:
                    detail += " · GPU 占用弹窗采集失败"
                print(f"WATCHER_STATUS {now:%H:%M:%S} · {selected_host} · 平台 {entry_slots} · "
                      f"物理 GPU {len(selected_gpu_samples)} 张 · 本人 GPU 占用 {self_gpu_text}{detail}", flush=True)
            except PlatformAuthenticationError as exc:
                emit_session("invalid", f"登录会话失效，请重新登录：{exc}")
                if args.no_login:
                    print("WATCHER_STATUS 登录会话失效 · 监控已停止，请在主界面重新登录", flush=True)
                    raise SystemExit(3) from None  # GUI收到明确状态，避免预期认证失败变成PyInstaller堆栈。
                now = datetime.now()  # v0.5.2：只有明确 /login 或 HTTP 401/403 才进入人工登录恢复。
                print(f"[{now:%H:%M:%S}] 登录会话确认失效：{exc}", flush=True)
                logger.warning("platform authentication expired: %s", exc)
                platform_collector.close()
                try:
                    interactive_login(config, reason="expired")
                    print( f"[{datetime.now():%H:%M:%S}] 登录资料已更新；下一轮将验证主机接口，验证成功后再恢复监控。", flush=True, )
                    login_validation_pending = True
                    self_occupancy_known = False  # 登录会话更新后重新确认本人占用，不沿用更新前的判断。
                    owned_instances = []
                    occupancy_identity_unresolved = False
                    pending_start_until = 0.0
                    pending_start_machine = ""
                    evaluator.rearm_host(selected_host)
                    next_usage_capture = 0.0  # 登录恢复后的下一轮尽快重采占用，解除未知状态。
                except Exception as login_exc:
                    print( f"[{datetime.now():%H:%M:%S}] 登录恢复失败：{login_exc}", flush=True, )
                    logger.exception("interactive login recovery failed")
            except PlatformTransientError as exc:
                if not platform_session_verified:  # 后续入口或数据故障不否定本轮已成功的会话核验。
                    emit_session("unknown", f"平台请求异常，无法确认会话：{exc}")
                print(f"WATCHER_STATUS 平台采集暂时失败 · 等待重试：{exc}", flush=True)
                now = datetime.now()  # 页面/API 慢、超时、429、5xx 都属于数据采集瞬时故障。 不弹登录窗口，不修改本人占用状态，也绝不发送开机请求。
                print( f"[{now:%H:%M:%S}] AutoDL 主机接口暂时不可用：{exc} | " "本轮跳过开机，保持登录状态并自动重试。", flush=True, )
                logger.warning("platform transient failure: %s", exc)
            except Exception as exc:
                if not platform_session_verified:
                    emit_session("unknown", f"主机接口核验失败，无法确认会话：{exc}")
                print(f"WATCHER_STATUS 本轮采集失败 · 等待重试：{exc}", flush=True)
                now = datetime.now()  # 本轮任意环节抛异常 → 打日志，不中断循环
                print(f"[{now:%H:%M:%S}] 本轮失败：{exc}", flush=True)
                logger.exception("collection/auto-start cycle failed")

            cycles += 1  # 控制循环间隔：睡眠到距离上次开始正好 poll_seconds
            if args.max_cycles and cycles >= args.max_cycles:
                break
            deadline = cycle_start + config.monitor.poll_seconds  # 采集耗时计入周期，避免每轮额外叠加完整等待时间。
            while not stop_requested() and time.monotonic() < deadline:
                time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))  # 分段等待以便及时响应 GUI 停止文件。
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，正在保存状态并退出……")  # Ctrl+C → 保存状态后优雅退出
        state_store.save(
            {
                "saved_at": datetime.now().isoformat(),
                "capacity_fingerprint": capacity_fingerprint,
                "evaluator": evaluator.export_state(),
            }
        )
    finally:
        try:
            state_store.save({"saved_at": datetime.now().isoformat(),
                              "capacity_fingerprint": capacity_fingerprint,
                              "evaluator": evaluator.export_state()})
        finally:
            platform_collector.close()
            for handler in list(logger.handlers):
                handler.close()
                logger.removeHandler(handler)  # 释放处理器引用，后续同进程启动可重新绑定日志文件。
        print("监控已安全停止。")


if __name__ == "__main__":
    main()
