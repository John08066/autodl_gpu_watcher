from __future__ import annotations  # 自动开机模块的单元测试。

import unittest
from dataclasses import replace
from unittest.mock import Mock, call
from datetime import datetime, timedelta

from autodl_watcher.autostart import AutoStartCoordinator, is_instant_idle, select_target
from autodl_watcher.config import (
    AutoStartConfig,
    AutoStartTarget,
    IdleThresholds,
    MonitorConfig,
)
from autodl_watcher.models import AvailabilityAlert, ConfirmedGpu, GpuSample, PlatformHost


class AutoStartTest(unittest.TestCase):
    def setUp(self) -> None:  # 为当前测试用例准备共享配置、时间基准或测试对象。
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

    def _config(self, dry_run: bool = True) -> AutoStartConfig:  # 执行 `_config` 对应的内部处理逻辑。
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

    def test_selects_only_enabled_target_for_same_host(self) -> None:  # 验证测试场景 `selects_only_enabled_target_for_same_host` 的预期行为。
        self.assertEqual(select_target(self._config(), "gpu-202"), self.target_202)
        self.assertIsNone(select_target(self._config(), "gpu-203"))
        self.assertIsNone(select_target(self._config(), "gpu-201"))

    def test_instant_capacity_accepts_high_util_when_vram_is_enough(self) -> None:  # 验证测试场景 `instant_capacity_accepts_high_util_when_vram_is_enough` 的预期行为。
        now = datetime(2026, 7, 20, 16, 0, 0)
        high_util_good_vram = GpuSample("gpu-202", 0, "TITAN", 100, 2000, 24576, now)
        low_vram = GpuSample("gpu-202", 0, "TITAN", 0, 20000, 24576, now)
        stale = GpuSample( "gpu-202", 0, "TITAN", 0, 300, 24576, now - timedelta(seconds=100) )
        self.assertTrue(is_instant_idle(high_util_good_vram, self.thresholds, 90, now))
        self.assertFalse(is_instant_idle(low_vram, self.thresholds, 90, now))
        self.assertFalse(is_instant_idle(stale, self.thresholds, 90, now))

    def test_dry_run_never_calls_platform_api(self) -> None:  # 验证测试场景 `dry_run_never_calls_platform_api` 的预期行为。
        class FailIfCalled:
            def collect(self):  # 执行 `collect` 对应的内部处理逻辑。
                raise AssertionError("collect must not be called in dry-run")

            def post_api_json(self, path, payload):  # 执行 `post_api_json` 对应的内部处理逻辑。
                raise AssertionError("post_api_json must not be called in dry-run")

            def get_instance_state(self, instance_uuid, machine_name):
                raise AssertionError("get_instance_state must not be called in dry-run")

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

    def test_auto_selects_entry_with_more_current_free_slots(self) -> None:  # 验证测试场景 `auto_selects_entry_with_more_current_free_slots` 的预期行为。
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
        config = AutoStartConfig( True, "gpu-203", True, True, 0, 0, 1, (target_203_1, target_203_2), )
        slots = (("autodl-203-1", 0, 2), ("autodl-203-2", 1, 2))
        self.assertEqual( select_target(config, "gpu-203", platform_slots=slots), target_203_2, )

    def test_auto_tie_falls_back_to_priority(self) -> None:  # 验证测试场景 `auto_tie_falls_back_to_priority` 的预期行为。
        target_203_1 = AutoStartTarget("gpu-203", "autodl-203-1", "one", "gpu", 10, True)
        target_203_2 = AutoStartTarget("gpu-203", "autodl-203-2", "two", "gpu", 20, True)
        config = AutoStartConfig(True, "gpu-203", True, True, 0, 0, 1, (target_203_2, target_203_1))
        slots = (("autodl-203-1", 1, 2), ("autodl-203-2", 1, 2))
        self.assertEqual(select_target(config, "gpu-203", platform_slots=slots), target_203_1)


