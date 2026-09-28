from __future__ import annotations  # 通知格式化的单元测试。

import unittest
from datetime import datetime

from autodl_watcher.autostart import StartAttemptResult, format_start_result
from autodl_watcher.main import (
    _owned_instances_from_records,
)
from autodl_watcher.models import AvailabilityAlert, ConfirmedGpu, OccupancyRecord
from autodl_watcher.notifiers.formatting import format_alert


class FormattingTest(unittest.TestCase):
    def test_user_visible_terms_use_gpu_index_and_start_ready(self) -> None:  # 验证测试场景 `user_visible_terms_use_gpu_index_and_start_ready` 的预期行为。
        gpu = ConfirmedGpu(
            host="gpu-203",
            gpu_index=0,
            gpu_name="Tesla V100",
            util_pct=99,
            memory_used_mb=11056,
            memory_total_mb=32768,
            idle_seconds=0,
            observed_at=datetime(2026, 7, 20, 20, 44, 53),
        )
        alert = AvailabilityAlert(
            host="gpu-203",
            platform_free_count=2,
            platform_total_count=2,
            physical_total_count=2,
            actionable_count=1,
            confirmed_gpus=(gpu,),
            platform_sources=("autodl-203-1", "autodl-203-2"),
        )
        text = format_alert(alert)
        self.assertIn("AutoDL 开机达标", text)
        self.assertIn("GPU INDEX：#0", text)
        self.assertNotIn("告警", text)

    def test_start_result_displays_platform_gpu_ids(self) -> None:  # 验证测试场景 `start_result_displays_platform_gpu_ids` 的预期行为。
        result = StartAttemptResult(
            status="request_accepted",
            host="gpu-202",
            instance_uuid="instance",
            machine_name="autodl-202-2",
            message="ok",
            api_code="Success",
            platform_free_before=3,
            platform_total_before=3,
            platform_free_after=2,
            platform_total_after=3,
        )
        text = format_start_result(result)
        self.assertIn("开机请求成功受理", text)
        self.assertIn("开机前 3/3，开机后 2/3", text)


    def test_self_occupancy_uses_actual_entry_and_gpu_index(self) -> None:  # 验证本人占用状态使用实际入口和 GPU INDEX，而不是等待平台空位。
        observed_at = datetime(2026, 7, 21, 19, 12, 58)
        records = [
            OccupancyRecord(
                observed_at=observed_at,
                host="gpu-203",
                machine_name="autodl-203-1",
                gpu_index=0,
                gpu_uuid="gpu0",
                gpu_name="Tesla V100",
                occupied=True,
                instance_id="mine",
                user="何太急",
                started_at_text="2026-07-21 19:12:34",
            ),
            OccupancyRecord(
                observed_at=observed_at,
                host="gpu-203",
                machine_name="autodl-203-2",
                gpu_index=1,
                gpu_uuid="gpu1",
                gpu_name="Tesla V100",
                occupied=True,
                instance_id="other",
                user="任爽",
                started_at_text="2026-07-20 18:28:58",
            ),
        ]
        owned = _owned_instances_from_records(records, "何太急", "gpu-203")
        self.assertEqual([(item.instance_id, item.gpu_index) for item in owned], [("mine", 0)])


if __name__ == "__main__":
    unittest.main()
