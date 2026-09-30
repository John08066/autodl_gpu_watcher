import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from autodl_watcher import remote_probe
from autodl_watcher.config import load_config
from autodl_watcher.gui import ROOT, DATA_PREFIX, discovery_rows, discover
from autodl_watcher.models import PlatformHost
from autodl_watcher.training import TrainingServer, TrainingTracker, parse_progress
from autodl_watcher.training_ui import gpu_table, task_memory
from test_training import snapshot, LOG


class Version081Test(unittest.TestCase):
    def test_power_eligibility_requires_current_instance_not_saved_uuid(self):
        config = load_config(ROOT / "config.yaml")
        hosts = [PlatformHost("gpu-203", 0, 2, (), tuple((item.machine_name, 0, 2) for item in config.auto_start.targets))]
        target = config.auto_start.targets[0]
        instances = [dict(machine_name=target.machine_name, instance_uuid=target.instance_uuid)]
        rows = discovery_rows(config, hosts, instances)
        self.assertEqual(rows[0][2], "可开机")
        self.assertTrue(all(row[2] == "无实例" for row in rows[1:]))
        instances[0]["instance_uuid"] = "rebuilt-instance"
        self.assertEqual(discovery_rows(config, hosts, instances)[0][2], "实例已变更")
        self.assertTrue(all(row[2] == "未核验" for row in discovery_rows(config, hosts, None)))

    def test_discovery_instance_failure_keeps_readonly_platform_available(self):
        with patch("autodl_watcher.collectors.PlatformBrowserCollector") as factory, patch("autodl_watcher.collectors.TelemetryApiCollector"), patch("sys.stdout", new_callable=io.StringIO) as output:
            factory.return_value.collect.return_value = [PlatformHost("gpu-203", 0, 2, (), (("autodl-203-1", 0, 2),))]
            factory.return_value.get_account_instances.side_effect = OSError("temporary error")
            discover()
        rows = next(json.loads(line[len(DATA_PREFIX):]) for line in output.getvalue().splitlines() if line.startswith(DATA_PREFIX))
        self.assertEqual(rows[0][2], "未核验")
        self.assertIn('"status": "valid"', output.getvalue())

    def test_process_table_does_not_confuse_card_memory_and_own_memory(self):
        value = dict(received_at=time.time(), gpus=[dict(index=0, memory_used=16000, memory_total=32000, util=80, temperature=60)],
                     gpu_processes=[dict(gpu_index=0, pid=10, user="本人", process="train.py", config="mine", memory_used=6000),
                                    dict(gpu_index=0, pid=20, user="other", process="train.py", config="other-run", memory_used=10000)])
        text = gpu_table(value, "server")
        self.assertIn("VRAM整卡", text)
        self.assertIn("15.62/31.25", text)
        self.assertIn("本人", text)
        self.assertIn("other-run", text)
        self.assertIn("5.86", text)
        self.assertIn("9.77", text)
        container_text = gpu_table(dict(value, container=True), "203-1")
        self.assertIn("容器0", container_text)
        self.assertIn("容器编号不等于平台INDEX", container_text)
        self.assertIn("5.86", task_memory(dict(alive=True, gpu_memory_mb=6000)))
        self.assertIn("无法核验", task_memory(dict(alive=True, gpu_memory_mb=None)))
        self.assertNotIn("15.62", task_memory(dict(alive=True, gpu_memory_mb=None)))
        self.assertIn("已退出", task_memory(dict(alive=False, gpu_memory_mb=6000)))

    def test_container_pid_cannot_be_assigned_even_if_local_pid_has_same_number(self):
        apps = [dict(uuid="gpu", pid=123, memory_used=7000, process_name="python")]
        with patch.object(remote_probe.os, "getuid", side_effect=AssertionError("must not match container UID"), create=True):
            row = remote_probe.public_gpu_processes([dict(uuid="gpu", index=0)], apps, True)[0]
        self.assertFalse(row["own"])
        self.assertEqual(row["user"], "宿主用户未知")
        self.assertEqual(row["memory_used"], 7000)

    def test_visible_other_user_metadata_excludes_secrets_and_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory); process = folder / "123"; process.mkdir()
            (process / "stat").write_text("123 (python) " + " ".join(["R", "1"] + ["0"] * 17 + ["100", "0"]))
            (process / "cmdline").write_bytes(b"python\0train.py\0--config\0/project/config.yaml\0--token\0PRIVATE-VALUE\0")
            real_path = Path
            def path(value):
                return folder if str(value) == "/proc" else real_path(value)
            apps = [dict(uuid="gpu", pid=123, memory_used=7000, process_name="python")]
            with patch.object(remote_probe, "Path", side_effect=path), patch.object(remote_probe.os, "getuid", return_value=99, create=True), patch.object(remote_probe, "pwd", SimpleNamespace(getpwuid=lambda uid: SimpleNamespace(pw_name="other-user")), create=True):
                row = remote_probe.public_gpu_processes([dict(uuid="gpu", index=0)], apps, False)[0]
            self.assertEqual((row["user"], row["process"], row["config"]), ("other-user", "train.py", "config.yaml"))
            self.assertFalse(row["own"])
            self.assertNotIn("PRIVATE-VALUE", json.dumps(row))
            self.assertNotIn("logs", row)

    def test_missing_process_memory_is_unknown_not_zero(self):
        with patch.object(remote_probe.subprocess, "run", side_effect=[SimpleNamespace(returncode=0, stdout="0,gpu,GPU,80,1000,32000,60"), SimpleNamespace(returncode=0, stdout="gpu,123,python,N/A")]):
            _, apps, _ = remote_probe.query_gpu()
        self.assertEqual(apps[0]["pid"], 123)
        self.assertIsNone(apps[0]["memory_used"])

    def test_error_variants_and_nonfinite_loss_are_detected(self):
        for error in ["TypeError: bad batch", "KeyError: missing", "OSError: disk error", "Exception: fail", "CUDA error: device-side assert triggered", "Killed", "[ERROR] training failed", "Iter: 3000 training-loss, overall: nan"]:
            with self.subTest(error=error):
                self.assertTrue(parse_progress(LOG + error)["error"])
        value = parse_progress(LOG + "Iter: 3000 training-loss, overall: nan\nIter: 3000 training-metric, acc: 0.5")
        self.assertTrue(value["error"])

    def test_console_failure_is_not_hidden_by_newer_training_log(self):
        raw = snapshot(alive=False)
        primary = raw["tasks"][0]["logs"][0]
        raw["tasks"][0]["logs"].append(dict(path="/project/console.log", head=LOG+"TypeError: batch failed", tail="", modified_at=primary["modified_at"]-10))
        result = TrainingTracker(TrainingServer("s", "s", "server")).update(raw)
        self.assertEqual(result["cards"][0]["level"], "error")
        self.assertIn("batch failed", result["cards"][0]["progress"]["error"])

    def test_observed_failure_stays_red_after_log_rotation_or_missing_logs(self):
        tracker = TrainingTracker(TrainingServer("s", "s", "server"))
        first = tracker.update(snapshot(LOG + "TypeError: bad batch"))["cards"][0]
        self.assertEqual(first["level"], "error")
        later = tracker.update(snapshot(LOG + "===> Epoch[3] start!"))["cards"][0]
        self.assertEqual(later["level"], "error")
        current = snapshot(); current["tasks"][0]["logs"] = []
        self.assertEqual(tracker.update(current)["cards"][0]["level"], "error")
        partial = dict(snapshot(), tasks=[], scan_ok=False)
        self.assertEqual(tracker.update(partial)["cards"][0]["level"], "error")
        current["tasks"][0]["key"] = "new-process-key"
        self.assertEqual(tracker.update(current)["cards"][0]["level"], "warning")


if __name__ == "__main__":
    unittest.main()
