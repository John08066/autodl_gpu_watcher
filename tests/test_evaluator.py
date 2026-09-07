from __future__ import annotations  # 评估器模块的单元测试。

import unittest
from datetime import datetime, timedelta

from autodl_watcher.config import IdleThresholds, MonitorConfig
from autodl_watcher.evaluator import (
    AvailabilityEvaluator,
    required_free_memory_mb,
    sample_meets_capacity,
)
from autodl_watcher.models import GpuSample, PlatformHost


class EvaluatorTest(unittest.TestCase):
    def setUp(self) -> None:  # 为当前测试用例准备共享配置、时间基准或测试对象。
        self.monitor = MonitorConfig(
            poll_seconds=10,
            confirmation_seconds=0,
            min_idle_samples=1,
            max_sample_gap_seconds=20,
            stale_after_seconds=90,
            reset_busy_samples=1,
        )
        self.thresholds = IdleThresholds(
            gpu_util_check_enabled=False,
            gpu_util_max_pct=100,
            memory_free_min_mb=8192,
            memory_free_min_ratio=0.25,
        )
        self.base = datetime(2026, 7, 20, 11, 0, 0)

    def _sample(self, offset: int, util: float, used: int, total: int = 24576) -> GpuSample:  # 执行 `_sample` 对应的内部处理逻辑。
        return GpuSample(
            host="gpu-202",
            gpu_index=1,
            gpu_name="NVIDIA TITAN RTX",
            util_pct=util,
            memory_used_mb=used,
            memory_total_mb=total,
            observed_at=self.base + timedelta(seconds=offset),
        )

    def test_alerts_immediately_when_vram_is_sufficient(self) -> None:  # 验证测试场景 `alerts_immediately_when_vram_is_sufficient` 的预期行为。
        evaluator = AvailabilityEvaluator(self.monitor, self.thresholds)
        platform = [PlatformHost("gpu-202", 1, 1)]
        alerts = evaluator.evaluate( platform, [self._sample(0, 96, 1837)], self.base, )
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].actionable_count, 1)

    def test_high_gpu_util_is_not_a_hard_gate(self) -> None:  # 验证测试场景 `high_gpu_util_is_not_a_hard_gate` 的预期行为。
        sample = self._sample(0, 100, 2000)
        self.assertTrue(sample_meets_capacity(sample, self.thresholds))

    def test_optional_gpu_util_hard_gate_still_supported(self) -> None:  # 验证测试场景 `optional_gpu_util_hard_gate_still_supported` 的预期行为。
        hard_thresholds = IdleThresholds(True, 80, 8192, 0.25)
        self.assertFalse(sample_meets_capacity(self._sample(0, 96, 2000), hard_thresholds))
        self.assertTrue(sample_meets_capacity(self._sample(0, 70, 2000), hard_thresholds))

    def test_no_alert_when_free_memory_is_insufficient(self) -> None:  # 验证测试场景 `no_alert_when_free_memory_is_insufficient` 的预期行为。
        evaluator = AvailabilityEvaluator(self.monitor, self.thresholds)
        platform = [PlatformHost("gpu-202", 1, 1)]
        alerts = evaluator.evaluate(
            platform,
            [self._sample(0, 0, 20327)],  # only 4249 MB free
            self.base,
        )
        self.assertEqual(alerts, [])

    def test_ratio_requirement_scales_on_40gb_gpu(self) -> None:  # 验证测试场景 `ratio_requirement_scales_on_40gb_gpu` 的预期行为。
        sample = self._sample(0, 100, 30000, total=40960)
        self.assertEqual(required_free_memory_mb(sample, self.thresholds), 10240)
        self.assertTrue(sample_meets_capacity(sample, self.thresholds))
        too_full = self._sample(0, 0, 32000, total=40960)
        self.assertFalse(sample_meets_capacity(too_full, self.thresholds))

    def test_no_alert_when_platform_has_no_slot(self) -> None:  # 验证测试场景 `no_alert_when_platform_has_no_slot` 的预期行为。
        evaluator = AvailabilityEvaluator(self.monitor, self.thresholds)
        alerts = evaluator.evaluate( [PlatformHost("gpu-202", 0, 1)], [self._sample(0, 0, 300)], self.base, )
        self.assertEqual(alerts, [])

    def test_deduplicates_same_capacity_episode(self) -> None:  # 验证测试场景 `deduplicates_same_capacity_episode` 的预期行为。
        evaluator = AvailabilityEvaluator(self.monitor, self.thresholds)
        platform = [PlatformHost("gpu-202", 1, 1)]
        total_alerts = 0
        for offset in (0, 10, 20):
            total_alerts += len(
                evaluator.evaluate(
                    platform,
                    [self._sample(offset, 100, 300)],
                    self.base + timedelta(seconds=offset),
                )
            )
        self.assertEqual(total_alerts, 1)

    def test_rearm_host_allows_a_second_alert_after_self_instance_ends(self) -> None:  # 验证本人实例结束后，清除指定主机的 alerted 状态可以再次触发开机。
        evaluator = AvailabilityEvaluator(self.monitor, self.thresholds)
        platform = [PlatformHost("gpu-202", 1, 1)]

        first = evaluator.evaluate( platform, [self._sample(0, 100, 300)], self.base, )
        duplicate = evaluator.evaluate(
            platform,
            [self._sample(10, 100, 300)],
            self.base + timedelta(seconds=10),
        )
        reset_count = evaluator.rearm_host("gpu-202")
        second = evaluator.evaluate(
            platform,
            [self._sample(20, 100, 300)],
            self.base + timedelta(seconds=20),
        )

        self.assertEqual(len(first), 1)
        self.assertEqual(duplicate, [])
        self.assertEqual(reset_count, 1)
        self.assertEqual(len(second), 1)

    def test_rearm_host_does_not_change_other_hosts(self) -> None:  # 验证重新武装只作用于指定物理主机。
        evaluator = AvailabilityEvaluator(self.monitor, self.thresholds)
        platform = [PlatformHost("gpu-202", 1, 1)]
        evaluator.evaluate( platform, [self._sample(0, 100, 300)], self.base, )

        self.assertEqual(evaluator.rearm_host("gpu-203"), 0)
        self.assertEqual(
            evaluator.evaluate( platform, [self._sample(10, 100, 300)], self.base + timedelta(seconds=10), ),
            [],
        )

    def test_physical_gpu_count_comes_from_unique_gpu_indices(self) -> None:  # 验证测试场景 `physical_gpu_count_comes_from_unique_gpu_indices` 的预期行为。
        evaluator = AvailabilityEvaluator(self.monitor, self.thresholds)
        platform = [PlatformHost("gpu-202", 1, 1, ("autodl-202-2", "autodl-202-4"))]
        alerts = evaluator.evaluate(
            platform,
            [
                GpuSample("gpu-202", 0, "NVIDIA TITAN RTX", 100, 300, 24576, self.base),
                GpuSample("gpu-202", 1, "NVIDIA TITAN RTX", 100, 300, 24576, self.base),
                GpuSample("gpu-202", 2, "NVIDIA TITAN RTX", 0, 23000, 24576, self.base),
                GpuSample("gpu-202", 3, "NVIDIA TITAN RTX", 0, 23000, 24576, self.base),
            ],
            self.base,
        )
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].physical_total_count, 4)
        self.assertEqual(alerts[0].actionable_count, 2)
        self.assertEqual([gpu.gpu_index for gpu in alerts[0].confirmed_gpus], [0, 1])

    def test_unrelated_telemetry_host_never_alerts(self) -> None:  # 验证测试场景 `unrelated_telemetry_host_never_alerts` 的预期行为。
        evaluator = AvailabilityEvaluator(self.monitor, self.thresholds)
        alerts = evaluator.evaluate(
            [PlatformHost("gpu-202", 1, 1)],
            [GpuSample("gpu-176", 0, "A100", 0, 300, 40960, self.base)],
            self.base,
        )
        self.assertEqual(alerts, [])


if __name__ == "__main__":
    unittest.main()
