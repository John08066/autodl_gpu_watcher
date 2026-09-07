"""v0.5.1 状态恢复安全性的回归测试。"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock

from autodl_watcher.main import (
    _persisted_state_is_fresh,
    _restore_recent_evaluator_state,
)


class StateRestoreV051Test(unittest.TestCase):
    def test_old_evaluator_state_is_not_restored(self) -> None:
        now = datetime(2026, 8, 9, 14, 0, 0)
        persisted = {"saved_at": (now - timedelta(hours=2)).isoformat()}
        self.assertFalse(_persisted_state_is_fresh(persisted, now))

    def test_recent_evaluator_state_can_be_restored(self) -> None:
        now = datetime(2026, 8, 9, 14, 0, 0)
        persisted = {"saved_at": (now - timedelta(seconds=60)).isoformat()}
        self.assertTrue(_persisted_state_is_fresh(persisted, now))

    def test_recent_state_restores_continuity_but_rearms_alerted(self) -> None:
        now = datetime(2026, 8, 9, 14, 0, 0)
        persisted = {
            "saved_at": (now - timedelta(seconds=30)).isoformat(),
            "capacity_fingerprint": "fp",
            "evaluator": {"gpu-203|0": {"alerted": True}},
        }
        evaluator = Mock()
        evaluator.rearm_host.return_value = 1

        restored, reset_count = _restore_recent_evaluator_state(
            evaluator,
            persisted,
            "fp",
            now,
            "gpu-203",
        )

        self.assertTrue(restored)
        self.assertEqual(reset_count, 1)
        evaluator.import_state.assert_called_once_with(persisted["evaluator"])
        evaluator.rearm_host.assert_called_once_with("gpu-203")

    def test_fingerprint_mismatch_does_not_import_or_rearm(self) -> None:
        now = datetime(2026, 8, 9, 14, 0, 0)
        persisted = {
            "saved_at": now.isoformat(),
            "capacity_fingerprint": "old",
            "evaluator": {},
        }
        evaluator = Mock()

        restored, reset_count = _restore_recent_evaluator_state(
            evaluator,
            persisted,
            "new",
            now,
            "gpu-203",
        )

        self.assertFalse(restored)
        self.assertEqual(reset_count, 0)
        evaluator.import_state.assert_not_called()
        evaluator.rearm_host.assert_not_called()


if __name__ == "__main__":
    unittest.main()
