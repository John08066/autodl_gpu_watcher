from __future__ import annotations  # v0.5.1 状态恢复安全性的回归测试。

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

        restored, reset_count = _restore_recent_evaluator_state( evaluator, persisted, "fp", now, "gpu-203", )

        self.assertTrue(restored)
        self.assertEqual(reset_count, 1)
        evaluator.import_state.assert_called_once_with(persisted["evaluator"])
        evaluator.rearm_host.assert_called_once_with("gpu-203")

    def test_fingerprint_mismatch_does_not_import_or_rearm(self) -> None:
        now = datetime(2026, 8, 9, 14, 0, 0)
        persisted = { "saved_at": now.isoformat(), "capacity_fingerprint": "old", "evaluator": {}, }
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

class AbsenceDebounceV054Test(unittest.TestCase):
    def test_first_empty_snapshot_does_not_confirm_shutdown(self) -> None:
        from autodl_watcher.main import _advance_absence_confirmation

        streak, confirmed, fast = _advance_absence_confirmation(
            previous_known=True,
            previous_active=True,
            complete_snapshot=True,
            captured_owned=False,
            current_streak=0,
            required=2,
        )
        self.assertEqual(streak, 1)
        self.assertFalse(confirmed)
        self.assertTrue(fast)

    def test_second_consecutive_empty_snapshot_confirms_shutdown(self) -> None:
        from autodl_watcher.main import _advance_absence_confirmation

        streak, confirmed, fast = _advance_absence_confirmation(
            previous_known=True,
            previous_active=True,
            complete_snapshot=True,
            captured_owned=False,
            current_streak=1,
            required=2,
        )
        self.assertEqual(streak, 2)
        self.assertTrue(confirmed)
        self.assertFalse(fast)

    def test_failed_snapshot_breaks_absence_streak(self) -> None:
        from autodl_watcher.main import _advance_absence_confirmation

        streak, confirmed, fast = _advance_absence_confirmation(
            previous_known=True,
            previous_active=True,
            complete_snapshot=False,
            captured_owned=False,
            current_streak=1,
            required=2,
        )
        self.assertEqual(streak, 0)
        self.assertFalse(confirmed)
        self.assertFalse(fast)
