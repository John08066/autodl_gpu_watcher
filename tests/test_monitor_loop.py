from contextlib import redirect_stdout
from datetime import datetime, timedelta
from dataclasses import replace
import io
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch, call

from autodl_watcher.collectors import PlatformAuthenticationError, PlatformTransientError
from autodl_watcher.gui import ROOT
from autodl_watcher.config import load_config
from autodl_watcher.main import _fresh_gpu_indices, main
from autodl_watcher.models import GpuSample, OccupancyRecord, PlatformHost


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


    @staticmethod
    def _conversion_config():
        config = load_config(ROOT / "config.yaml")
        return replace(config,
            auto_start=replace(config.auto_start, recheck_delay_seconds=0,
                               post_start_check_seconds=-1),
            usage_tracking=replace(config.usage_tracking, enabled=True, interval_seconds=1e-6,
                                   absence_recheck_seconds=1e-6),
            monitor=replace(config.monitor, poll_seconds=0.01, confirmation_seconds=0,
                            min_idle_samples=1))

    def test_no_gpu_instance_is_visible_when_occupancy_fails_but_no_slot_exists(self):
        config = self._conversion_config()
        host = PlatformHost("gpu-203", 0, 2, ("autodl-203-1", "autodl-203-2"),
                            (("autodl-203-1", 0, 2), ("autodl-203-2", 0, 2)))
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()) as output:
            with patch("autodl_watcher.main.load_config", return_value=config), \
                 patch("autodl_watcher.main.PlatformBrowserCollector") as platform, \
                 patch("autodl_watcher.main.TelemetryApiCollector") as telemetry:
                platform.return_value.collect.return_value = [host]
                platform.return_value.collect_occupancy.side_effect = RuntimeError("popup unavailable")
                platform.return_value.get_instance_state.return_value = {
                    "status": "running", "start_mode": "non_gpu",
                    "host_account_gpu_clear": True,
                }
                telemetry.return_value.collect.side_effect = lambda: [
                    GpuSample("gpu-203", 0, "Tesla V100", 0, 0, 32000, datetime.now())]
                main(["--config", str(ROOT / "config.yaml"), "--host", "203", "--entry", "1",
                      "--live", "--convert-no-gpu", "--max-cycles", "2",
                      "--runtime-dir", directory, "--no-login"])
                self.assertIn("无卡运行", output.getvalue())
                self.assertTrue(platform.return_value.get_instance_state.called)
                platform.return_value.post_api_json.assert_not_called()

    def test_occupancy_failure_does_not_hide_same_instance_switch(self):
        config = self._conversion_config()
        first = next(item for item in config.auto_start.targets if item.machine_name == "autodl-203-1")
        host = PlatformHost("gpu-203", 1, 2, ("autodl-203-1", "autodl-203-2"),
                            (("autodl-203-1", 1, 2), ("autodl-203-2", 1, 2)))
        phase = {"off": False, "on": False, "reads_after_off": 0}

        def instance_state(*_args):
            if phase["on"]:
                return {"status": "running", "start_mode": "gpu", "host_account_gpu_clear": False}
            if phase["off"]:
                phase["reads_after_off"] += 1
                status = "shutting_down" if phase["reads_after_off"] == 1 else "shutdown"
                return {"status": status, "start_mode": "non_gpu", "host_account_gpu_clear": True}
            return {"status": "running", "start_mode": "non_gpu", "host_account_gpu_clear": True}

        def api_response(path, _payload):
            phase["off" if path.endswith("power_off") else "on"] = True
            return {"code": "Success"}

        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()) as output:
            with patch("autodl_watcher.main.load_config", return_value=config), \
                 patch("autodl_watcher.main.PlatformBrowserCollector") as platform, \
                 patch("autodl_watcher.main.TelemetryApiCollector") as telemetry:
                platform.return_value.collect.return_value = [host]
                platform.return_value.collect_occupancy.side_effect = RuntimeError("popup unavailable")
                platform.return_value.get_instance_state.side_effect = instance_state
                platform.return_value.post_api_json.side_effect = api_response
                telemetry.return_value.collect.side_effect = lambda: [
                    GpuSample("gpu-203", 0, "Tesla V100", 0, 0, 32000, datetime.now())]
                main(["--config", str(ROOT / "config.yaml"), "--host", "203", "--entry", "1",
                      "--live", "--convert-no-gpu", "--max-cycles", "4",
                      "--runtime-dir", directory, "--no-login"])
                self.assertIn("无卡运行", output.getvalue())
                self.assertEqual(platform.return_value.post_api_json.call_args_list, [
                    call("/api/v2/instance/power_off", {"instance_uuid": first.instance_uuid, "release": "now"}),
                    call("/api/v2/instance/power_on", {"instance_uuid": first.instance_uuid, "start_mode": "gpu"}),
                ])

    def test_old_gpu_occupancy_is_overridden_but_fresh_positive_still_blocks(self):
        config = self._conversion_config()
        first = next(item for item in config.auto_start.targets if item.machine_name == "autodl-203-1")
        names = ("autodl-203-1", "autodl-203-2")
        blocked_host = PlatformHost("gpu-203", 0, 2, names,
                                    (("autodl-203-1", 0, 2), ("autodl-203-2", 0, 2)))
        ready_host = PlatformHost("gpu-203", 1, 2, names,
                                  (("autodl-203-1", 1, 2), ("autodl-203-2", 1, 2)))
        mine = OccupancyRecord(datetime.now(), "gpu-203", "autodl-203-1", 0,
                               "gpu-0", "Tesla V100", True, first.instance_uuid,
                               config.usage_tracking.self_user, "2026-09-25 00:00:00")
        for fresh_second in (False, True):
            with self.subTest(fresh_second=fresh_second):
                phase = {"platform_reads": 0, "state_reads": 0, "occupancy_reads": 0,
                         "off": False, "on": False}

                def collect_platform():
                    phase["platform_reads"] += 1
                    return [blocked_host if phase["platform_reads"] == 1 else ready_host]

                def collect_occupancy(machine_name, **_kwargs):
                    phase["occupancy_reads"] += 1
                    cycle = (phase["occupancy_reads"] - 1) // 2
                    if cycle == 0 or (cycle == 1 and fresh_second):
                        return [mine] if machine_name == first.machine_name else []
                    raise RuntimeError("popup unavailable")

                def instance_state(*_args):
                    phase["state_reads"] += 1
                    if phase["on"]:
                        return {"status": "running", "start_mode": "gpu",
                                "host_account_gpu_clear": False}
                    if phase["off"]:
                        return {"status": "shutdown", "start_mode": "non_gpu",
                                "host_account_gpu_clear": True}
                    if phase["state_reads"] == 1:
                        return {"status": "running", "start_mode": "gpu",
                                "host_account_gpu_clear": False}
                    return {"status": "running", "start_mode": "non_gpu",
                            "host_account_gpu_clear": True}

                def api_response(path, _payload):
                    phase["off" if path.endswith("power_off") else "on"] = True
                    return {"code": "Success"}

                with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()) as output:
                    with patch("autodl_watcher.main.load_config", return_value=config), \
                         patch("autodl_watcher.main.PlatformBrowserCollector") as platform, \
                         patch("autodl_watcher.main.TelemetryApiCollector") as telemetry:
                        platform.return_value.collect.side_effect = collect_platform
                        platform.return_value.collect_occupancy.side_effect = collect_occupancy
                        platform.return_value.get_instance_state.side_effect = instance_state
                        platform.return_value.post_api_json.side_effect = api_response
                        telemetry.return_value.collect.side_effect = lambda: [
                            GpuSample("gpu-203", 0, "Tesla V100", 0, 0, 32000, datetime.now())]
                        main(["--config", str(ROOT / "config.yaml"), "--host", "203", "--entry", "1",
                              "--live", "--convert-no-gpu", "--max-cycles", "2" if fresh_second else "4",
                              "--runtime-dir", directory, "--no-login"])
                        self.assertGreaterEqual(phase["occupancy_reads"], 4)
                        if fresh_second:
                            platform.return_value.post_api_json.assert_not_called()
                        else:
                            self.assertEqual(platform.return_value.post_api_json.call_args_list, [
                                call("/api/v2/instance/power_off",
                                     {"instance_uuid": first.instance_uuid, "release": "now"}),
                                call("/api/v2/instance/power_on",
                                     {"instance_uuid": first.instance_uuid, "start_mode": "gpu"}),
                            ])

    def test_conflicting_fresh_gpu_occupancy_stays_blocked_after_popup_failure(self):
        config = self._conversion_config()
        first = next(item for item in config.auto_start.targets if item.machine_name == "autodl-203-1")
        host = PlatformHost("gpu-203", 1, 2, ("autodl-203-1", "autodl-203-2"),
                            (("autodl-203-1", 1, 2), ("autodl-203-2", 1, 2)))
        mine = OccupancyRecord(datetime.now(), "gpu-203", first.machine_name, 0,
                               "gpu-0", "Tesla V100", True, first.instance_uuid,
                               config.usage_tracking.self_user, "2026-09-25 00:00:00")
        occupancy_reads = 0

        def collect_occupancy(machine_name, **_kwargs):
            nonlocal occupancy_reads
            occupancy_reads += 1
            if occupancy_reads <= 2:
                return [mine] if machine_name == first.machine_name else []
            raise RuntimeError("popup unavailable")

        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            with patch("autodl_watcher.main.load_config", return_value=config), \
                 patch("autodl_watcher.main.PlatformBrowserCollector") as platform, \
                 patch("autodl_watcher.main.TelemetryApiCollector") as telemetry:
                platform.return_value.collect.return_value = [host]
                platform.return_value.collect_occupancy.side_effect = collect_occupancy
                platform.return_value.get_instance_state.return_value = {
                    "status": "running", "start_mode": "non_gpu",
                    "host_account_gpu_clear": True,
                }
                telemetry.return_value.collect.side_effect = lambda: [
                    GpuSample("gpu-203", 0, "Tesla V100", 0, 0, 32000, datetime.now())]
                main(["--config", str(ROOT / "config.yaml"), "--host", "203", "--entry", "1",
                      "--live", "--convert-no-gpu", "--max-cycles", "2",
                      "--runtime-dir", directory, "--no-login"])
                self.assertEqual(occupancy_reads, 4)
                platform.return_value.post_api_json.assert_not_called()

    def test_conflict_clears_after_two_reliable_empty_snapshots(self):
        config = self._conversion_config()
        first = next(item for item in config.auto_start.targets if item.machine_name == "autodl-203-1")
        host = PlatformHost("gpu-203", 1, 2, ("autodl-203-1", "autodl-203-2"),
                            (("autodl-203-1", 1, 2), ("autodl-203-2", 1, 2)))
        mine = OccupancyRecord(datetime.now(), "gpu-203", first.machine_name, 0,
                               "gpu-0", "Tesla V100", True, first.instance_uuid,
                               config.usage_tracking.self_user, "2026-09-25 00:00:00")
        phase = {"occupancy_reads": 0, "telemetry_reads": 0, "off": False, "on": False}

        def collect_occupancy(machine_name, **_kwargs):
            phase["occupancy_reads"] += 1
            cycle = (phase["occupancy_reads"] - 1) // 2
            if cycle == 0:
                return [mine] if machine_name == first.machine_name else []
            if cycle == 1:
                raise RuntimeError("popup unavailable")
            return []

        def collect_telemetry():
            phase["telemetry_reads"] += 1
            return [GpuSample("gpu-203", 0, "Tesla V100", 0, 0, 32000, datetime.now())]

        def instance_state(*_args):
            if phase["on"]:
                return {"status": "running", "start_mode": "gpu",
                        "host_account_gpu_clear": False}
            return {"status": "shutdown" if phase["off"] else "running",
                    "start_mode": "non_gpu", "host_account_gpu_clear": True}

        def api_response(path, _payload):
            self.assertGreaterEqual(phase["occupancy_reads"], 8)
            self.assertGreaterEqual(phase["telemetry_reads"], 6)  # 第二次可靠空快照后的下一轮才可重新评估。
            phase["off" if path.endswith("power_off") else "on"] = True
            return {"code": "Success"}

        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            with patch("autodl_watcher.main.load_config", return_value=config), \
                 patch("autodl_watcher.main.PlatformBrowserCollector") as platform, \
                 patch("autodl_watcher.main.TelemetryApiCollector") as telemetry:
                platform.return_value.collect.return_value = [host]
                platform.return_value.collect_occupancy.side_effect = collect_occupancy
                platform.return_value.get_instance_state.side_effect = instance_state
                platform.return_value.post_api_json.side_effect = api_response
                telemetry.return_value.collect.side_effect = collect_telemetry
                main(["--config", str(ROOT / "config.yaml"), "--host", "203", "--entry", "1",
                      "--live", "--convert-no-gpu", "--max-cycles", "7",
                      "--runtime-dir", directory, "--no-login"])
                self.assertEqual(platform.return_value.post_api_json.call_args_list, [
                    call("/api/v2/instance/power_off",
                         {"instance_uuid": first.instance_uuid, "release": "now"}),
                    call("/api/v2/instance/power_on",
                         {"instance_uuid": first.instance_uuid, "start_mode": "gpu"}),
                ])

    def test_other_same_host_gpu_or_instance_query_failure_still_blocks_switch(self):
        config = self._conversion_config()
        host = PlatformHost("gpu-203", 1, 2, ("autodl-203-1", "autodl-203-2"),
                            (("autodl-203-1", 1, 2), ("autodl-203-2", 1, 2)))
        for other_gpu, query_error in ((True, False), (False, True)):
            with self.subTest(other_gpu=other_gpu, query_error=query_error):
                with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
                    with patch("autodl_watcher.main.load_config", return_value=config), \
                         patch("autodl_watcher.main.PlatformBrowserCollector") as platform, \
                         patch("autodl_watcher.main.TelemetryApiCollector") as telemetry:
                        platform.return_value.collect.return_value = [host]
                        platform.return_value.collect_occupancy.side_effect = RuntimeError("popup unavailable")
                        if query_error:
                            platform.return_value.get_instance_state.side_effect = PlatformTransientError("list unavailable")
                        else:
                            platform.return_value.get_instance_state.return_value = {
                                "status": "running", "start_mode": "non_gpu",
                                "host_account_gpu_clear": False,
                            }
                        telemetry.return_value.collect.side_effect = lambda: [
                            GpuSample("gpu-203", 0, "Tesla V100", 0, 0, 32000, datetime.now())]
                        main(["--config", str(ROOT / "config.yaml"), "--host", "203", "--entry", "1",
                              "--live", "--convert-no-gpu", "--max-cycles", "2",
                              "--runtime-dir", directory, "--no-login"])
                        self.assertTrue(platform.return_value.get_instance_state.called)
                        platform.return_value.post_api_json.assert_not_called()

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


    def test_six_column_occupancy_matches_account_uuid_and_records_user(self):
        config = self._conversion_config()
        first = next(item for item in config.auto_start.targets if item.machine_name == "autodl-203-1")
        host = PlatformHost("gpu-203", 0, 2, ("autodl-203-1", "autodl-203-2"),
                            (("autodl-203-1", 0, 2), ("autodl-203-2", 0, 2)))
        mine = OccupancyRecord(datetime.now(), "gpu-203", first.machine_name, 0,
                               "gpu-0", "Tesla V100", True,
                               f"aaaaaaaaaa-bbbbbbbb (other)\n{first.instance_uuid} (mine)",
                               "", "2026-09-25 00:00:00")
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()) as output:
            with patch("autodl_watcher.main.load_config", return_value=config), \
                 patch("autodl_watcher.main.PlatformBrowserCollector") as platform, \
                 patch("autodl_watcher.main.TelemetryApiCollector") as telemetry:
                platform.return_value.collect.return_value = [host]
                platform.return_value.collect_occupancy.side_effect = (
                    lambda name, **_kwargs: [mine] if name == first.machine_name else [])
                platform.return_value.get_account_instances.return_value = [
                    {"instance_uuid": first.instance_uuid, "machine_name": first.machine_name,
                     "status": "running", "start_mode": "gpu"}]
                platform.return_value.get_instance_state.return_value = {
                    "status": "running", "start_mode": "gpu", "host_account_gpu_clear": False}
                telemetry.return_value.collect.return_value = [
                    GpuSample("gpu-203", 0, "Tesla V100", 0, 0, 32000, datetime.now())]
                main(["--config", str(ROOT / "config.yaml"), "--host", "203", "--entry", "1",
                      "--live", "--convert-no-gpu", "--max-cycles", "1",
                      "--runtime-dir", directory, "--no-login"])
                self.assertIn("本人 GPU 占用 有", output.getvalue())
                self.assertIn(
                    f"{config.usage_tracking.self_user}({first.instance_uuid})", output.getvalue())
                self.assertIn("aaaaaaaaaa-bbbbbbbb", output.getvalue())
                platform.return_value.get_account_instances.assert_called_once()
                platform.return_value.post_api_json.assert_not_called()

    def test_unknown_six_column_identity_with_list_failure_blocks_switch(self):
        config = self._conversion_config()
        first = next(item for item in config.auto_start.targets if item.machine_name == "autodl-203-1")
        host = PlatformHost("gpu-203", 1, 2, ("autodl-203-1", "autodl-203-2"),
                            (("autodl-203-1", 1, 2), ("autodl-203-2", 1, 2)))
        unknown = OccupancyRecord(datetime.now(), "gpu-203", first.machine_name, 0,
                                  "gpu-0", "Tesla V100", True,
                                  "aaaaaaaaaa-bbbbbbbb (other)\ncccccccccc-dddddddd (other)",
                                  "", "2026-09-25 00:00:00")
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()) as output:
            with patch("autodl_watcher.main.load_config", return_value=config), \
                 patch("autodl_watcher.main.PlatformBrowserCollector") as platform, \
                 patch("autodl_watcher.main.TelemetryApiCollector") as telemetry:
                platform.return_value.collect.return_value = [host]
                platform.return_value.collect_occupancy.side_effect = (
                    lambda name, **_kwargs: [unknown] if name == first.machine_name else [])
                platform.return_value.get_account_instances.side_effect = PlatformTransientError(
                    "instance list unavailable")
                platform.return_value.get_instance_state.return_value = {
                    "status": "running", "start_mode": "non_gpu", "host_account_gpu_clear": True}
                telemetry.return_value.collect.side_effect = lambda: [
                    GpuSample("gpu-203", 0, "Tesla V100", 0, 0, 32000, datetime.now())]
                main(["--config", str(ROOT / "config.yaml"), "--host", "203", "--entry", "1",
                      "--live", "--convert-no-gpu", "--max-cycles", "2",
                      "--runtime-dir", directory, "--no-login"])
                self.assertIn("占用身份未确认", output.getvalue())
                self.assertNotIn("本人 GPU 占用 无", output.getvalue())
                platform.return_value.post_api_json.assert_not_called()

    def test_unknown_six_column_identity_excluded_by_complete_account_list(self):
        config = self._conversion_config()
        first = next(item for item in config.auto_start.targets if item.machine_name == "autodl-203-1")
        host = PlatformHost("gpu-203", 0, 2, ("autodl-203-1", "autodl-203-2"),
                            (("autodl-203-1", 0, 2), ("autodl-203-2", 0, 2)))
        other = OccupancyRecord(datetime.now(), "gpu-203", first.machine_name, 0,
                                "gpu-0", "Tesla V100", True,
                                "aaaaaaaaaa-bbbbbbbb (other)\ncccccccccc-dddddddd (other)",
                                "", "2026-09-25 00:00:00")
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()) as output:
            with patch("autodl_watcher.main.load_config", return_value=config), \
                 patch("autodl_watcher.main.PlatformBrowserCollector") as platform, \
                 patch("autodl_watcher.main.TelemetryApiCollector") as telemetry:
                platform.return_value.collect.return_value = [host]
                platform.return_value.collect_occupancy.side_effect = (
                    lambda name, **_kwargs: [other] if name == first.machine_name else [])
                platform.return_value.get_account_instances.return_value = [
                    {"instance_uuid": first.instance_uuid, "machine_name": first.machine_name,
                     "status": "running", "start_mode": "non_gpu"}]
                telemetry.return_value.collect.side_effect = lambda: [
                    GpuSample("gpu-203", 0, "Tesla V100", 0, 0, 32000, datetime.now())]
                main(["--config", str(ROOT / "config.yaml"), "--host", "203", "--entry", "1",
                      "--dry-run", "--max-cycles", "5", "--runtime-dir", directory, "--no-login"])
                self.assertIn("本人 GPU 占用 无", output.getvalue())
                platform.return_value.get_account_instances.assert_called()
                platform.return_value.post_api_json.assert_not_called()

    def test_unparseable_instance_id_cell_blocks_switch_even_with_clear_account_list(self):
        config = self._conversion_config()
        first = next(item for item in config.auto_start.targets if item.machine_name == "autodl-203-1")
        host = PlatformHost("gpu-203", 1, 2, ("autodl-203-1", "autodl-203-2"),
                            (("autodl-203-1", 1, 2), ("autodl-203-2", 1, 2)))
        unknown = OccupancyRecord(datetime.now(), "gpu-203", first.machine_name, 0,
                                  "gpu-0", "Tesla V100", True,
                                  f"aaaaaaaaaa-bbbbbbbb (other)\n{first.instance_uuid}e (bad suffix)",
                                  "", "2026-09-25 00:00:00")
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()) as output:
            with patch("autodl_watcher.main.load_config", return_value=config), \
                 patch("autodl_watcher.main.PlatformBrowserCollector") as platform, \
                 patch("autodl_watcher.main.TelemetryApiCollector") as telemetry:
                platform.return_value.collect.return_value = [host]
                platform.return_value.collect_occupancy.side_effect = (
                    lambda name, **_kwargs: [unknown] if name == first.machine_name else [])
                platform.return_value.get_account_instances.return_value = [
                    {"instance_uuid": first.instance_uuid, "machine_name": first.machine_name,
                     "status": "running", "start_mode": "non_gpu"}]
                platform.return_value.get_instance_state.return_value = {
                    "status": "running", "start_mode": "non_gpu", "host_account_gpu_clear": True}
                telemetry.return_value.collect.side_effect = lambda: [
                    GpuSample("gpu-203", 0, "Tesla V100", 0, 0, 32000, datetime.now())]
                main(["--config", str(ROOT / "config.yaml"), "--host", "203", "--entry", "1",
                      "--live", "--convert-no-gpu", "--max-cycles", "2",
                      "--runtime-dir", directory, "--no-login"])
                self.assertIn("占用身份未确认", output.getvalue())
                self.assertNotIn("本人 GPU 占用 无", output.getvalue())
                platform.return_value.post_api_json.assert_not_called()

    def test_missing_configured_instance_id_is_explicit_in_status(self):
        config = self._conversion_config()
        host = PlatformHost("gpu-203", 0, 2, ("autodl-203-1", "autodl-203-2"),
                            (("autodl-203-1", 0, 2), ("autodl-203-2", 0, 2)))
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()) as output:
            with patch("autodl_watcher.main.load_config", return_value=config), \
                 patch("autodl_watcher.main.PlatformBrowserCollector") as platform, \
                 patch("autodl_watcher.main.TelemetryApiCollector") as telemetry:
                platform.return_value.collect.return_value = [host]
                platform.return_value.collect_occupancy.return_value = []
                platform.return_value.get_instance_state.side_effect = PlatformTransientError(
                    "实例列表中目标 UUID 缺失或重复")
                telemetry.return_value.collect.return_value = []
                main(["--config", str(ROOT / "config.yaml"), "--host", "203", "--entry", "1",
                      "--live", "--convert-no-gpu", "--max-cycles", "1",
                      "--runtime-dir", directory, "--no-login"])
                self.assertIn("配置实例不存在或ID已过期", output.getvalue())
                platform.return_value.post_api_json.assert_not_called()


if __name__ == "__main__":
    unittest.main()
