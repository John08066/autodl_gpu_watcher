"""
演示脚本 — 手动测试 evaluator 的告警触发逻辑。

构造 3 组模拟的 GPU 采样数据（3 张 GPU，3 轮采样），
运行 AvailabilityEvaluator 评估，观察告警生成结果。

适用场景：
    无需真实浏览器和 API 连接，快速验证 evaluator 的逻辑正确性。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .config import load_config
from .evaluator import AvailabilityEvaluator
from .models import GpuSample, PlatformHost
from .notifiers import ConsoleNotifier


def sample(at: datetime, gpu_index: int, util: float, used: int) -> GpuSample:
    """功能：
        构造一个模拟的 GpuSample。

    参数：
        at (datetime)：构造样本时使用的采样时间。
        gpu_index (int)：物理服务器上的 GPU INDEX 编号。
        util (float)：构造演示样本时的 GPU 利用率百分比。
        used (int)：构造演示样本时的已用显存 MB。

    返回：
        GpuSample：构造完成的 GpuSample。

    补充说明：
        参数：
            at: 采样时间
            gpu_index: GPU INDEX
            util: GPU 利用率（%）
            used: 已用显存（MB）
    """
    return GpuSample(
        host="gpu-202",
        gpu_index=gpu_index,
        gpu_name="NVIDIA TITAN RTX",
        util_pct=util,
        memory_used_mb=used,
        memory_total_mb=24576,
        observed_at=at,
    )


def main() -> None:
    """功能：
        运行评估器演示，用构造的采样序列展示显存达标事件如何触发。

    参数：
        无。

    返回：
        None：函数通过副作用完成初始化、输出、持久化或资源管理。
    """
    config = load_config()
    evaluator = AvailabilityEvaluator(config.monitor, config.idle_thresholds)
    notifier = ConsoleNotifier()
    base = datetime.now().replace(microsecond=0)
    # 模拟平台：gpu-202，3 个空闲 GPU ID
    platform = [PlatformHost(host="gpu-202", free_count=3, total_count=3)]

    # 构造 3 轮采样：GPU#0 和 GPU#2 显存不足，GPU#1 显存充裕
    rounds = [
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
