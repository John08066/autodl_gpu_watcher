import argparse
import io
import json
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from autodl_watcher.config import load_config
from autodl_watcher.gui import ROOT, SESSION_PREFIX, discover, log_tag, monitor_command, worker_command, worker_python
from autodl_watcher.collectors.platform import PlatformAuthenticationError, PlatformTransientError
from autodl_watcher.main import _build_parser, apply_monitor_options, main, positive_seconds


class GuiOptionsTest(unittest.TestCase):
    def test_pythonw_gui_uses_console_interpreter_for_piped_workers(self):
        executable = str(Path("runtime") / "pythonw.exe")
        with patch("sys.executable", executable):
            self.assertEqual(worker_python(), str(Path("runtime") / "python.exe"))

    def test_source_and_frozen_worker_commands(self):
        import sys
        with patch.object(sys, "frozen", False, create=True):
            command = worker_command("discover")
            self.assertEqual(command[1:], ["-u", "-m", "autodl_watcher.gui", "--discover"])
        with patch.object(sys, "frozen", True, create=True):
            command = worker_command("monitor", "--dry-run")
            self.assertEqual(command, [str(ROOT / "_internal/AutoDLWorker.exe"), "monitor", "--dry-run"])
            self.assertNotIn("-m", command)
        with self.assertRaises(KeyError):
            worker_command("arbitrary-module")

    def test_log_colors_prioritize_failures(self):
        self.assertEqual(log_tag("开机成功，但验证失败"), "error")
        self.assertEqual(log_tag("连接断开，正在重连"), "error")
        self.assertEqual(log_tag("GPU额度不足，暂停自动开机"), "error")
        self.assertEqual(log_tag("已开机，继续监控"), "success")
        self.assertEqual(log_tag("开机成功 · connected"), "success")
        self.assertEqual(log_tag("本人占用待确认"), "normal")
        self.assertEqual(log_tag("登录会话已同步，等待核验"), "normal")


    def test_discover_reports_authentication_and_network_failures_distinctly(self):
        failures = [(PlatformAuthenticationError("HTTP 401"), "invalid"),
                    (PlatformTransientError("Network.getResponseBody: No resource"), "unknown")]
        for error, expected in failures:
            with self.subTest(status=expected), patch("autodl_watcher.collectors.PlatformBrowserCollector") as factory, \
                    patch("sys.stdout", new_callable=io.StringIO) as output:
                factory.return_value.collect.side_effect = error
                with self.assertRaises(SystemExit) as failure:
                    discover()
                self.assertNotEqual(failure.exception.code, 0)
                messages = [json.loads(line[len(SESSION_PREFIX):]) for line in output.getvalue().splitlines()
                            if line.startswith(SESSION_PREFIX)]
                self.assertEqual([item["status"] for item in messages], ["checking", expected])
                self.assertTrue(messages[-1]["checked_at"])
                factory.return_value.close.assert_called_once()
                self.assertNotIn("Traceback", output.getvalue())

    def test_discover_marks_session_valid_only_after_collection(self):
        with patch("autodl_watcher.collectors.PlatformBrowserCollector") as factory, \
                patch("sys.stdout", new_callable=io.StringIO) as output:
            factory.return_value.collect.return_value = []
            discover()
            messages = [json.loads(line[len(SESSION_PREFIX):]) for line in output.getvalue().splitlines()
                        if line.startswith(SESSION_PREFIX)]
            self.assertEqual([item["status"] for item in messages], ["checking", "valid"])
            self.assertIn("WATCHER_HOSTS []", output.getvalue())
            factory.return_value.close.assert_called_once()

    def test_discover_cleanup_failure_does_not_mask_authentication_message(self):
        with patch("autodl_watcher.collectors.PlatformBrowserCollector") as factory, \
                patch("sys.stdout", new_callable=io.StringIO) as output:
            factory.return_value.collect.side_effect = PlatformAuthenticationError("HTTP 401")
            factory.return_value.close.side_effect = OSError("close failed")
            with self.assertRaises(SystemExit):
                discover()
            self.assertIn('"status": "invalid"', output.getvalue())
            self.assertIn("清理失败", output.getvalue())

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

    def test_no_gpu_conversion_requires_live_and_fixed_entry(self):
        with self.assertRaises(ValueError):
            monitor_command(self.config, "autodl-203-1", "user", 1, 2, False, Path("stop"), True)
        command = monitor_command(self.config, "autodl-203-1", "user", 1, 2, True, Path("stop"), True)
        self.assertIn("--convert-no-gpu", command)
        self.assertIn("--entry", command)
        self.assertEqual(command[command.index("--entry") + 1], "autodl-203-1")
        for args in (["--convert-no-gpu", "--live"], ["--convert-no-gpu", "--entry", "1", "--dry-run"]):
            with self.subTest(args=args), self.assertRaises(SystemExit):
                main(["--config", str(ROOT / "config.yaml"), *args])

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
