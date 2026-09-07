"""v0.5.0 状态恢复安全性的回归测试。"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from autodl_watcher.main import (
    _fresh_owned_instances_from_rows,
    _persisted_state_is_fresh,
)


class StateRestoreV050Test(unittest.TestCase):
    def test_old_evaluator_state_is_not_restored(self) -> None:
        now = datetime(2026, 8, 9, 14, 0, 0)
        persisted = {"saved_at": (now - timedelta(hours=2)).isoformat()}
        self.assertFalse(_persisted_state_is_fresh(persisted, now))

    def test_recent_evaluator_state_can_be_restored(self) -> None:
        now = datetime(2026, 8, 9, 14, 0, 0)
        persisted = {"saved_at": (now - timedelta(seconds=60)).isoformat()}
        self.assertTrue(_persisted_state_is_fresh(persisted, now))

    def test_stale_sqlite_current_instance_is_not_trusted_as_live(self) -> None:
        now = datetime(2026, 8, 9, 14, 0, 0)
        rows = [
            {
                "host": "gpu-203",
                "machine_name": "autodl-203-2",
                "gpu_index": 0,
                "user": "何太急",
                "instance_id": "old-instance",
                "last_seen_at": (now - timedelta(hours=1)).isoformat(),
            }
        ]
        self.assertEqual(
            _fresh_owned_instances_from_rows(rows, "何太急", "gpu-203", now),
            [],
        )

    def test_recent_sqlite_current_instance_is_only_a_fresh_hint(self) -> None:
        now = datetime(2026, 8, 9, 14, 0, 0)
        rows = [
            {
                "host": "gpu-203",
                "machine_name": "autodl-203-2",
                "gpu_index": 0,
                "user": "何太急",
                "instance_id": "recent-instance",
                "last_seen_at": (now - timedelta(seconds=30)).isoformat(),
            }
        ]
        result = _fresh_owned_instances_from_rows(rows, "何太急", "gpu-203", now)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].instance_id, "recent-instance")


if __name__ == "__main__":
    unittest.main()
