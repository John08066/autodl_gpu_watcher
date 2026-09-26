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

    def test_disabled_usage_reports_keep_occupancy_checks_without_database(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()) as output:
            with patch("autodl_watcher.main.PlatformBrowserCollector") as platform, \
                 patch("autodl_watcher.main.TelemetryApiCollector") as telemetry, \
                 patch("autodl_watcher.main.UsageSqliteLogger") as database:
                platform.return_value.collect.return_value = [PlatformHost(
                    "gpu-999", 0, 2, ("autodl-999-1",), (("autodl-999-1", 0, 2),))]
                platform.return_value.collect_occupancy.return_value = []
                telemetry.return_value.collect.return_value = [GpuSample(
                    "gpu-999", 0, "Test GPU", 10, 1000, 32000, datetime.now())]
                main(["--config", str(ROOT / "config.yaml"), "--host", "999", "--entry", "1",
                      "--user", "测试用户", "--poll-seconds", "0.01", "--usage-seconds", "0.01",
                      "--max-cycles", "2", "--runtime-dir", directory, "--dry-run", "--no-login",
                      "--no-usage-report"])
                database.assert_not_called()  # 关闭统计不能只隐藏按钮，必须停止建立历史数据库。
                self.assertGreater(platform.return_value.collect_occupancy.call_count, 0)
                platform.return_value.post_api_json.assert_not_called()
            self.assertIn("占用统计与报表已关闭", output.getvalue())
            self.assertFalse((Path(directory) / "occupancy.db").exists())
            self.assertTrue((Path(directory) / "state.json").exists())

    def test_disabled_usage_reports_still_block_unknown_ownership_in_live_mode(self):
        config = self._conversion_config()
        host = PlatformHost("gpu-203", 1, 2, ("autodl-203-1",), (("autodl-203-1", 1, 2),))
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            with patch("autodl_watcher.main.load_config", return_value=config), \
                 patch("autodl_watcher.main.PlatformBrowserCollector") as platform, \
                 patch("autodl_watcher.main.TelemetryApiCollector") as telemetry, \
                 patch("autodl_watcher.main.UsageSqliteLogger") as database:
                platform.return_value.collect.return_value = [host]
                platform.return_value.collect_occupancy.side_effect = PlatformTransientError("occupancy timeout")
                telemetry.return_value.collect.side_effect = lambda: [
                    GpuSample("gpu-203", 0, "Tesla V100", 0, 0, 32000, datetime.now())]
                main(["--config", str(ROOT / "config.yaml"), "--host", "203", "--entry", "1",
                      "--live", "--max-cycles", "2", "--runtime-dir", directory, "--no-login",
                      "--no-usage-report"])
                database.assert_not_called()
                self.assertGreater(platform.return_value.collect_occupancy.call_count, 0)
                platform.return_value.post_api_json.assert_not_called()

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

    @staticmethod
    def _provide_account_snapshot(platform, target):  # 旧转换用例也提供真实接口现在必需的完整个人实例列表。
        query = platform.get_instance_state.side_effect
        snapshot = {}
        def state(*args):
            result = query(*args)
            snapshot.update(result)
            return result
        platform.get_instance_state.side_effect = state
        platform.get_account_instances.side_effect = lambda **_kwargs: [dict(
            snapshot, instance_uuid=target.instance_uuid, machine_name=target.machine_name)]

    @staticmethod
    def _one_free_gpu(machine_name):
        now = datetime.now()
        return [
            OccupancyRecord(now, "gpu-203", machine_name, 0, "gpu-0", "Tesla V100",
                            False, "", "", ""),
            OccupancyRecord(now, "gpu-203", machine_name, 1, "gpu-1", "Tesla V100",
                            True, "aaaaaaaaaa-bbbbbbbb", "other_user", "2026-09-25 00:00:00"),
        ]

    def test_live_loop_releases_other_no_gpu_instances_before_starting_shutdown_target(self):
        config = self._conversion_config()
        first = next(item for item in config.auto_start.targets if item.machine_name == "autodl-203-1")
        rows = [
            {"instance_uuid": first.instance_uuid, "machine_name": first.machine_name,
             "status": "shutdown", "start_mode": "non_gpu"},
            {"instance_uuid": "1111111111-11111111", "machine_name": "autodl-201-1",
             "status": "running", "start_mode": "non_gpu"},
            {"instance_uuid": "2222222222-22222222", "machine_name": "autodl-204-1",
             "status": "running", "start_mode": "non_gpu"},
            {"instance_uuid": "3333333333-33333333", "machine_name": "autodl-202-2",
             "status": "running", "start_mode": "gpu"}]
        def state(uuid, name):
            row = next(item for item in rows if item["instance_uuid"] == uuid and item["machine_name"] == name)
            return dict(row, host_account_gpu_clear=row["status"] == "shutdown" or row["start_mode"] == "non_gpu")
        def power(path, payload):
            row = next(item for item in rows if item["instance_uuid"] == payload["instance_uuid"])
            row["status"] = "shutdown" if path.endswith("power_off") else "running"
            if path.endswith("power_on"):
                self.assertTrue(all(item["status"] == "shutdown" for item in rows if item["start_mode"] == "non_gpu"))
                row["start_mode"] = "gpu"
            return {"code": "Success"}
        host = PlatformHost("gpu-203", 1, 2, (first.machine_name, "autodl-203-2"),
                            ((first.machine_name, 1, 2), ("autodl-203-2", 1, 2)))
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            with patch("autodl_watcher.main.load_config", return_value=config), \
                 patch("autodl_watcher.main.PlatformBrowserCollector") as platform, \
                 patch("autodl_watcher.main.TelemetryApiCollector") as telemetry:
                platform.return_value.collect.return_value = [host]
                platform.return_value.collect_occupancy.side_effect = lambda name, **_kw: self._one_free_gpu(name)
                platform.return_value.get_instance_state.side_effect = state
                platform.return_value.get_account_instances.side_effect = lambda **_kw: [dict(item) for item in rows]
                platform.return_value.post_api_json.side_effect = power
                telemetry.return_value.collect.side_effect = lambda: [
                    GpuSample("gpu-203", 0, "Tesla V100", 0, 0, 32000, datetime.now())]
                main(["--config", str(ROOT / "config.yaml"), "--host", "203", "--entry", "1",
                      "--live", "--convert-no-gpu", "--max-cycles", "4", "--runtime-dir", directory,
                      "--no-login", "--no-usage-report"])
                self.assertEqual(platform.return_value.post_api_json.call_args_list, [
                    call("/api/v2/instance/power_off", {"instance_uuid": "1111111111-11111111", "release": "now"}),
                    call("/api/v2/instance/power_off", {"instance_uuid": "2222222222-22222222", "release": "now"}),
                    call("/api/v2/instance/power_on", {"instance_uuid": first.instance_uuid, "start_mode": "gpu"})])
                self.assertTrue(any(item.kwargs.get("require_personal_scope") is True
                                    for item in platform.return_value.get_account_instances.call_args_list))
            self.assertFalse((Path(directory) / "occupancy.db").exists())

    def test_conversion_retries_after_transient_capacity_recheck_failure(self):
        config = self._conversion_config()
        first = next(item for item in config.auto_start.targets if item.machine_name == "autodl-203-1")
        host = PlatformHost("gpu-203", 1, 2, ("autodl-203-1", "autodl-203-2"),
                            (("autodl-203-1", 1, 2), ("autodl-203-2", 1, 2)))
        for failing_collector in ("platform", "telemetry"):
            with self.subTest(failing_collector=failing_collector):
                phase = {"off": False}
                counts = {"platform": 0, "telemetry": 0}

                def collect_platform():
                    counts["platform"] += 1
                    if failing_collector == "platform" and counts["platform"] == 2:
                        raise PlatformTransientError("recheck timeout")
                    return [host]

                def collect_telemetry():
                    counts["telemetry"] += 1
                    if failing_collector == "telemetry" and counts["telemetry"] == 2:
                        raise TimeoutError("telemetry recheck timeout")
                    return [GpuSample("gpu-203", 0, "Tesla V100", 0, 0, 32000, datetime.now())]

                def api_response(path, _payload):
                    phase["off"] = True
                    return {"code": "Success"}

                with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()) as output:
                    with patch("autodl_watcher.main.load_config", return_value=config), \
                         patch("autodl_watcher.main.PlatformBrowserCollector") as platform, \
                         patch("autodl_watcher.main.TelemetryApiCollector") as telemetry:
                        platform.return_value.collect.side_effect = collect_platform
                        platform.return_value.collect_occupancy.side_effect = (
                            lambda machine_name, **kwargs: self._one_free_gpu(machine_name))
                        platform.return_value.get_instance_state.return_value = {
                            "status": "running", "start_mode": "non_gpu", "host_account_gpu_clear": True}
                        platform.return_value.get_account_instances.return_value = [{
                            "instance_uuid": first.instance_uuid, "machine_name": first.machine_name,
                            "status": "running", "start_mode": "non_gpu"}]
                        platform.return_value.post_api_json.side_effect = api_response
                        telemetry.return_value.collect.side_effect = collect_telemetry
                        main(["--config", str(ROOT / "config.yaml"), "--host", "203", "--entry", "1",
                              "--live", "--convert-no-gpu", "--max-cycles", "2",
                              "--runtime-dir", directory, "--no-login"])
                        self.assertEqual(platform.return_value.post_api_json.call_args_list, [
                            call("/api/v2/instance/power_off", {
                                "instance_uuid": first.instance_uuid, "release": "now"})])
                        self.assertIn("二次确认未通过", output.getvalue())
                        self.assertTrue(phase["off"])

    def test_quota_blocked_status_keeps_read_only_monitoring_without_repeat_power_requests(self):
        config = self._conversion_config()
        first = next(item for item in config.auto_start.targets if item.machine_name == "autodl-203-1")
        host = PlatformHost("gpu-203", 1, 2, ("autodl-203-1", "autodl-203-2"),
                            (("autodl-203-1", 1, 2), ("autodl-203-2", 1, 2)))
        for conversion in (False, True):
            with self.subTest(conversion=conversion):
                phase = {"off": False}

                def instance_state(*_args):
                    return {"status": "running" if conversion and not phase["off"] else "shutdown",
                            "start_mode": "non_gpu", "host_account_gpu_clear": True}

                def api_response(path, _payload):
                    if path.endswith("power_off"):
                        phase["off"] = True
                        return {"code": "Success"}
                    return {"code": "GpuStockReqNum", "msg": "tenant quota exceeded"}

                with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()) as output:
                    with patch("autodl_watcher.main.load_config", return_value=config), \
                         patch("autodl_watcher.main.PlatformBrowserCollector") as platform, \
                         patch("autodl_watcher.main.TelemetryApiCollector") as telemetry:
                        platform.return_value.collect.return_value = [host]
                        platform.return_value.collect_occupancy.side_effect = (
                            lambda machine_name, **kwargs: self._one_free_gpu(machine_name))
                        platform.return_value.get_instance_state.side_effect = instance_state
                        self._provide_account_snapshot(platform.return_value, first)
                        platform.return_value.post_api_json.side_effect = api_response
                        telemetry.return_value.collect.side_effect = lambda: [
                            GpuSample("gpu-203", 0, "Tesla V100", 0, 0, 32000, datetime.now())]
                        args = ["--config", str(ROOT / "config.yaml"), "--host", "203", "--entry", "1",
                                "--live", "--max-cycles", "6", "--runtime-dir", directory, "--no-login"]
                        main(args + (["--convert-no-gpu"] if conversion else []))
                        requests = [call("/api/v2/instance/power_on", {
                            "instance_uuid": first.instance_uuid, "start_mode": "gpu"})]
                        if conversion:
                            requests.insert(0, call("/api/v2/instance/power_off", {
                                "instance_uuid": first.instance_uuid, "release": "now"}))
                        self.assertEqual(platform.return_value.post_api_json.call_args_list, requests)
                        self.assertGreaterEqual(platform.return_value.collect.call_count, 6)
                        self.assertGreaterEqual(telemetry.return_value.collect.call_count, 6)
                        lines = output.getvalue().splitlines()
                        heartbeat = [line for line in lines if line.startswith("WATCHER_STATUS ")][-1]
                        status = [line for line in lines if " | 动作=" in line][-1]
                        self.assertIn("额度不足", heartbeat)
                        self.assertIn("暂停自动开机", status)
                        self.assertNotIn("等待无卡关机", status)

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

    def test_other_entry_popup_failure_does_not_hide_same_instance_switch(self):
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

        def collect_occupancy(machine_name, **_kwargs):
            if machine_name == first.machine_name:
                return self._one_free_gpu(machine_name)
            raise RuntimeError("other entry popup unavailable")

        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()) as output:
            with patch("autodl_watcher.main.load_config", return_value=config), \
                 patch("autodl_watcher.main.PlatformBrowserCollector") as platform, \
                 patch("autodl_watcher.main.TelemetryApiCollector") as telemetry:
                platform.return_value.collect.return_value = [host]
                platform.return_value.collect_occupancy.side_effect = collect_occupancy
                platform.return_value.get_instance_state.side_effect = instance_state
                self._provide_account_snapshot(platform.return_value, first)
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

    def test_target_popup_failure_blocks_same_instance_switch(self):
        config = self._conversion_config()
        host = PlatformHost("gpu-203", 1, 2, ("autodl-203-1", "autodl-203-2"),
                            (("autodl-203-1", 1, 2), ("autodl-203-2", 1, 2)))
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()) as output:
            with (patch("autodl_watcher.main.load_config", return_value=config),
                  patch("autodl_watcher.main.PlatformBrowserCollector") as platform,
                  patch("autodl_watcher.main.TelemetryApiCollector") as telemetry):
                platform.return_value.collect.return_value = [host]
                platform.return_value.collect_occupancy.side_effect = RuntimeError("popup unavailable")
                platform.return_value.get_instance_state.return_value = {
                    "status": "running", "start_mode": "non_gpu", "host_account_gpu_clear": True}
                telemetry.return_value.collect.side_effect = lambda: [
                    GpuSample("gpu-203", 0, "Tesla V100", 0, 0, 32000, datetime.now())]
                main(["--config", str(ROOT / "config.yaml"), "--host", "203", "--entry", "1",
                      "--live", "--convert-no-gpu", "--max-cycles", "2",
                      "--runtime-dir", directory, "--no-login"])
                self.assertIn("目标入口占用详情不可用", output.getvalue())
                platform.return_value.post_api_json.assert_not_called()

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
                        if machine_name != first.machine_name:
                            return []
                        rows = self._one_free_gpu(machine_name)
                        return [replace(rows[0], occupied=True, instance_id=first.instance_uuid,
                                        user=config.usage_tracking.self_user), rows[1]]
                    return self._one_free_gpu(machine_name) if machine_name == first.machine_name else []

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
                        self._provide_account_snapshot(platform.return_value, first)
                        platform.return_value.post_api_json.side_effect = api_response
                        telemetry.return_value.collect.side_effect = lambda: [
                            GpuSample("gpu-203", 0, "Tesla V100", 0, 0, 32000, datetime.now())]
                        main(["--config", str(ROOT / "config.yaml"), "--host", "203", "--entry", "1",
                              "--live", "--convert-no-gpu", "--max-cycles", "2" if fresh_second else "4",
                              "--runtime-dir", directory, "--no-login"])
                        self.assertGreaterEqual(phase["occupancy_reads"], 3 if fresh_second else 4, output.getvalue())
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
                      "--live", "--convert-no-gpu", "--max-cycles", "3",
                      "--runtime-dir", directory, "--no-login"])
                self.assertGreaterEqual(occupancy_reads, 4)
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
            return self._one_free_gpu(machine_name)

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
                 patch("autodl_watcher.main.TelemetryApiCollector") as telemetry, \
                 patch("autodl_watcher.main.time.monotonic",
                       side_effect=(step / 10 for step in range(1000))):  # 固定推进各轮采样，避免依赖真实微秒计时。
                platform.return_value.collect.return_value = [host]
                platform.return_value.collect_occupancy.side_effect = collect_occupancy
                platform.return_value.get_instance_state.side_effect = instance_state
                self._provide_account_snapshot(platform.return_value, first)
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
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()) as output:
            with patch("autodl_watcher.main.PlatformBrowserCollector") as platform, \
                 patch("autodl_watcher.main.interactive_login") as login:
                platform.return_value.collect.side_effect = PlatformAuthenticationError("expired")
                with self.assertRaises(SystemExit) as stopped:
                    main(["--config", str(ROOT / "config.yaml"), "--runtime-dir", directory,
                          "--dry-run", "--no-login"])
                self.assertEqual(stopped.exception.code, 3)
                self.assertIn('"status": "invalid"', output.getvalue())
                self.assertNotIn("Traceback", output.getvalue())
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