class NoGpuConversionTest(unittest.TestCase):
    def setUp(self):
        self.first = AutoStartTarget("gpu-203", "autodl-203-1", "instance-1", "gpu", 10, True)
        self.second = AutoStartTarget("gpu-203", "autodl-203-2", "instance-2", "gpu", 20, True)
        self.config = AutoStartConfig(True, "gpu-203", False, True, 0, -1, 1, (self.first, self.second))
        self.monitor = MonitorConfig(1, 0, 1, 2, 90, 1)
        self.thresholds = IdleThresholds(False, 100, 8192, 0.25)
        self.platform = Mock()
        self.telemetry = Mock()
        self.telemetry.collect.return_value = [
            GpuSample("gpu-203", 0, "Tesla V100", 0, 0, 32000, datetime.now())
        ]
        self.before = PlatformHost("gpu-203", 2, 2,
            ("autodl-203-1", "autodl-203-2"),
            (("autodl-203-1", 2, 2), ("autodl-203-2", 1, 2)))
        self.after = PlatformHost("gpu-203", 2, 2,
            ("autodl-203-1", "autodl-203-2"),
            (("autodl-203-1", 1, 2), ("autodl-203-2", 2, 2)))
        self.alert = AvailabilityAlert("gpu-203", 2, 2, 2, 1, (),
            ("autodl-203-1", "autodl-203-2"), self.before.source_slots)

    def _starter(self):
        return AutoStartCoordinator(self.config, self.monitor, self.thresholds,
                                    self.platform, self.telemetry, convert_no_gpu=True)

    def test_shutdown_then_confirm_same_uuid_and_power_on_gpu(self):
        self.platform.collect.side_effect = [[self.before], [self.after]]
        self.platform.get_instance_state.side_effect = [
            {"status": "running", "start_mode": "non_gpu", "host_account_gpu_clear": True},
            {"status": "shutting_down", "start_mode": "non_gpu", "host_account_gpu_clear": True},
            {"status": "shutdown", "start_mode": "non_gpu", "host_account_gpu_clear": True},
            {"status": "shutdown", "start_mode": "non_gpu", "host_account_gpu_clear": True},
        ]
        self.platform.post_api_json.return_value = {"code": "Success"}
        starter = self._starter()
        self.assertEqual(starter.attempt(self.alert).status, "shutdown_requested")
        self.assertEqual(starter.pending_switch, self.first)
        self.assertEqual(starter.continue_switch().status, "shutdown_pending")
        self.assertEqual(self.platform.post_api_json.call_count, 1)
        self.assertEqual(starter.continue_switch().status, "request_accepted")
        self.assertIsNone(starter.pending_switch)
        self.assertEqual(self.platform.post_api_json.call_args_list, [
            call("/api/v2/instance/power_off", {"instance_uuid": "instance-1", "release": "now"}),
            call("/api/v2/instance/power_on", {"instance_uuid": "instance-1", "start_mode": "gpu"}),
        ])

    def test_target_slot_lost_after_shutdown_never_uses_other_instance(self):
        lost = replace(self.after, source_slots=(("autodl-203-1", 0, 2), ("autodl-203-2", 2, 2)))
        self.platform.collect.side_effect = [[self.before], [lost]]
        self.platform.get_instance_state.side_effect = [
            {"status": "running", "start_mode": "non_gpu", "host_account_gpu_clear": True},
            {"status": "shutdown", "start_mode": "non_gpu", "host_account_gpu_clear": True},
        ]
        self.platform.post_api_json.return_value = {"code": "Success"}
        starter = self._starter()
        self.assertEqual(starter.attempt(self.alert).status, "shutdown_requested")
        self.assertEqual(starter.continue_switch().status, "recheck_failed")
        self.assertEqual(self.platform.post_api_json.call_count, 1)
        self.assertEqual(starter.pending_switch, self.first)

    def test_running_gpu_or_shutting_down_blocks_power_off_and_power_on(self):
        for status, mode in (("running", "gpu"), ("shutting_down", "non_gpu"), ("starting", "gpu")):
            with self.subTest(status=status):
                self.platform.reset_mock()
                self.platform.collect.return_value = [self.before]
                self.platform.get_instance_state.return_value = {"status": status, "start_mode": mode, "host_account_gpu_clear": True}
                starter = self._starter()
                self.assertEqual(starter.attempt(self.alert).status, "instance_state_blocked")
                self.platform.post_api_json.assert_not_called()

    def test_rejected_shutdown_and_stop_signal_fail_closed(self):
        self.platform.collect.return_value = [self.before]
        self.platform.get_instance_state.return_value = {"status": "running", "start_mode": "non_gpu", "host_account_gpu_clear": True}
        self.platform.post_api_json.return_value = {"code": "Failure", "msg": "busy"}
        starter = self._starter()
        self.assertEqual(starter.attempt(self.alert).status, "shutdown_failed")
        self.assertIsNone(starter.pending_switch)
        self.platform.post_api_json.reset_mock()
        self.assertEqual(starter.attempt(self.alert, stop_requested=lambda: True).status, "cancelled")
        self.platform.post_api_json.assert_not_called()

    def test_conflicting_status_after_shutdown_never_repeats_power_off(self):
        self.platform.collect.side_effect = [[self.before], [self.after]]
        self.platform.get_instance_state.side_effect = [
            {"status": "running", "start_mode": "non_gpu", "host_account_gpu_clear": True},
            {"status": "shutdown", "start_mode": "non_gpu", "host_account_gpu_clear": True},
            {"status": "running", "start_mode": "non_gpu", "host_account_gpu_clear": True},
        ]
        self.platform.post_api_json.return_value = {"code": "Success"}
        starter = self._starter()
        self.assertEqual(starter.attempt(self.alert).status, "shutdown_requested")
        self.assertEqual(starter.continue_switch().status, "instance_state_blocked")
        self.assertEqual(starter.pending_switch, self.first)
        self.assertEqual(self.platform.post_api_json.call_count, 1)

    def test_existing_gpu_start_clears_pending_without_second_power_on(self):
        self.platform.collect.return_value = [self.before]
        self.platform.get_instance_state.side_effect = [
            {"status": "running", "start_mode": "non_gpu", "host_account_gpu_clear": True},
            {"status": "starting", "start_mode": "gpu", "host_account_gpu_clear": True},
        ]
        self.platform.post_api_json.return_value = {"code": "Success"}
        starter = self._starter()
        self.assertEqual(starter.attempt(self.alert).status, "shutdown_requested")
        self.assertEqual(starter.continue_switch().status, "gpu_start_observed")
        self.assertIsNone(starter.pending_switch)
        self.assertEqual(self.platform.post_api_json.call_count, 1)

    def test_lost_shutdown_response_still_tracks_same_uuid(self):
        self.platform.collect.side_effect = [[self.before], [self.after]]
        self.platform.get_instance_state.side_effect = [
            {"status": "running", "start_mode": "non_gpu", "host_account_gpu_clear": True},
            {"status": "shutdown", "start_mode": "non_gpu", "host_account_gpu_clear": True},
            {"status": "shutdown", "start_mode": "non_gpu", "host_account_gpu_clear": True},
        ]
        self.platform.post_api_json.side_effect = [TimeoutError("response lost"), {"code": "Success"}]
        starter = self._starter()
        self.assertEqual(starter.attempt(self.alert).status, "shutdown_uncertain")
        self.assertEqual(starter.pending_switch, self.first)
        self.assertEqual(starter.continue_switch().status, "request_accepted")
        self.assertEqual(self.platform.post_api_json.call_args_list, [
            call("/api/v2/instance/power_off", {"instance_uuid": "instance-1", "release": "now"}),
            call("/api/v2/instance/power_on", {"instance_uuid": "instance-1", "start_mode": "gpu"}),
        ])

    def test_lost_gpu_start_response_does_not_repeat_power_on(self):
        self.platform.collect.side_effect = [[self.before], [self.after]]
        self.platform.get_instance_state.side_effect = [
            {"status": "running", "start_mode": "non_gpu", "host_account_gpu_clear": True},
            {"status": "shutdown", "start_mode": "non_gpu", "host_account_gpu_clear": True},
            {"status": "shutdown", "start_mode": "non_gpu", "host_account_gpu_clear": True},
            {"status": "shutdown", "start_mode": "non_gpu", "host_account_gpu_clear": True},
        ]
        self.platform.post_api_json.side_effect = [{"code": "Success"}, TimeoutError("response lost")]
        starter = self._starter()
        self.assertEqual(starter.attempt(self.alert).status, "shutdown_requested")
        with self.assertRaises(TimeoutError):
            starter.continue_switch()
        self.assertEqual(starter.continue_switch().status, "gpu_start_uncertain")
        self.assertEqual(self.platform.post_api_json.call_count, 2)
        self.assertEqual(starter.pending_switch, self.first)

    def test_restart_after_shutdown_can_start_fixed_instance(self):
        self.platform.collect.return_value = [self.before]
        self.platform.get_instance_state.return_value = {"status": "shutdown", "start_mode": "non_gpu", "host_account_gpu_clear": True}
        self.platform.post_api_json.return_value = {"code": "Success"}
        starter = self._starter()
        self.assertEqual(starter.attempt(self.alert).status, "request_accepted")
        self.platform.post_api_json.assert_called_once_with(
            "/api/v2/instance/power_on", {"instance_uuid": "instance-1", "start_mode": "gpu"})

    def test_other_account_gpu_appearing_after_shutdown_blocks_power_on(self):
        self.platform.collect.side_effect = [[self.before], [self.after]]
        self.platform.get_instance_state.side_effect = [
            {"status": "running", "start_mode": "non_gpu", "host_account_gpu_clear": True},
            {"status": "shutdown", "start_mode": "non_gpu", "host_account_gpu_clear": False},
            {"status": "shutdown", "start_mode": "non_gpu", "host_account_gpu_clear": False},
        ]
        self.platform.post_api_json.return_value = {"code": "Success"}
        starter = self._starter()
        self.assertEqual(starter.attempt(self.alert).status, "shutdown_requested")
        self.assertEqual(starter.continue_switch().status, "instance_state_blocked")
        self.assertEqual(self.platform.post_api_json.call_count, 1)
        self.assertEqual(starter.pending_switch, self.first)

    def test_other_account_gpu_or_missing_proof_blocks_state_change(self):
        self.platform.collect.return_value = [self.before]
        for proof in (False, None):
            with self.subTest(proof=proof):
                self.platform.post_api_json.reset_mock()
                self.platform.get_instance_state.return_value = {
                    "status": "running", "start_mode": "non_gpu", "host_account_gpu_clear": proof,
                }
                self.assertEqual(self._starter().attempt(self.alert).status, "instance_state_blocked")
                self.platform.post_api_json.assert_not_called()
        self.platform.get_instance_state.return_value = {
            "status": "shutdown", "start_mode": "non_gpu", "host_account_gpu_clear": False,
        }
        self.assertEqual(self._starter().attempt(self.alert).status, "instance_state_blocked")
        self.platform.post_api_json.assert_not_called()

    def test_dry_run_does_not_query_instance_or_send_state_change(self):
        starter = AutoStartCoordinator(replace(self.config, dry_run=True), self.monitor,
            self.thresholds, self.platform, self.telemetry, convert_no_gpu=True)
        self.assertEqual(starter.attempt(self.alert).status, "dry_run")
        self.platform.get_instance_state.assert_not_called()
        self.platform.post_api_json.assert_not_called()

    def test_regular_start_requires_confirmed_shutdown_and_matching_account(self):
        self.platform.collect.return_value = [self.before]
        self.platform.get_instance_state.return_value = {
            "status": "shutdown", "start_mode": "gpu", "host_account_gpu_clear": True,
        }
        self.platform.post_api_json.return_value = {"code": "Success"}
        starter = AutoStartCoordinator(self.config, self.monitor, self.thresholds,
                                       self.platform, self.telemetry)
        self.assertEqual(starter.attempt(self.alert).status, "request_accepted")
        self.platform.get_instance_state.assert_called_once_with("instance-1", "autodl-203-1")
        self.platform.post_api_json.assert_called_once_with(
            "/api/v2/instance/power_on", {"instance_uuid": "instance-1", "start_mode": "gpu"})

    def test_regular_start_blocks_running_or_other_account_gpu(self):
        self.platform.collect.return_value = [self.before]
        for status, proof in (("running", True), ("shutdown", False), ("shutdown", None)):
            with self.subTest(status=status, proof=proof):
                self.platform.post_api_json.reset_mock()
                self.platform.get_instance_state.return_value = {
                    "status": status, "start_mode": "non_gpu", "host_account_gpu_clear": proof,
                }
                starter = AutoStartCoordinator(self.config, self.monitor, self.thresholds,
                                               self.platform, self.telemetry)
                self.assertEqual(starter.attempt(self.alert).status, "instance_state_blocked")
                self.platform.post_api_json.assert_not_called()

    def test_regular_start_blocks_missing_or_duplicate_configured_uuid(self):
        from autodl_watcher.collectors import PlatformTransientError
        self.platform.collect.return_value = [self.before]
        for reason in ("目标 UUID 缺失", "目标 UUID 重复"):
            with self.subTest(reason=reason):
                self.platform.post_api_json.reset_mock()
                self.platform.get_instance_state.side_effect = PlatformTransientError(reason)
                starter = AutoStartCoordinator(self.config, self.monitor, self.thresholds,
                                               self.platform, self.telemetry)
                result = starter.attempt(self.alert)
                self.assertEqual(result.status, "instance_state_blocked")
                self.assertIn(reason, result.message)
                self.platform.post_api_json.assert_not_called()

    def test_regular_start_checks_instance_even_without_capacity_recheck(self):
        config = replace(self.config, verify_before_start=False)
        self.platform.get_instance_state.return_value = {
            "status": "shutdown", "start_mode": "gpu", "host_account_gpu_clear": True,
        }
        self.platform.post_api_json.return_value = {"code": "Success"}
        starter = AutoStartCoordinator(config, self.monitor, self.thresholds,
                                       self.platform, self.telemetry)
        self.assertEqual(starter.attempt(self.alert).status, "request_accepted")
        self.platform.get_instance_state.assert_called_once_with("instance-1", "autodl-203-1")
        self.platform.collect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
