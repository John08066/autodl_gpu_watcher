from contextlib import redirect_stdout
from datetime import datetime, timedelta
from dataclasses import replace
import io
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch, call

from autodl_watcher.collectors import PlatformAuthenticationError
from autodl_watcher.gui import ROOT
from autodl_watcher.config import load_config
from autodl_watcher.main import _fresh_gpu_indices, main
from autodl_watcher.models import GpuSample, PlatformHost


class MonitorLoopTest(unittest.TestCase):
    def test_stale_samples_cannot_normalize_occupancy_rows(self):
        now = datetime.now()
        sample = GpuSample("gpu-202", 0, "Test GPU", 0, 0, 32000, now - timedelta(seconds=91))
        self.assertIsNone(_fresh_gpu_indices([sample], now, 90))
        self.assertIsNone(_fresh_gpu_indices([], now, 90))

    def test_two_read_only_cycles_reach_persistence_without_power_on(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()) as output:
            with patch("autodl_watcher.main.PlatformBrowserCollector") as platform, \
                 patch("autodl_watcher.main.TelemetryApiCollector") as telemetry:
                platform.return_value.collect.return_value = [PlatformHost(
                    "gpu-999", 0, 2, ("autodl-999-1",), (("autodl-999-1", 0, 2),))]
                platform.return_value.collect_occupancy.return_value = []
                telemetry.return_value.collect.return_value = [GpuSample(
                    "gpu-999", 0, "Test GPU", 10, 1000, 32000, datetime.now())]
                main(["--config", str(ROOT / "config.yaml"), "--host", "999", "--entry", "1",
                      "--user", "测试用户", "--poll-seconds", "0.01", "--usage-seconds", "0.01",
                      "--max-cycles", "2", "--runtime-dir", directory, "--dry-run", "--no-login"])
                self.assertEqual(platform.return_value.collect.call_count, 2)
                self.assertEqual(telemetry.return_value.collect.call_count, 2)
                platform.return_value.post_api_json.assert_not_called()
                platform.return_value.close.assert_called_once()
            self.assertEqual(output.getvalue().count("WATCHER_STATUS"), 2)
            self.assertTrue((Path(directory) / "state.json").exists())
            self.assertTrue((Path(directory) / "occupancy.db").exists())

    def test_stop_during_cycle_skips_long_poll_wait(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            stop = Path(directory) / "stop"
            def finish_cycle():
                stop.touch()
                return [PlatformHost("gpu-203", 0, 2, ("autodl-203-1",), (("autodl-203-1", 0, 2),))]
            with patch("autodl_watcher.main.PlatformBrowserCollector") as platform, \
                 patch("autodl_watcher.main.TelemetryApiCollector") as telemetry:
                platform.return_value.collect.side_effect = finish_cycle
                platform.return_value.collect_occupancy.return_value = []
                telemetry.return_value.collect.return_value = []
                started = time.monotonic()
                main(["--config", str(ROOT / "config.yaml"), "--host", "203", "--entry", "1",
                      "--poll-seconds", "120", "--stop-file", str(stop), "--runtime-dir", directory,
                      "--dry-run", "--no-login"])
                self.assertLess(time.monotonic() - started, 3)
                telemetry.return_value.collect.assert_not_called()
                platform.return_value.post_api_json.assert_not_called()


    def test_main_loop_switches_same_instance_after_confirmed_shutdown(self):
        config = load_config(ROOT / "config.yaml")
        first = next(item for item in config.auto_start.targets if item.machine_name == "autodl-203-1")
        config = replace(config,
            auto_start=replace(config.auto_start, targets=(first,), recheck_delay_seconds=0,
                               post_start_check_seconds=-1),
            usage_tracking=replace(config.usage_tracking, enabled=False),
            monitor=replace(config.monitor, poll_seconds=0.01, confirmation_seconds=0,
                            min_idle_samples=1))
        host = PlatformHost("gpu-203", 1, 2, ("autodl-203-1",), (("autodl-203-1", 1, 2),))
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            with patch("autodl_watcher.main.load_config", return_value=config),                  patch("autodl_watcher.main.PlatformBrowserCollector") as platform,                  patch("autodl_watcher.main.TelemetryApiCollector") as telemetry:
                platform.return_value.collect.return_value = [host]
                platform.return_value.get_instance_state.side_effect = [
                    {"status": "running", "start_mode": "non_gpu"},
                    {"status": "shutting_down", "start_mode": "non_gpu"},
                    {"status": "shutdown", "start_mode": "non_gpu"},
                    {"status": "shutdown", "start_mode": "non_gpu"},
                ]
                platform.return_value.post_api_json.return_value = {"code": "Success"}
                telemetry.return_value.collect.side_effect = lambda: [
                    GpuSample("gpu-203", 0, "Tesla V100", 0, 0, 32000, datetime.now())]
                main(["--config", str(ROOT / "config.yaml"), "--host", "203", "--entry", "1",
                      "--live", "--convert-no-gpu", "--max-cycles", "3",
                      "--runtime-dir", directory, "--no-login"])
                self.assertEqual(platform.return_value.post_api_json.call_args_list, [
                    call("/api/v2/instance/power_off", {"instance_uuid": first.instance_uuid, "release": "now"}),
                    call("/api/v2/instance/power_on", {"instance_uuid": first.instance_uuid, "start_mode": "gpu"}),
                ])

    def test_expired_ui_session_exits_and_releases_browser(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            with patch("autodl_watcher.main.PlatformBrowserCollector") as platform, \
                 patch("autodl_watcher.main.interactive_login") as login:
                platform.return_value.collect.side_effect = PlatformAuthenticationError("expired")
                with self.assertRaises(PlatformAuthenticationError):
                    main(["--config", str(ROOT / "config.yaml"), "--runtime-dir", directory,
                          "--dry-run", "--no-login"])
                login.assert_not_called()
                platform.return_value.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
