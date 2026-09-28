from contextlib import redirect_stdout
from datetime import datetime
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from autodl_watcher.collectors.platform import PlatformAuthenticationError, PlatformTransientError, PlatformBrowserCollector
from autodl_watcher.config import load_config
from autodl_watcher.gui import ROOT
from autodl_watcher.main import main
from autodl_watcher.models import GpuSample, OccupancyRecord, PlatformHost


class Version070Test(unittest.TestCase):
    def collector(self):
        collector = PlatformBrowserCollector(load_config(ROOT / "config.yaml").platform)
        collector.start = Mock()
        collector._context = Mock()
        collector._authorization = "test-only"
        return collector

    def test_http_200_login_expiry_is_authentication_failure(self):
        for message in ("登陆超时，请重新登录; 登录失败，请重试", "登录超时，请重新登录"):
            with self.subTest(message=message):
                response = Mock(status=200, request=None)
                response.json.return_value = {"code": "fixture_expired", "msg": message}
                with self.assertRaises(PlatformAuthenticationError):
                    self.collector()._parse_machine_list_response(response)

    def test_non_authentication_business_error_is_not_login_expiry(self):
        for message in ("库存不足", "服务繁忙，请稍后重试", "登录服务暂时不可用"):
            response = Mock(status=200, request=None)
            response.json.return_value = {"code": "fixture_other", "msg": message}
            with self.assertRaises(RuntimeError) as raised:
                self.collector()._parse_machine_list_response(response)
            self.assertNotIsInstance(raised.exception, PlatformAuthenticationError)

    def test_instance_api_expiry_is_not_retried(self):
        collector = self.collector()
        response = Mock(status=200, ok=True)
        response.json.return_value = {"code": "fixture_expired", "msg": "登陆超时，请重新登录; 登录失败，请重试"}
        collector._context.request.post.return_value = response
        with self.assertRaises(PlatformAuthenticationError):
            collector.post_api_json("/api/v2/instance/list", {})
        collector._context.request.post.assert_called_once()

    def test_business_expiry_stops_monitor_without_sampling_or_power(self):
        response = Mock(status=200, request=None)
        response.json.return_value = {"code": "fixture_expired", "msg": "登陆超时，请重新登录; 登录失败，请重试"}
        collector = self.collector()
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()) as output:
            with patch("autodl_watcher.main.PlatformBrowserCollector", return_value=collector), \
                 patch.object(collector, "collect", side_effect=lambda: collector._parse_machine_list_response(response)) as collect, \
                 patch.object(collector, "close"), patch.object(collector, "post_api_json") as power, \
                 patch("autodl_watcher.main.TelemetryApiCollector") as telemetry:
                with self.assertRaises(SystemExit) as stopped:
                    main(["--config", str(ROOT / "config.yaml"), "--host", "203", "--entry", "1",
                          "--dry-run", "--no-login", "--max-cycles", "3", "--poll-seconds", "0.01", "--runtime-dir", directory])
                self.assertEqual(stopped.exception.code, 3)
                collect.assert_called_once()
                telemetry.return_value.collect.assert_not_called()
                power.assert_not_called()
            self.assertIn("监控203-1 | 登录已失效 | 动作：监控已停止，请重新登录", output.getvalue())
            self.assertNotIn("Traceback", output.getvalue())

    def test_local_startup_failure_stops_without_retry_or_power(self):
        from autodl_watcher.login import LocalStartupError
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()) as output:
            with patch("autodl_watcher.main.PlatformBrowserCollector") as platform, \
                 patch("autodl_watcher.main.TelemetryApiCollector") as telemetry:
                platform.return_value.collect.side_effect = LocalStartupError("未找到 Windows PowerShell：missing/powershell.exe")
                with self.assertRaises(SystemExit) as stopped:
                    main(["--config", str(ROOT / "config.yaml"), "--host", "203", "--entry", "1",
                          "--dry-run", "--no-login", "--max-cycles", "3", "--runtime-dir", directory])
                self.assertEqual(stopped.exception.code, 5)
                platform.return_value.collect.assert_called_once()
                platform.return_value.post_api_json.assert_not_called()
                telemetry.return_value.collect.assert_not_called()
            self.assertIn("监控203-1 | 本地启动失败 | 动作：监控已停止", output.getvalue())
            self.assertIn("powershell.exe", output.getvalue())
            self.assertNotIn("等待重试", output.getvalue())
            self.assertNotIn("登录已失效", output.getvalue())

    def test_expiry_during_occupancy_stops_immediately(self):
        host = PlatformHost("gpu-203", 0, 2, ("autodl-203-1",), (("autodl-203-1", 0, 2),))
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()) as output:
            with patch("autodl_watcher.main.PlatformBrowserCollector") as platform, \
                 patch("autodl_watcher.main.TelemetryApiCollector") as telemetry:
                platform.return_value.collect.return_value = [host]
                platform.return_value.collect_occupancy.side_effect = PlatformAuthenticationError("登录超时，请重新登录")
                telemetry.return_value.collect.return_value = []
                with self.assertRaises(SystemExit) as stopped:
                    main(["--config", str(ROOT / "config.yaml"), "--host", "203", "--entry", "1",
                          "--dry-run", "--no-login", "--max-cycles", "3", "--runtime-dir", directory])
                self.assertEqual(stopped.exception.code, 3)
                platform.return_value.collect_occupancy.assert_called_once()
                platform.return_value.post_api_json.assert_not_called()
            self.assertIn("登录已失效", output.getvalue())
            self.assertNotIn("等待重试", output.getvalue())

    def test_transient_failure_prints_once_and_recovers_next_cycle(self):
        host = PlatformHost("gpu-203", 0, 2, ("autodl-203-1",), (("autodl-203-1", 0, 2),))
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()) as output:
            with patch("autodl_watcher.main.PlatformBrowserCollector") as platform, \
                 patch("autodl_watcher.main.TelemetryApiCollector") as telemetry:
                platform.return_value.collect.side_effect = [PlatformTransientError("HTTP 503"), [host]]
                platform.return_value.collect_occupancy.return_value = []
                telemetry.return_value.collect.return_value = []
                main(["--config", str(ROOT / "config.yaml"), "--host", "203", "--entry", "1",
                      "--dry-run", "--no-login", "--max-cycles", "2", "--poll-seconds", "0.01", "--runtime-dir", directory])
                platform.return_value.post_api_json.assert_not_called()
            headlines = [line for line in output.getvalue().splitlines() if "] 监控203-1 |" in line]
            self.assertEqual(len(headlines), 2)
            self.assertIn("本轮采集暂时失败", headlines[0])
            self.assertIn("实例状态：", headlines[1])
            self.assertNotIn("登录已失效", output.getvalue())

    def test_two_line_output_refreshes_occupancy_every_cycle_without_history(self):
        config = load_config(ROOT / "config.yaml")
        target = next(item for item in config.auto_start.targets if item.machine_name == "autodl-203-1")
        host = PlatformHost("gpu-203", 0, 2, ("autodl-203-1", "autodl-203-2"),
                            (("autodl-203-1", 0, 2), ("autodl-203-2", 0, 2)))
        def occupancy(name, **kwargs):
            return [OccupancyRecord(datetime.now(), "gpu-203", name, 1, "gpu-1", "V100", True,
                                    f"{target.instance_uuid} (自己)\naaaaaaaaaa-bbbbbbbb (其他用户)", "", "")]
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()) as output:
            with patch("autodl_watcher.main.PlatformBrowserCollector") as platform, \
                 patch("autodl_watcher.main.TelemetryApiCollector") as telemetry:
                platform.return_value.collect.return_value = [host]
                platform.return_value.collect_occupancy.side_effect = occupancy
                platform.return_value.get_account_instances.return_value = [
                    {"instance_uuid": target.instance_uuid, "machine_name": target.machine_name}]
                platform.return_value.get_instance_state.return_value = {
                    "status": "running", "start_mode": "gpu", "host_account_gpu_clear": False}
                telemetry.return_value.collect.side_effect = lambda: [GpuSample("gpu-203", 1, "V100", 0, 1000, 32000, datetime.now())]
                main(["--config", str(ROOT / "config.yaml"), "--host", "203", "--entry", "1",
                      "--dry-run", "--no-login", "--max-cycles", "2", "--poll-seconds", "0.01",
                      "--runtime-dir", directory])
                platform.return_value.post_api_json.assert_not_called()
            text = output.getvalue()
            self.assertEqual(platform.return_value.collect_occupancy.call_count, 4)  # 两轮都读取两个入口，不复用旧名单。
            self.assertEqual(text.count("监控203-1 | 实例状态：有卡运行 | 动作：继续监控"), 2)
            self.assertEqual(text.count("203-1[#1:自己,#1:其他用户]; 203-2[#1:自己,#1:其他用户]"), 2)
            self.assertEqual(text.count("平台空闲：203-1=0/2,203-2=0/2"), 2)
            lines = text.splitlines()
            for index, line in enumerate(lines):
                if "] 监控203-1 |" in line:
                    self.assertIn("平台空闲：", line)
                    self.assertTrue(lines[index + 1].startswith("  203-1["))
            self.assertNotIn("占用更新", text)
            for old in ("占用快照 |", "实例保留=", "物理GPU并发=", "新增事件=", "预选入口=", "开机达标=", "监控203-2 |"):
                self.assertNotIn(old, text)
            self.assertFalse(list(Path(directory).rglob("*.db")))
            self.assertFalse(list(Path(directory).rglob("*.csv")))
            self.assertTrue((Path(directory) / "state.json").exists())


if __name__ == "__main__":
    unittest.main()
