"""
占用日志模块的单元测试。

测试覆盖：
    - 页面弹窗表格行解析
    - 同 GPU 不同实例的保留（不做错误去重）
    - 同实例跨入口的去重
    - SQLite 写入和查询（entry/instance/gpu 维度）
    - 结束事件的正确触发（按 instance_id 而非 GPU INDEX）
    - Windows 上 SQLite 文件句柄的及时释放
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from autodl_watcher.collectors.platform import parse_occupancy_cells
from autodl_watcher.models import OccupancyRecord
from autodl_watcher.usage import (
    UsageSqliteLogger,
    aggregate_gpu_occupants,
    merge_duplicate_instances,
)


class UsageTest(unittest.TestCase):
    def test_parses_occupancy_table_row(self) -> None:
        """功能：
            验证测试场景 `parses_occupancy_table_row` 的预期行为。

        参数：
            无。

        返回：
            None：无返回值。
        """
        observed = datetime(2026, 7, 20, 20, 43, 0)
        record = parse_occupancy_cells(
            [
                "0",
                "GPU-uuid",
                "Tesla V100-PCIE-32GB (32GB)",
                "是",
                "instance-1",
                "用户甲",
                "2026-07-20 18:29:58",
            ],
            observed_at=observed,
            host="gpu-203",
            machine_name="autodl-203-1",
        )
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.gpu_index, 0)
        self.assertTrue(record.occupied)
        self.assertEqual(record.user, "用户甲")

    def test_different_instances_on_same_gpu_are_both_preserved(self) -> None:
        """功能：
            验证测试场景 `different_instances_on_same_gpu_are_both_preserved` 的预期行为。

        参数：
            无。

        返回：
            None：无返回值。
        """
        now = datetime(2026, 7, 20, 21, 0, 0)
        records = [
            OccupancyRecord(now, "gpu-203", "autodl-203-1", 0, "uuid-0", "V100", True, "i-1", "甲", ""),
            OccupancyRecord(now, "gpu-203", "autodl-203-2", 0, "uuid-0", "V100", True, "i-2", "乙", ""),
        ]
        merged = merge_duplicate_instances(records)
        self.assertEqual(len(merged), 2)
        self.assertEqual({item.user for item in merged}, {"甲", "乙"})

        gpu_rows = aggregate_gpu_occupants(records)
        self.assertEqual(len(gpu_rows), 1)
        self.assertEqual(gpu_rows[0]["occupant_count"], 2)
        self.assertIn("甲", gpu_rows[0]["active_users"])
        self.assertIn("乙", gpu_rows[0]["active_users"])

    def test_same_instance_seen_from_two_entries_is_deduplicated(self) -> None:
        """功能：
            验证测试场景 `same_instance_seen_from_two_entries_is_deduplicated` 的预期行为。

        参数：
            无。

        返回：
            None：无返回值。
        """
        now = datetime(2026, 7, 20, 21, 0, 0)
        records = [
            OccupancyRecord(now, "gpu-203", "autodl-203-1", 0, "uuid-0", "V100", True, "same", "甲", ""),
            OccupancyRecord(now + timedelta(seconds=1), "gpu-203", "autodl-203-2", 0, "uuid-0", "V100", True, "same", "甲", ""),
        ]
        merged = merge_duplicate_instances(records)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].machine_name, "autodl-203-1|autodl-203-2")

    def test_sqlite_records_all_entries_instances_and_gpu_concurrency(self) -> None:
        """功能：
            验证测试场景 `sqlite_records_all_entries_instances_and_gpu_concurrency` 的预期行为。

        参数：
            无。

        返回：
            None：无返回值。
        """
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "occupancy.db"
            logger = UsageSqliteLogger(database)
            now = datetime(2026, 7, 20, 21, 0, 0)
            records = [
                OccupancyRecord(now, "gpu-203", "autodl-203-1", 0, "uuid-0", "V100", True, "i-1", "甲", "2026-07-20 18:00:00"),
                OccupancyRecord(now + timedelta(seconds=1), "gpu-203", "autodl-203-2", 0, "uuid-0", "V100", True, "i-2", "乙", "2026-07-20 19:00:00"),
                OccupancyRecord(now, "gpu-203", "autodl-203-1", 1, "uuid-1", "V100", False, "", "", ""),
                OccupancyRecord(now + timedelta(seconds=1), "gpu-203", "autodl-203-2", 1, "uuid-1", "V100", False, "", "", ""),
            ]
            events = logger.record(records)
            self.assertEqual(len(events), 2)

            conn = sqlite3.connect(database)
            try:
                entry_count = conn.execute("SELECT COUNT(*) FROM entry_snapshots").fetchone()[0]
                instance_count = conn.execute("SELECT COUNT(*) FROM instance_snapshots").fetchone()[0]
                gpu_count = conn.execute("SELECT COUNT(*) FROM gpu_snapshots").fetchone()[0]
                current_count = conn.execute("SELECT COUNT(*) FROM current_instances").fetchone()[0]
                occupant_count = conn.execute(
                    "SELECT occupant_count FROM gpu_snapshots WHERE gpu_index = 0"
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(entry_count, 4)
            self.assertEqual(instance_count, 2)
            self.assertEqual(gpu_count, 2)
            self.assertEqual(current_count, 2)
            self.assertEqual(occupant_count, 2)

            # Windows regression: both logger and verification connection must
            # be closed before TemporaryDirectory cleanup.
            renamed = database.with_suffix(".verified")
            database.rename(renamed)
            renamed.rename(database)

    def test_end_event_is_keyed_by_instance_not_gpu(self) -> None:
        """功能：
            验证测试场景 `end_event_is_keyed_by_instance_not_gpu` 的预期行为。

        参数：
            无。

        返回：
            None：无返回值。
        """
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "occupancy.db"
            logger = UsageSqliteLogger(database)
            now = datetime(2026, 7, 20, 21, 0, 0)
            logger.record(
                [
                    OccupancyRecord(now, "gpu-203", "autodl-203-1", 0, "uuid", "V100", True, "i-1", "甲", "2026-07-20 20:00:00"),
                    OccupancyRecord(now, "gpu-203", "autodl-203-2", 0, "uuid", "V100", True, "i-2", "乙", "2026-07-20 20:30:00"),
                ]
            )
            events = logger.record(
                [
                    OccupancyRecord(now + timedelta(minutes=10), "gpu-203", "autodl-203-1", 0, "uuid", "V100", False, "", "", ""),
                    OccupancyRecord(now + timedelta(minutes=10), "gpu-203", "autodl-203-2", 0, "uuid", "V100", True, "i-2", "乙", "2026-07-20 20:30:00"),
                ]
            )
            end_users = [event["user"] for event in events if event["event"] == "END_SEEN"]
            self.assertEqual(end_users, ["甲"])
            current = logger.current_instances()
            self.assertEqual(len(current), 1)
            self.assertEqual(current[0]["user"], "乙")

    def test_sqlite_connection_is_closed_after_record(self) -> None:
        """功能：
            验证测试场景 `sqlite_connection_is_closed_after_record` 的预期行为。

        参数：
            无。

        返回：
            None：无返回值。
        """
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "occupancy.db"
            logger = UsageSqliteLogger(database)
            now = datetime(2026, 7, 20, 21, 0, 0)
            logger.record(
                [
                    OccupancyRecord(
                        now, "gpu-203", "autodl-203-1", 0, "uuid",
                        "V100", True, "i-1", "甲", "2026-07-20 20:00:00"
                    )
                ]
            )
            # On Windows this rename fails immediately if SQLite still holds
            # an open file handle.  Renaming back preserves the fixture.
            renamed = database.with_suffix(".moved")
            database.rename(renamed)
            renamed.rename(database)



if __name__ == "__main__":
    unittest.main()

class UsagePartialSnapshotV050Test(unittest.TestCase):
    def test_partial_snapshot_does_not_create_false_end_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "occupancy.db"
            logger = UsageSqliteLogger(database)
            now = datetime(2026, 8, 9, 12, 0, 0)
            logger.record(
                [
                    OccupancyRecord(
                        now, "gpu-203", "autodl-203-2", 0, "uuid0", "V100",
                        True, "mine", "何太急", "2026-08-09 11:00:00"
                    ),
                    OccupancyRecord(
                        now, "gpu-203", "autodl-203-2", 1, "uuid1", "V100",
                        False, "", "", ""
                    ),
                ],
                complete_snapshot=True,
                snapshot_hosts={"gpu-203"},
            )

            later = now + timedelta(minutes=1)
            events = logger.record(
                [
                    OccupancyRecord(
                        later, "gpu-203", "autodl-203-1", 0, "uuid0", "V100",
                        False, "", "", ""
                    )
                ],
                complete_snapshot=False,
                snapshot_hosts={"gpu-203"},
            )

            self.assertEqual(events, [])
            current = logger.current_instances()
            self.assertEqual(len(current), 1)
            self.assertEqual(current[0]["instance_id"], "mine")

    def test_complete_snapshot_can_clear_previous_instance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "occupancy.db"
            logger = UsageSqliteLogger(database)
            now = datetime(2026, 8, 9, 12, 0, 0)
            logger.record(
                [
                    OccupancyRecord(
                        now, "gpu-203", "autodl-203-2", 0, "uuid0", "V100",
                        True, "mine", "何太急", "2026-08-09 11:00:00"
                    )
                ],
                complete_snapshot=True,
                snapshot_hosts={"gpu-203"},
            )
            later = now + timedelta(minutes=1)
            events = logger.record(
                [
                    OccupancyRecord(
                        later, "gpu-203", "autodl-203-2", 0, "uuid0", "V100",
                        False, "", "", ""
                    )
                ],
                complete_snapshot=True,
                snapshot_hosts={"gpu-203"},
            )
            self.assertEqual([e["event"] for e in events], ["END_SEEN"])
            self.assertEqual(logger.current_instances(), [])