class SessionMonitorRegressionTest(unittest.TestCase):
    def test_session_status_distinguishes_platform_failure_from_telemetry_failure(self):
        import json
        from autodl_watcher.session import SESSION_PREFIX
        host = PlatformHost("gpu-999", 0, 2, ("autodl-999-1",), (("autodl-999-1", 0, 2),))
        sample = GpuSample("gpu-999", 0, "Test GPU", 0, 0, 32000, datetime.now())
        for failure_source, expected in (("platform", ["valid", "unknown"]), ("telemetry", ["valid", "valid"])):
            with self.subTest(source=failure_source), tempfile.TemporaryDirectory() as directory, \
                 redirect_stdout(io.StringIO()) as output, \
                 patch("autodl_watcher.main.PlatformBrowserCollector") as platform, \
                 patch("autodl_watcher.main.TelemetryApiCollector") as telemetry:
                platform.return_value.collect.side_effect = [[host], RuntimeError("machine response malformed")] if failure_source == "platform" else [[host], [host]]
                platform.return_value.collect_occupancy.return_value = []
                telemetry.return_value.collect.side_effect = [[sample], RuntimeError("telemetry unavailable")] if failure_source == "telemetry" else [[sample], [sample]]
                main(["--config", str(ROOT / "config.yaml"), "--host", "999", "--entry", "1", "--dry-run", "--no-login",
                      "--poll-seconds", "0.01", "--max-cycles", "2", "--runtime-dir", directory])
                statuses = [json.loads(line[len(SESSION_PREFIX):])["status"] for line in output.getvalue().splitlines()
                            if line.startswith(SESSION_PREFIX)]
                self.assertEqual(statuses, expected)
                platform.return_value.post_api_json.assert_not_called()
                platform.return_value.close.assert_called_once()
