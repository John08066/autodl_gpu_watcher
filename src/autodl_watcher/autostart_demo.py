from __future__ import annotations  # 自动开机演示脚本 — 手动验证指定固定实例的 power_on 链路。

import argparse
from dataclasses import replace
from datetime import datetime

from .autostart import AutoStartCoordinator, format_start_result
from .cli import normalize_entry, normalize_host, select_cli_target
from .collectors import PlatformBrowserCollector, TelemetryApiCollector
from .config import load_config
from .models import AvailabilityAlert, ConfirmedGpu


def main() -> None:  # 运行独立的自动开机演示入口，用于验证入口选择与 power_on 调用链。
    config = load_config()
    parser = argparse.ArgumentParser(description="手动验证指定固定实例的 power_on 链路。")
    parser.add_argument("--host", default=config.auto_start.default_host)
    parser.add_argument("--entry", default=None)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--live", action="store_true")
    args = parser.parse_args()

    try:  # 解析并选择目标入口
        host = normalize_host(args.host)
        entry = normalize_entry(args.entry, host)
        target = select_cli_target(config.auto_start, host, entry)
    except ValueError as exc:
        parser.error(str(exc))

    dry_run = config.auto_start.dry_run
    if args.dry_run:
        dry_run = True
    elif args.live:
        dry_run = False
    auto_config = replace(config.auto_start, dry_run=dry_run, targets=(target,))

    platform = PlatformBrowserCollector(config.platform)
    telemetry = TelemetryApiCollector(config.telemetry)
    try:
        gpu = ConfirmedGpu(  # 构造一个虚构的开机事件（跳过 evaluator）
            host=host,
            gpu_index=0,
            gpu_name="synthetic-for-manual-test",
            util_pct=0,
            memory_used_mb=300,
            memory_total_mb=24576,
            idle_seconds=config.monitor.confirmation_seconds,
            observed_at=datetime.now(),
        )
        alert = AvailabilityAlert(
            host=host,
            platform_free_count=1,
            platform_total_count=1,
            physical_total_count=1,
            actionable_count=1,
            confirmed_gpus=(gpu,),
            platform_sources=(target.machine_name,),
        )
        result = AutoStartCoordinator(  # 执行开机尝试
            auto_config,
            config.monitor,
            config.idle_thresholds,
            platform,
            telemetry,
        ).attempt(alert)
        print(format_start_result(result))
        if not auto_config.dry_run:
            print("警告：本命令处于真实开机模式。")
    finally:
        platform.close()


if __name__ == "__main__":
    main()
