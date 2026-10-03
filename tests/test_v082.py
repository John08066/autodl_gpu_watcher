from dataclasses import replace
import queue
import unittest
from unittest.mock import patch

from autodl_watcher.training import TrainingServer
from autodl_watcher.training_ui import TrainingService
from test_training import snapshot


class PendingConnectionTest(unittest.TestCase):
    def setUp(self):
        self.service = TrainingService(queue.Queue())
        self.old = TrainingServer("s", "203-1", "original")
        self.new = replace(self.old, ssh_alias="replacement", project="/new/project")
        self.service.register(self.old)
        self.service.start("s", 60)

    def test_save_during_request_preserves_old_source_until_next_start(self):
        state = self.service.states["s"]
        state["busy"] = True
        self.service.register(self.new)
        self.assertEqual(state["server"], self.old)
        self.assertEqual(state["pending"], self.new)
        self.service.receive(("s", 0, snapshot(), ""))
        self.assertIsNotNone(state["snapshot"])
        with patch("autodl_watcher.training_ui.threading.Thread") as thread, \
             patch("autodl_watcher.training_ui.time.monotonic", return_value=state["due"]):
            self.service.tick()
            self.assertEqual(thread.call_args.kwargs["args"][-1].server, self.old)
        self.service.stop("s")
        with self.assertRaises(ValueError):
            self.service.start("s", 60)
        self.service.receive(("s", 0, snapshot(), ""))
        self.service.start("s", 30)
        current = self.service.states["s"]
        self.assertEqual(current["server"], self.new)
        self.assertIsNone(current["pending"])
        self.assertIsNone(current["snapshot"])
        self.assertTrue(current["active"])
        self.assertEqual(current["interval"], 30)
        with patch("autodl_watcher.training_ui.threading.Thread") as thread:
            self.service.tick()
            self.assertEqual(thread.call_args.kwargs["args"][-1].server, self.new)

    def test_last_saved_connection_wins_and_reverting_clears_pending(self):
        self.service.register(self.new)
        latest = replace(self.new, name="改名", ssh_alias="last")
        self.service.register(latest)
        self.assertEqual(self.service.states["s"]["pending"], latest)
        self.service.register(self.old)
        self.assertIsNone(self.service.states["s"]["pending"])
        self.assertTrue(self.service.states["s"]["active"])

    def test_new_server_is_configurable_without_stopping_existing_monitor(self):
        self.service.register(TrainingServer("other", "201-1", "newhost"))
        self.assertTrue(self.service.states["s"]["active"])
        self.assertFalse(self.service.states["other"]["active"])
        self.assertIsNone(self.service.states["other"]["snapshot"])

    def test_saving_stopped_busy_connection_still_discards_late_result(self):
        state = self.service.states["s"]
        state["busy"] = True
        self.service.stop("s")
        self.service.register(self.new)
        self.service.receive(("s", 0, snapshot(), ""))
        self.assertIsNone(state["snapshot"])
        self.service.start("s", 60)
        self.assertEqual(self.service.states["s"]["server"], self.new)


if __name__ == "__main__":
    unittest.main()
