"""
自动开机模块的单元测试。

测试覆盖：
    - 入口选择：按平台空位、优先级排序
    - 即时空闲判定：显存条件、过期处理
    - Dry-run 模式：不调用平台 API
    - 入口空位相同时的优先级回退
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from autodl_watcher.autostart import AutoStartCoordinator, is_instant_idle, select_target
from autodl_watcher.config import (
    AutoStartConfig,
    AutoStartTarget,
    IdleThresholds,
    MonitorConfig,
)
from autodl_watcher.models import AvailabilityAlert, ConfirmedGpu, GpuSample


class AutoStartTest(unittest.TestCase):
    def setUp(self) -> None:
        """功能：
            为当前测试用例准备共享配置、时间基准或测试对象。

        参数：
            无。

        返回：
            None：函数通过副作用完成初始化、输出、持久化或资源管理。
        """
        self.monitor = MonitorConfig(
            poll_seconds=10,
            confirmation_seconds=0,
            min_idle_samples=1,
            max_sample_gap_seconds=20,
            stale_after_seconds=90,
            reset_busy_samples=1,
        )
        self.thresholds = IdleThresholds(False, 100, 8192, 0.25)
        self.target_202 = AutoStartTarget(
            host="gpu-202",
            machine_name="autodl-202-2",
            instance_uuid="3ed243ad9c-d028a55d",
            start_mode="gpu",
            priority=10,
            enabled=True,
        )
        self.target_203_disabled = AutoStartTarget(
            host="gpu-203",
            machine_name="autodl-203-1",
            instance_uuid="disabled",
            start_mode="gpu",
            priority=1,
            enabled=False,
        )

    def _config(self, dry_run: bool = True) -> AutoStartConfig:
        """功能：
            执行 `_config` 对应的内部处理逻辑。

        参数：
            dry_run (bool)：是否只模拟判定而不真正发送开机请求。

        返回：
            AutoStartConfig：函数计算或构造出的结果。
        """
        return AutoStartConfig(
            enabled=True,
            default_host="gpu-203",
            dry_run=dry_run,
            verify_before_start=True,
            recheck_delay_seconds=0,
            post_start_check_seconds=0,
            max_starts_per_event=1,
            targets=(self.target_203_disabled, self.target_202),
        )

    def test_selects_only_enabled_target_for_same_host(self) -> None:
        """功能：
            验证测试场景 `selects_only_enabled_target_for_same_host` 的预期行为。

        参数：
            无。

        返回：
            None：无返回值。
        """
        self.assertEqual(select_target(self._config(), "gpu-202"), self.target_202)
        self.assertIsNone(select_target(self._config(), "gpu-203"))
        self.assertIsNone(select_target(self._config(), "gpu-201"))

    def test_instant_capacity_accepts_high_util_when_vram_is_enough(self) -> None:
        """功能：
            验证测试场景 `instant_capacity_accepts_high_util_when_vram_is_enough` 的预期行为。

        参数：
            无。

        返回：
            None：无返回值。
        """
        now = datetime(2026, 7, 20, 16, 0, 0)
        high_util_good_vram = GpuSample("gpu-202", 0, "TITAN", 100, 2000, 24576, now)
        low_vram = GpuSample("gpu-202", 0, "TITAN", 0, 20000, 24576, now)
        stale = GpuSample(
            "gpu-202", 0, "TITAN", 0, 300, 24576, now - timedelta(seconds=100)
        )
        self.assertTrue(is_instant_idle(high_util_good_vram, self.thresholds, 90, now))
        self.assertFalse(is_instant_idle(low_vram, self.thresholds, 90, now))
        self.assertFalse(is_instant_idle(stale, self.thresholds, 90, now))

    def test_dry_run_never_calls_platform_api(self) -> None:
        """功能：
            验证测试场景 `dry_run_never_calls_platform_api` 的预期行为。

        参数：
            无。

        返回：
            None：无返回值。
        """
        class FailIfCalled:
            def collect(self):
                """功能：
                    执行 `collect` 对应的内部处理逻辑。

                参数：
                    无。

                返回：
                    未显式标注：本轮采集得到的平台主机列表或物理 GPU 样本列表。
                """
                raise AssertionError("collect must not be called in dry-run")

            def post_api_json(self, path, payload):
                """功能：
                    执行 `post_api_json` 对应的内部处理逻辑。

                参数：
                    path (未显式标注)：文件路径、API 相对路径或目标输出路径，具体含义由函数上下文决定。
                    payload (未显式标注)：待解析的 JSON 字典，或发送给 AutoDL API 的请求体。

                返回：
                    未显式标注：AutoDL API 返回的 JSON 字典。
                """
                raise AssertionError("post_api_json must not be called in dry-run")

        confirmed = ConfirmedGpu(
            host="gpu-202",
            gpu_index=1,
            gpu_name="NVIDIA TITAN RTX",
            util_pct=100,
            memory_used_mb=300,
            memory_total_mb=24576,
            idle_seconds=0,
            observed_at=datetime(2026, 7, 20, 16, 0, 0),
        )
        alert = AvailabilityAlert(
            host="gpu-202",
            platform_free_count=1,
            platform_total_count=3,
            physical_total_count=3,
            actionable_count=1,
            confirmed_gpus=(confirmed,),
            platform_sources=("autodl-202-2",),
        )
        coordinator = AutoStartCoordinator(
            self._config(dry_run=True),
            self.monitor,
            self.thresholds,
            FailIfCalled(),
            FailIfCalled(),
        )
        result = coordinator.attempt(alert)
        self.assertEqual(result.status, "dry_run")
        self.assertEqual(result.instance_uuid, "3ed243ad9c-d028a55d")

    def test_auto_selects_entry_with_more_current_free_slots(self) -> None:
        """功能：
            验证测试场景 `auto_selects_entry_with_more_current_free_slots` 的预期行为。

        参数：
            无。

        返回：
            None：无返回值。
        """
        target_203_1 = AutoStartTarget(
            host="gpu-203",
            machine_name="autodl-203-1",
            instance_uuid="instance-1",
            start_mode="gpu",
            priority=10,
            enabled=True,
        )
        target_203_2 = AutoStartTarget(
            host="gpu-203",
            machine_name="autodl-203-2",
            instance_uuid="instance-2",
            start_mode="gpu",
            priority=20,
            enabled=True,
        )
        config = AutoStartConfig(
            True, "gpu-203", True, True, 0, 0, 1,
            (target_203_1, target_203_2),
        )
        slots = (("autodl-203-1", 0, 2), ("autodl-203-2", 1, 2))
        self.assertEqual(
            select_target(config, "gpu-203", platform_slots=slots),
            target_203_2,
        )

    def test_auto_tie_falls_back_to_priority(self) -> None:
        """功能：
            验证测试场景 `auto_tie_falls_back_to_priority` 的预期行为。

        参数：
            无。

        返回：
            None：无返回值。
        """
        target_203_1 = AutoStartTarget("gpu-203", "autodl-203-1", "one", "gpu", 10, True)
        target_203_2 = AutoStartTarget("gpu-203", "autodl-203-2", "two", "gpu", 20, True)
        config = AutoStartConfig(True, "gpu-203", True, True, 0, 0, 1, (target_203_2, target_203_1))
        slots = (("autodl-203-1", 1, 2), ("autodl-203-2", 1, 2))
        self.assertEqual(select_target(config, "gpu-203", platform_slots=slots), target_203_1)


if __name__ == "__main__":
    unittest.main()
