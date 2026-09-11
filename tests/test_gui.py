import argparse
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from autodl_watcher.config import load_config
from autodl_watcher.gui import ROOT, monitor_command, worker_python
from autodl_watcher.main import _build_parser, apply_monitor_options, main, positive_seconds


class GuiOptionsTest(unittest.TestCase):
    def test_pythonw_gui_uses_console_interpreter_for_piped_workers(self):
        executable = str(Path("runtime") / "pythonw.exe")
        with patch("sys.executable", executable):
            self.assertEqual(worker_python(), str(Path("runtime") / "python.exe"))

    def setUp(self):
        self.config = load_config(ROOT / "config.yaml")

    def test_project_local_runtime_paths(self):
        self.assertEqual(self.config.platform.user_data_dir, ROOT / "runtime" / "browser_profile")
        self.assertEqual(self.config.runtime.state_file, ROOT / "runtime" / "state.json")
        self.assertEqual(self.config.usage_tracking.database_path, ROOT / "runtime" / "usage" / "occupancy.db")

    def test_invalid_intervals_are_rejected(self):
        for value in ("0", "-1", "nan", "inf", "-inf", "", "abc"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                positive_seconds(value)
        self.assertEqual(positive_seconds("0.5"), 0.5)

    def test_username_and_large_poll_interval_reach_core(self):
        args = _build_parser("gpu-203").parse_args(["--user", " 新用户 ", "--poll-seconds", "120",
                                                   "--usage-seconds", "0.5"])
        config = apply_monitor_options(self.config, args)
        self.assertEqual(config.usage_tracking.self_user, "新用户")
        self.assertEqual(config.monitor.poll_seconds, 120)
        self.assertGreaterEqual(config.monitor.max_sample_gap_seconds, 240)
        self.assertEqual(config.usage_tracking.interval_seconds, 0.5)

    def test_unknown_server_allows_monitoring_but_blocks_live_start(self):
        command = monitor_command(self.config, "autodl-999-1", "测试用户", 1, 2, False, Path("stop"))
        self.assertIn("--dry-run", command)
        self.assertIn("gpu-999", command)
        with self.assertRaises(ValueError):
            monitor_command(self.config, "autodl-999-1", "测试用户", 1, 2, True, Path("stop"))

    def test_empty_username_is_rejected(self):
        with self.assertRaises(ValueError):
            monitor_command(self.config, "autodl-203-1", " ", 1, 2, False, Path("stop"))

    def test_live_start_requires_enabled_configuration(self):
        config = replace(self.config, auto_start=replace(self.config.auto_start, enabled=False))
        with self.assertRaises(ValueError):
            monitor_command(config, "autodl-203-1", "user", 1, 2, True, Path("stop"))

    def test_stopped_monitor_never_contacts_platform_and_saves_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stop = root / "stop"
            stop.touch()
            with patch("autodl_watcher.main.PlatformBrowserCollector") as platform:
                main(["--config", str(ROOT / "config.yaml"), "--dry-run", "--stop-file", str(stop),
                      "--runtime-dir", str(root), "--no-login"])
                platform.return_value.collect.assert_not_called()
                platform.return_value.close.assert_called_once()
            self.assertTrue((root / "state.json").exists())


if __name__ == "__main__":
    unittest.main()
