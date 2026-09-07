from __future__ import annotations  # 冒烟测试脚本 — 快速验证采集器和两层门控链路是否正常工作。

from datetime import datetime

from .collectors import (
    PlatformBrowserCollector,
    TelemetryApiCollector,
    filter_samples_to_platform_candidates,
)
from .config import load_config
from .evaluator import sample_meets_capacity


def main() -> None:  # 执行一次真实平台与 Telemetry 采集，打印两层数据映射，供部署前冒烟验证。
    config = load_config()
    platform = PlatformBrowserCollector(config.platform)
    telemetry = TelemetryApiCollector(config.telemetry)
    try:
        platform_hosts = platform.collect()  # 采集两层数据
        all_gpu_samples = telemetry.collect()
        gpu_samples, no_slot_hosts, unauthorized_hosts = (  # 两层门控过滤
            filter_samples_to_platform_candidates(all_gpu_samples, platform_hosts)
        )

        print("\n[平台 GPU ID 状态]")
        for item in platform_hosts:
            entries = ", ".join( f"{name}={idle}/{total}" for name, idle, total in item.source_slots )
            print( f"  {item.host}: 可分配 {item.free_count}/{item.total_count} | {entries}" )

        print("\n[真实 GPU INDEX 与显存]")
        for item in gpu_samples:
            age = int((datetime.now() - item.observed_at).total_seconds())
            ready = sample_meets_capacity(item, config.idle_thresholds)
            print(
                f"  {item.host} INDEX#{item.gpu_index}: "
                f"free={item.memory_free_mb}/{item.memory_total_mb}MB "
                f"util={item.util_pct:.0f}% age={age}s "
                f"开机达标={'是' if ready else '否'}"
            )

        print("\n[平台无可分配 GPU ID 的可见主机]")
        print(f"  {', '.join(no_slot_hosts) if no_slot_hosts else '无'}")
        print("\n[当前账号不可申请、已忽略的监控主机]")
        print(f"  {', '.join(unauthorized_hosts) if unauthorized_hosts else '无'}")
    finally:
        platform.close()


if __name__ == "__main__":
    main()
