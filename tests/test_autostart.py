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
from autodl_watcher.models import AvailabilityAlert, ConfirmedGpu, GpuSample, OccupancyRecord, PlatformHost


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


class InstanceStateMock(Mock):
    def _execute_mock_call(self, *args, **kwargs):
        value = super()._execute_mock_call(*args, **kwargs)
        self.last_state = value
        return value


class NoGpuConversionTest(unittest.TestCase):
    def setUp(self):
        self.first = AutoStartTarget("gpu-203", "autodl-203-1", "instance-1", "gpu", 10, True)
        self.second = AutoStartTarget("gpu-203", "autodl-203-2", "instance-2", "gpu", 20, True)
        self.config = AutoStartConfig(True, "gpu-203", False, True, 0, -1, 1, (self.first, self.second))
        self.monitor = MonitorConfig(1, 0, 1, 2, 90, 1)
        self.thresholds = IdleThresholds(False, 100, 8192, 0.25)
        self.platform = Mock()
        self.platform.get_instance_state = InstanceStateMock()
        self.platform.get_account_instances.side_effect = lambda **kwargs: [{
            "instance_uuid": self.platform.get_instance_state.call_args.args[0],
            "machine_name": self.platform.get_instance_state.call_args.args[1],
            **self.platform.get_instance_state.last_state,
        }]
        self.telemetry = Mock()
        self.platform.collect_occupancy.side_effect = lambda machine_name, *, expected_idle, expected_total: [
            OccupancyRecord(datetime.now(), "gpu-203", machine_name, index, f"gpu-{index}",
                            "Tesla V100", index >= expected_idle, "", "", "")
            for index in range(expected_total)
        ]
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

    def test_free_entry_and_ready_memory_on_different_gpu_blocks_shutdown(self):
        host = replace(self.before, free_count=1,
                       source_slots=(("autodl-203-1", 1, 2), ("autodl-203-2", 1, 2)))
        self.platform.collect.return_value = [host]
        self.telemetry.collect.return_value = [
            GpuSample("gpu-203", 0, "Tesla V100", 0, 30000, 32000, datetime.now()),
            GpuSample("gpu-203", 1, "Tesla V100", 0, 0, 32000, datetime.now()),
        ]
        self.platform.get_instance_state.return_value = {
            "status": "running", "start_mode": "non_gpu", "host_account_gpu_clear": True}
        result = self._starter().attempt(self.alert)
        self.assertEqual(result.status, "recheck_failed")
        self.platform.post_api_json.assert_not_called()

    def test_conversion_rechecks_indices_even_when_optional_recheck_is_disabled(self):
        host = replace(self.before, free_count=1,
                       source_slots=(("autodl-203-1", 1, 2), ("autodl-203-2", 1, 2)))
        self.platform.collect.return_value = [host]
        self.telemetry.collect.return_value = [
            GpuSample("gpu-203", 0, "Tesla V100", 0, 30000, 32000, datetime.now()),
            GpuSample("gpu-203", 1, "Tesla V100", 0, 0, 32000, datetime.now()),
        ]
        starter = AutoStartCoordinator(replace(self.config, verify_before_start=False), self.monitor,
                                       self.thresholds, self.platform, self.telemetry, convert_no_gpu=True)
        self.assertEqual(starter.attempt(self.alert).status, "recheck_failed")
        self.platform.collect.assert_called_once()
        self.platform.post_api_json.assert_not_called()

    def test_instance_recheck_failure_is_retryable_before_shutdown(self):
        self.platform.collect.return_value = [self.before]
        self.platform.get_instance_state.side_effect = TimeoutError("instance list timeout")
        starter = self._starter()
        self.assertEqual(starter.attempt(self.alert).status, "recheck_failed")
        self.assertIsNone(starter.pending_switch)
        self.platform.post_api_json.assert_not_called()

    def test_recheck_authentication_failure_still_requires_login(self):
        from autodl_watcher.collectors import PlatformAuthenticationError
        self.platform.collect.side_effect = PlatformAuthenticationError("session expired")
        with self.assertRaises(PlatformAuthenticationError):
            self._starter().attempt(self.alert)
        self.platform.post_api_json.assert_not_called()

    def test_occupancy_failure_blocks_shutdown(self):
        self.platform.collect.return_value = [self.before]
        self.platform.collect_occupancy.side_effect = RuntimeError("popup unavailable")
        self.assertEqual(self._starter().attempt(self.alert).status, "recheck_failed")
        self.platform.post_api_json.assert_not_called()

    def test_free_entry_and_ready_memory_on_different_gpu_blocks_power_on(self):
        after = replace(self.after, free_count=1,
                        source_slots=(("autodl-203-1", 1, 2), ("autodl-203-2", 1, 2)))
        self.platform.collect.side_effect = [[self.before], [after]]
        self.telemetry.collect.side_effect = [
            [GpuSample("gpu-203", 0, "Tesla V100", 0, 0, 32000, datetime.now())],
            [GpuSample("gpu-203", 1, "Tesla V100", 0, 0, 32000, datetime.now())],
        ]
        self.platform.get_instance_state.side_effect = [
            {"status": "running", "start_mode": "non_gpu", "host_account_gpu_clear": True},
            {"status": "shutdown", "start_mode": "non_gpu", "host_account_gpu_clear": True},
        ]
        self.platform.post_api_json.return_value = {"code": "Success"}
        starter = self._starter()
        self.assertEqual(starter.attempt(self.alert).status, "shutdown_requested")
        self.assertEqual(starter.continue_switch().status, "recheck_failed")
        self.assertEqual(self.platform.post_api_json.call_args_list, [
            call("/api/v2/instance/power_off", {"instance_uuid": "instance-1", "release": "now"})])

    def test_shutdown_then_confirm_same_uuid_and_power_on_gpu(self):
        self.platform.collect.side_effect = [[self.before], [self.after]]
        self.platform.get_instance_state.side_effect = [
            {"status": "running", "start_mode": "non_gpu", "host_account_gpu_clear": True},
            {"status": "shutting_down", "start_mode": "non_gpu", "host_account_gpu_clear": True},
            {"status": "shutdown", "start_mode": "non_gpu", "host_account_gpu_clear": True},
            {"status": "shutdown", "start_mode": "non_gpu", "host_account_gpu_clear": True},
            {"status": "shutdown", "start_mode": "non_gpu", "host_account_gpu_clear": True}
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

    def test_starting_gpu_stays_pending_and_running_clears_without_second_power_on(self):
        self.platform.collect.return_value = [self.before]
        self.platform.get_instance_state.side_effect = [
            {"status": "running", "start_mode": "non_gpu", "host_account_gpu_clear": True},
            {"status": "starting", "start_mode": "gpu", "host_account_gpu_clear": True},
            {"status": "running", "start_mode": "gpu", "host_account_gpu_clear": True},
        ]
        self.platform.post_api_json.return_value = {"code": "Success"}
        starter = self._starter()
        self.assertEqual(starter.attempt(self.alert).status, "shutdown_requested")
        self.assertEqual(starter.continue_switch().status, "gpu_start_pending")
        self.assertEqual(starter.pending_switch, self.first)
        self.assertEqual(starter.continue_switch().status, "gpu_start_observed")
        self.assertIsNone(starter.pending_switch)
        self.assertEqual(self.platform.post_api_json.call_count, 1)

    def test_lost_shutdown_response_still_tracks_same_uuid(self):
        self.platform.collect.side_effect = [[self.before], [self.after]]
        self.platform.get_instance_state.side_effect = [
            {"status": "running", "start_mode": "non_gpu", "host_account_gpu_clear": True},
            {"status": "shutdown", "start_mode": "non_gpu", "host_account_gpu_clear": True},
            {"status": "shutdown", "start_mode": "non_gpu", "host_account_gpu_clear": True},
            {"status": "shutdown", "start_mode": "non_gpu", "host_account_gpu_clear": True}
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
            {"status": "shutdown", "start_mode": "non_gpu", "host_account_gpu_clear": True}
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

    def test_quota_rejection_blocks_further_regular_gpu_requests_until_restart(self):
        self.platform.collect.return_value = [self.before]
        self.platform.get_instance_state.return_value = {
            "status": "shutdown", "start_mode": "gpu", "host_account_gpu_clear": True}
        self.platform.post_api_json.return_value = {
            "code": "GpuStockReqNum", "msg": "tenant quota exceeded"}
        starter = AutoStartCoordinator(self.config, self.monitor, self.thresholds,
                                       self.platform, self.telemetry)
        blocked = starter.attempt(self.alert)
        self.assertEqual(blocked.status, "quota_blocked")
        self.assertEqual(blocked.instance_uuid, self.first.instance_uuid)
        self.assertEqual(blocked.api_code, "GpuStockReqNum")
        self.assertEqual(blocked.api_msg, "tenant quota exceeded")
        self.assertIn("额度不足", blocked.message)
        for _ in range(3):
            self.assertEqual(starter.attempt(self.alert, target_override=self.second), blocked)
            self.assertEqual(starter.continue_switch(), blocked)
        self.platform.post_api_json.assert_called_once_with(
            "/api/v2/instance/power_on", {"instance_uuid": self.first.instance_uuid, "start_mode": "gpu"})
        self.platform.post_api_json.reset_mock()
        self.platform.post_api_json.return_value = {"code": "Success"}
        restarted = AutoStartCoordinator(self.config, self.monitor, self.thresholds,
                                         self.platform, self.telemetry)
        self.assertEqual(restarted.attempt(self.alert).status, "request_accepted")  # 显式重启监控才建立新协调器。
        self.platform.post_api_json.assert_called_once()

    def test_quota_rejection_keeps_fixed_conversion_blocked_without_repeat_power_on(self):
        self.platform.collect.return_value = [self.before]
        self.platform.get_instance_state.side_effect = [
            {"status": "running", "start_mode": "non_gpu", "host_account_gpu_clear": True},
            {"status": "shutdown", "start_mode": "non_gpu", "host_account_gpu_clear": True},
            {"status": "shutdown", "start_mode": "non_gpu", "host_account_gpu_clear": True},
            {"status": "shutdown", "start_mode": "non_gpu", "host_account_gpu_clear": True}
        ]
        self.platform.post_api_json.side_effect = [
            {"code": "Success"}, {"code": "GpuStockReqNum", "msg": "tenant quota exceeded"}]
        starter = self._starter()
        self.assertEqual(starter.attempt(self.alert).status, "shutdown_requested")
        blocked = starter.continue_switch()
        self.assertEqual(blocked.status, "quota_blocked")
        self.assertEqual(blocked.instance_uuid, self.first.instance_uuid)
        self.assertEqual(blocked.api_code, "GpuStockReqNum")
        self.assertEqual(blocked.api_msg, "tenant quota exceeded")
        for _ in range(3):
            self.assertEqual(starter.continue_switch(), blocked)
            self.assertEqual(starter.attempt(self.alert), blocked)
        self.assertEqual(starter.pending_switch, self.first)
        self.assertEqual(self.platform.post_api_json.call_args_list, [
            call("/api/v2/instance/power_off", {"instance_uuid": self.first.instance_uuid, "release": "now"}),
            call("/api/v2/instance/power_on", {"instance_uuid": self.first.instance_uuid, "start_mode": "gpu"})])

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

    def test_regular_start_rechecks_matching_gpu_indices(self):
        host = replace(self.before, free_count=1,
                       source_slots=(("autodl-203-1", 1, 2), ("autodl-203-2", 1, 2)))
        self.platform.collect.return_value = [host]
        self.telemetry.collect.return_value = [
            GpuSample("gpu-203", 1, "Tesla V100", 0, 0, 32000, datetime.now())]
        self.platform.get_instance_state.return_value = {
            "status": "shutdown", "start_mode": "gpu", "host_account_gpu_clear": True}
        starter = AutoStartCoordinator(self.config, self.monitor, self.thresholds,
                                       self.platform, self.telemetry)
        self.assertEqual(starter.attempt(self.alert).status, "recheck_failed")
        self.platform.post_api_json.assert_not_called()

    def test_a100_four_gpu_conversion_uses_same_target_and_capacity_ratio(self):
        target = AutoStartTarget("gpu-201", "autodl-201-1", "a100-instance", "gpu", 10, True)
        config = replace(self.config, default_host="gpu-201", targets=(target,))
        host = PlatformHost("gpu-201", 2, 4, (target.machine_name,), ((target.machine_name, 2, 4),))
        alert = AvailabilityAlert("gpu-201", 2, 4, 4, 1, (), (target.machine_name,), host.source_slots)
        self.platform.collect.return_value = [host]
        self.platform.collect_occupancy.side_effect = None
        self.platform.collect_occupancy.return_value = [
            OccupancyRecord(datetime.now(), "gpu-201", target.machine_name, index, f"a100-{index}",
                            "NVIDIA A100-SXM4-40GB", index >= 2, "", "", "")
            for index in range(4)]
        self.telemetry.collect.return_value = [
            GpuSample("gpu-201", 0, "NVIDIA A100-SXM4-40GB", 0, 32000, 40960, datetime.now())]
        self.platform.get_instance_state.side_effect = [
            {"status": "running", "start_mode": "non_gpu", "host_account_gpu_clear": True},
            {"status": "shutdown", "start_mode": "non_gpu", "host_account_gpu_clear": True},
            {"status": "shutdown", "start_mode": "non_gpu", "host_account_gpu_clear": True},
            {"status": "shutdown", "start_mode": "non_gpu", "host_account_gpu_clear": True}
        ]
        self.platform.post_api_json.return_value = {"code": "Success"}
        starter = AutoStartCoordinator(config, self.monitor, self.thresholds,
                                       self.platform, self.telemetry, convert_no_gpu=True)
        self.assertEqual(starter.attempt(alert).status, "recheck_failed")  # 8960MB低于A100的25%门槛，不能关机。
        self.platform.post_api_json.assert_not_called()
        self.telemetry.collect.return_value = [
            GpuSample("gpu-201", 0, "NVIDIA A100-SXM4-40GB", 0, 30000, 40960, datetime.now())]
        self.assertEqual(starter.attempt(alert).status, "shutdown_requested")
        self.assertEqual(starter.continue_switch().status, "request_accepted")
        self.assertEqual(self.platform.post_api_json.call_args_list, [
            call("/api/v2/instance/power_off", {"instance_uuid": target.instance_uuid, "release": "now"}),
            call("/api/v2/instance/power_on", {"instance_uuid": target.instance_uuid, "start_mode": "gpu"})])

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

    def test_regular_gpu_start_requires_capacity_even_when_optional_recheck_disabled(self):
        config = replace(self.config, verify_before_start=False)
        self.platform.collect.return_value = [self.before]
        self.platform.get_instance_state.return_value = {
            "status": "shutdown", "start_mode": "gpu", "host_account_gpu_clear": True,
        }
        self.platform.post_api_json.return_value = {"code": "Success"}
        starter = AutoStartCoordinator(config, self.monitor, self.thresholds,
                                       self.platform, self.telemetry)
        self.assertEqual(starter.attempt(self.alert).status, "request_accepted")
        self.platform.get_instance_state.assert_called_once_with("instance-1", "autodl-203-1")
        self.platform.collect.assert_called_once()
        self.platform.collect_occupancy.assert_called_once()


class AccountNoGpuReleaseTest(unittest.TestCase):
    def setUp(self):
        NoGpuConversionTest.setUp(self)
        self.states = {
            "instance-1": {"machine_name": "autodl-203-1", "status": "shutdown", "start_mode": "gpu"},
            "other-201": {"machine_name": "autodl-201-1", "status": "running", "start_mode": "non_gpu"},
            "other-202": {"machine_name": "autodl-202-4", "status": "running", "start_mode": "non_gpu"},
            "gpu-203": {"machine_name": "autodl-203-2", "status": "running", "start_mode": "gpu"},
        }
        self.platform.collect.return_value = [self.before]
        self.platform.get_account_instances.side_effect = self._rows
        self.platform.get_instance_state.side_effect = self._state
        self.platform.post_api_json.side_effect = self._power

    def _rows(self, **kwargs):
        return [{"instance_uuid": uuid, **state} for uuid, state in self.states.items()]

    def _state(self, uuid, name):
        state = self.states[uuid]
        if state["machine_name"] != name:
            raise ValueError("UUID 与入口不一致")
        return {"status": state["status"], "start_mode": state["start_mode"], "host_account_gpu_clear": True}

    def _power(self, path, payload):
        state = self.states[payload["instance_uuid"]]
        state["status"] = "shutting_down" if path.endswith("power_off") else "starting"
        if path.endswith("power_on"):
            state["start_mode"] = payload["start_mode"]
        return {"code": "Success"}

    def _starter(self):
        return AutoStartCoordinator(self.config, self.monitor, self.thresholds,
                                    self.platform, self.telemetry, convert_no_gpu=True)

    def test_releases_unconfigured_other_hosts_then_starts_original_shutdown_target(self):
        starter = self._starter()
        self.assertEqual(starter.attempt(self.alert).status, "shutdown_requested")
        self.assertEqual(self.platform.post_api_json.call_args_list, [
            call("/api/v2/instance/power_off", {"instance_uuid": "other-201", "release": "now"}),
            call("/api/v2/instance/power_off", {"instance_uuid": "other-202", "release": "now"}),
        ])
        self.assertEqual(starter.pending_switch, self.first)
        self.states["other-201"]["status"] = "shutdown"
        self.assertEqual(starter.continue_switch().status, "shutdown_pending")
        self.assertEqual(self.platform.post_api_json.call_count, 2)
        self.states["other-202"]["status"] = "shutdown"
        self.assertEqual(starter.continue_switch().status, "request_accepted")
        self.assertEqual(self.platform.post_api_json.call_args_list[-1],
            call("/api/v2/instance/power_on", {"instance_uuid": "instance-1", "start_mode": "gpu"}))
        self.assertEqual(self.states["gpu-203"]["status"], "running")

    def test_running_no_gpu_target_waits_for_other_no_gpu_shutdown_then_converts(self):
        self.states["instance-1"].update(status="running", start_mode="non_gpu")
        starter = self._starter()
        self.assertEqual(starter.attempt(self.alert).status, "shutdown_requested")
        self.states["other-201"]["status"] = self.states["other-202"]["status"] = "shutdown"
        self.assertEqual(starter.continue_switch().status, "shutdown_requested")
        self.assertEqual(self.platform.post_api_json.call_args_list[-1],
            call("/api/v2/instance/power_off", {"instance_uuid": "instance-1", "release": "now"}))
        self.states["instance-1"]["status"] = "shutdown"
        self.assertEqual(starter.continue_switch().status, "request_accepted")

    def test_lost_other_shutdown_response_is_never_repeated(self):
        def lost_response(path, payload):
            if path.endswith("power_off"):
                raise TimeoutError("response lost")
            return self._power(path, payload)
        self.platform.post_api_json.side_effect = lost_response
        starter = self._starter()
        self.assertEqual(starter.attempt(self.alert).status, "shutdown_uncertain")
        for _ in range(3):
            self.assertIn(starter.continue_switch().status, ("shutdown_pending", "shutdown_uncertain"))
        offs = [item.args[1]["instance_uuid"] for item in self.platform.post_api_json.call_args_list
                if item.args[0].endswith("power_off")]
        self.assertEqual(len(offs), len(set(offs)))
        self.assertFalse(any(item.args[0].endswith("power_on") for item in self.platform.post_api_json.call_args_list))

    def test_other_instance_mode_change_before_off_blocks_without_power_request(self):
        def changed_state(uuid, name):
            if uuid == "other-201":
                self.states[uuid]["start_mode"] = "gpu"
            return self._state(uuid, name)
        self.platform.get_instance_state.side_effect = changed_state
        self.assertEqual(self._starter().attempt(self.alert).status, "instance_state_blocked")
        self.platform.post_api_json.assert_not_called()

    def test_stop_during_other_instance_recheck_prevents_power_request(self):
        stopped = False
        def request_stop(uuid, name):
            nonlocal stopped
            if uuid == "other-201":
                stopped = True
            return self._state(uuid, name)
        self.platform.get_instance_state.side_effect = request_stop
        self.assertEqual(self._starter().attempt(self.alert, lambda: stopped).status, "cancelled")
        self.platform.post_api_json.assert_not_called()

    def test_capacity_lost_after_all_other_shutdown_blocks_gpu_start(self):
        starter = self._starter()
        self.assertEqual(starter.attempt(self.alert).status, "shutdown_requested")
        self.states["other-201"]["status"] = self.states["other-202"]["status"] = "shutdown"
        self.platform.collect.return_value = [replace(self.before, free_count=0,
            source_slots=(("autodl-203-1", 0, 2), ("autodl-203-2", 0, 2)))]
        self.assertEqual(starter.continue_switch().status, "recheck_failed")
        self.assertEqual(self.platform.post_api_json.call_count, 2)

    def test_missing_previously_shutdown_instance_blocks_gpu_start(self):
        starter = self._starter()
        self.assertEqual(starter.attempt(self.alert).status, "shutdown_requested")
        del self.states["other-201"]
        self.states["other-202"]["status"] = "shutdown"
        self.assertEqual(starter.continue_switch().status, "instance_state_blocked")
        self.assertEqual(self.platform.post_api_json.call_count, 2)

    def test_new_no_gpu_instance_during_pending_is_rechecked_and_released(self):
        starter = self._starter()
        self.assertEqual(starter.attempt(self.alert).status, "shutdown_requested")
        self.states["late-instance"] = {"machine_name": "autodl-202-2", "status": "running", "start_mode": "non_gpu"}
        self.assertEqual(starter.continue_switch().status, "shutdown_requested")
        self.assertEqual(self.platform.post_api_json.call_args_list[-1],
            call("/api/v2/instance/power_off", {"instance_uuid": "late-instance", "release": "now"}))
        for uuid in ("other-201", "other-202", "late-instance"):
            self.states[uuid]["status"] = "shutdown"
        self.assertEqual(starter.continue_switch().status, "request_accepted")
        self.platform.get_account_instances.assert_called_with(require_personal_scope=True)

    def test_restarted_other_no_gpu_instance_is_not_sent_second_shutdown(self):
        starter = self._starter()
        self.assertEqual(starter.attempt(self.alert).status, "shutdown_requested")
        self.states["other-201"]["status"] = "running"
        self.states["other-202"]["status"] = "shutdown"
        for _ in range(3):
            self.assertEqual(starter.continue_switch().status, "shutdown_pending")
        self.assertEqual(self.platform.post_api_json.call_count, 2)

    def test_unknown_account_row_blocks_every_power_request(self):
        for field, value in (("status", "unrecognized"), ("start_mode", ""), ("start_mode", "unknown")):
            with self.subTest(field=field, value=value):
                self.states["other-201"][field] = value
                self.assertEqual(self._starter().attempt(self.alert).status, "instance_state_blocked")
                self.platform.post_api_json.assert_not_called()
                self.states["other-201"].update(status="running", start_mode="non_gpu")

    def test_identity_change_before_other_shutdown_blocks_request(self):
        def renamed_state(uuid, name):
            if uuid == "other-201":
                self.states[uuid]["machine_name"] = "autodl-201-2"
            return self._state(uuid, name)
        self.platform.get_instance_state.side_effect = renamed_state
        self.assertEqual(self._starter().attempt(self.alert).status, "instance_state_blocked")
        self.platform.post_api_json.assert_not_called()

    def test_rejected_other_shutdown_remains_pending_without_repeat(self):
        self.platform.post_api_json.side_effect = None
        self.platform.post_api_json.return_value = {"code": "Failure", "msg": "busy"}
        starter = self._starter()
        self.assertEqual(starter.attempt(self.alert).status, "shutdown_failed")
        self.assertEqual(starter.continue_switch().status, "shutdown_failed")
        for _ in range(3):
            self.assertEqual(starter.continue_switch().status, "shutdown_pending")
        self.assertEqual(self.platform.post_api_json.call_count, 2)

    def test_ordinary_scope_verification_failure_never_releases_instances(self):
        from autodl_watcher.collectors import PlatformTransientError
        self.platform.get_account_instances.side_effect = PlatformTransientError("账号个人范围无法确认")
        self.assertEqual(self._starter().attempt(self.alert).status, "instance_state_blocked")
        self.platform.get_account_instances.assert_called_with(require_personal_scope=True)
        self.platform.post_api_json.assert_not_called()

    def test_capacity_failure_does_not_shutdown_other_hosts(self):
        self.telemetry.collect.return_value = [GpuSample("gpu-203", 0, "Tesla V100", 0, 31900, 32000, datetime.now())]
        self.assertEqual(self._starter().attempt(self.alert).status, "recheck_failed")
        self.platform.get_account_instances.assert_not_called()
        self.platform.post_api_json.assert_not_called()

    def test_quota_rejection_after_release_never_restarts_power_sequence(self):
        starter = self._starter()
        self.assertEqual(starter.attempt(self.alert).status, "shutdown_requested")
        self.states["other-201"]["status"] = self.states["other-202"]["status"] = "shutdown"
        self.platform.post_api_json.side_effect = None
        self.platform.post_api_json.return_value = {"code": "GpuStockReqNum", "msg": "tenant quota exceeded"}
        rejected = starter.continue_switch()
        self.assertEqual(rejected.status, "quota_blocked")
        for _ in range(3):
            self.assertEqual(starter.continue_switch(), rejected)
            self.assertEqual(starter.attempt(self.alert), rejected)
        self.assertEqual(self.platform.post_api_json.call_count, 3)

    def test_late_no_gpu_waits_for_target_shutdown_and_fresh_capacity_before_release(self):
        for capacity_available in (False, True):
            with self.subTest(capacity_available=capacity_available):
                self.setUp()
                self.states["other-201"]["status"] = self.states["other-202"]["status"] = "shutdown"
                self.states["instance-1"].update(status="running", start_mode="non_gpu")
                starter = self._starter()
                self.assertEqual(starter.attempt(self.alert).status, "shutdown_requested")
                self.states["late-instance"] = {"machine_name": "autodl-202-2", "status": "running", "start_mode": "non_gpu"}
                self.assertEqual(starter.continue_switch().status, "shutdown_pending")
                self.assertEqual(self.platform.post_api_json.call_count, 1)  # 旧目标尚未关机，仅等待，不额外中断新实例。
                self.states["instance-1"]["status"] = "shutdown"
                if not capacity_available:
                    self.platform.collect.return_value = [replace(self.before, free_count=0,
                        source_slots=(("autodl-203-1", 0, 2), ("autodl-203-2", 0, 2)))]
                    self.assertEqual(starter.continue_switch().status, "recheck_failed")
                    self.assertEqual(self.platform.post_api_json.call_count, 1)
                else:
                    self.assertEqual(starter.continue_switch().status, "shutdown_requested")
                    self.assertEqual(self.platform.post_api_json.call_args_list[-1],
                        call("/api/v2/instance/power_off", {"instance_uuid": "late-instance", "release": "now"}))
                    self.assertEqual(self.platform.post_api_json.call_count, 2)

    def test_waiting_other_no_gpu_transition_locks_target_then_releases_running_instance(self):
        for status in ("starting", "shutting_down"):
            with self.subTest(status=status):
                self.setUp()
                self.states["other-201"]["status"] = status
                self.states["other-202"]["status"] = "shutdown"
                starter = self._starter()
                self.assertEqual(starter.attempt(self.alert).status, "shutdown_pending")
                self.assertEqual(starter.pending_switch, self.first)
                self.platform.post_api_json.assert_not_called()
                self.states["other-201"]["status"] = "running"
                self.assertEqual(starter.continue_switch().status, "shutdown_requested")
                self.platform.post_api_json.assert_called_once_with(
                    "/api/v2/instance/power_off", {"instance_uuid": "other-201", "release": "now"})

    def test_initial_shutdown_target_lost_gpu_response_keeps_lock_without_repeat(self):
        for response_lost in (True, False):
            with self.subTest(response_lost=response_lost):
                self.setUp()
                self.states["other-201"]["status"] = self.states["other-202"]["status"] = "shutdown"
                self.platform.post_api_json.side_effect = TimeoutError("power_on response lost") if response_lost else None
                self.platform.post_api_json.return_value = {"code": "Success"}
                starter = self._starter()
                if response_lost:
                    with self.assertRaises(TimeoutError):
                        starter.attempt(self.alert)
                    self.assertEqual(starter.pending_switch, self.first)
                    for _ in range(3):
                        self.assertEqual(starter.continue_switch().status, "gpu_start_uncertain")
                    self.assertEqual(starter.pending_switch, self.first)
                else:
                    self.assertEqual(starter.attempt(self.alert).status, "request_accepted")
                    self.assertIsNone(starter.pending_switch)
                    self.assertIsNone(starter.continue_switch())
                self.platform.post_api_json.assert_called_once_with(
                    "/api/v2/instance/power_on", {"instance_uuid": "instance-1", "start_mode": "gpu"})


if __name__ == "__main__":
    unittest.main()
