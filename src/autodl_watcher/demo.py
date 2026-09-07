from __future__ import annotations  # 演示脚本 — 手动测试 evaluator 的告警触发逻辑。

from datetime import datetime, timedelta

from .config import load_config
from .evaluator import AvailabilityEvaluator
from .models import GpuSample, PlatformHost
from .notifiers import ConsoleNotifier


def sample(at: datetime, gpu_index: int, util: float, used: int) -> GpuSample:  # 构造一个模拟的 GpuSample。
    return GpuSample(
        host="gpu-202",
        gpu_index=gpu_index,
        gpu_name="NVIDIA TITAN RTX",
        util_pct=util,
        memory_used_mb=used,
        memory_total_mb=24576,
        observed_at=at,
    )


def main() -> None:  # 运行评估器演示，用构造的采样序列展示显存达标事件如何触发。
    config = load_config()
    evaluator = AvailabilityEvaluator(config.monitor, config.idle_thresholds)
    notifier = ConsoleNotifier()
    base = datetime.now().replace(microsecond=0)
    platform = [PlatformHost(host="gpu-202", free_count=3, total_count=3)]  # 模拟平台：gpu-202，3 个空闲 GPU ID

    rounds = [  # 构造 3 轮采样：GPU#0 和 GPU#2 显存不足，GPU#1 显存充裕
        [sample(base, 0, 90, 20000), sample(base, 1, 0, 350), sample(base, 2, 100, 20300)],
        [sample(base + timedelta(seconds=20), 0, 90, 20000), sample(base + timedelta(seconds=20), 1, 2, 360), sample(base + timedelta(seconds=20), 2, 100, 20300)],
        [sample(base + timedelta(seconds=40), 0, 90, 20000), sample(base + timedelta(seconds=40), 1, 0, 312), sample(base + timedelta(seconds=40), 2, 100, 20300)],
    ]

    for index, samples in enumerate(rounds, start=1):
        now = samples[0].observed_at
        alerts = evaluator.evaluate(platform, samples, now)
        print(f"[采样 {index}] {now:%H:%M:%S}，开机事件数={len(alerts)}")
        for alert in alerts:
            notifier.send(alert)


if __name__ == "__main__":
    main()
