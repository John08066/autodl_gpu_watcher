"""HTTP断连、限流、连接释放及电源门控回归；全部使用本地模拟。"""
import io
import json
import socket
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch

import requests

from autodl_watcher.collectors.telemetry import TelemetryApiCollector, TelemetryUnavailable
from autodl_watcher.config import TelemetryConfig
from autodl_watcher.gui import ROOT
from autodl_watcher.main import main
from autodl_watcher.models import PlatformHost
from autodl_watcher.session import SESSION_PREFIX


class TelemetryRecoveryTest(unittest.TestCase):
    def collector(self):
        collector = TelemetryApiCollector(TelemetryConfig(
            endpoint="http://127.0.0.1/api/latest", timeout_seconds=2,
            max_attempts=2, retry_delay_seconds=0, only_online=True))
        self.addCleanup(collector.close)
        return collector

    def response(self, status=200, retry_after=None):
        response = Mock(status_code=status, headers={})
        response.json.return_value = {"items": []}
        if retry_after is not None:
            response.headers["Retry-After"] = retry_after
        if status >= 400:
            response.raise_for_status.side_effect = requests.HTTPError(str(status), response=response)
        return response

    def test_backoff_caps_attempts_blocks_requests_and_resets_on_success(self):
        collector = self.collector()
        collector.config = replace(collector.config, max_attempts=100)
        collector._session = Mock()
        collector._session.get.side_effect = requests.ConnectionError("remote closed")
        now = 1000.0
        with patch("autodl_watcher.collectors.telemetry.time.monotonic") as clock:
            for i, delay in enumerate((60, 120, 240, 300, 300), 1):
                clock.return_value = now
                with self.assertRaisesRegex(TelemetryUnavailable, f"本轮 2 次请求，连续 {i} 轮失败"):
                    collector.collect()
                self.assertEqual(collector._retry_at, now + delay)
                self.assertEqual(collector._session.get.call_count, i * 2)
                with self.assertRaisesRegex(TelemetryUnavailable, "本轮未发送请求"):
                    collector.collect()
                self.assertEqual(collector._session.get.call_count, i * 2)
                now += delay
            self.assertEqual(collector._session.close.call_count, 10)
            clock.return_value = now
            response = self.response()
            collector._session.get.side_effect = None
            collector._session.get.return_value = response
            self.assertEqual(collector.collect(), [])
            self.assertEqual(collector._failed_rounds, 0)
            self.assertEqual(collector._retry_at, 0)
            response.close.assert_called_once()

    def test_rate_limit_and_retry_after_do_not_retry_immediately(self):
        for status, header, delay in ((429, None, 60), (429, "1200", 1200),
                                      (503, "180", 180), (429, "invalid", 60)):
            with self.subTest(status=status, header=header):
                collector = self.collector()
                response = self.response(status, header)
                collector._session = Mock()
                collector._session.get.return_value = response
                with patch("autodl_watcher.collectors.telemetry.time.monotonic", return_value=1000):
                    with self.assertRaises(TelemetryUnavailable):
                        collector.collect()
                    self.assertEqual(collector._retry_at, 1000 + delay)
                    with self.assertRaises(TelemetryUnavailable):
                        collector.collect()
                collector._session.get.assert_called_once()
                response.close.assert_called_once()

    def test_http_date_retry_after(self):
        response = self.response(503, format_datetime(datetime.now(timezone.utc) + timedelta(seconds=600)))
        self.assertTrue(598 <= TelemetryApiCollector._retry_after(response) <= 600)

    def test_invalid_json_closes_response_and_waits(self):
        collector = self.collector()
        collector._session = Mock()
        response = self.response()
        response.json.side_effect = ValueError("invalid JSON")
        collector._session.get.return_value = response
        with self.assertRaisesRegex(TelemetryUnavailable, "invalid JSON"):
            collector.collect()
        response.close.assert_called_once()
        collector._session.get.assert_called_once()

    def test_real_socket_disconnect_then_recovery_and_keepalive(self):
        peers = []
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def log_message(self, *_args):
                pass
            def do_GET(self):
                peers.append(self.client_address)
                if len(peers) <= 2:
                    self.close_connection = True
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                body = b'{"items": []}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        collector = self.collector()
        collector._session.trust_env = False
        collector.config = replace(collector.config, endpoint=f"http://127.0.0.1:{server.server_port}/latest")
        try:
            with self.assertRaises(TelemetryUnavailable) as error:
                collector.collect()
            self.assertIsInstance(error.exception.__cause__, requests.ConnectionError)
            self.assertIn("RemoteDisconnected", str(error.exception))
            self.assertEqual(len(peers), 2)
            with self.assertRaises(TelemetryUnavailable):
                collector.collect()
            self.assertEqual(len(peers), 2)
            collector._retry_at = 0  # 模拟等待结束；不实际睡眠60秒。
            self.assertEqual(collector.collect(), [])
            self.assertEqual(collector.collect(), [])
            self.assertEqual(len(peers), 4)
            self.assertEqual(peers[2], peers[3])  # 恢复后复用健康TCP连接。
        finally:
            collector.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_live_mode_failure_preserves_login_and_skips_power(self):
        host = PlatformHost("gpu-203", 1, 2, ("autodl-203-1",), (("autodl-203-1", 1, 2),))
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()) as output, \
             patch("autodl_watcher.main.PlatformBrowserCollector") as platform, \
             patch("autodl_watcher.main.TelemetryApiCollector") as telemetry, \
             patch("autodl_watcher.main.AvailabilityEvaluator.evaluate") as evaluate, \
             patch("autodl_watcher.main.interactive_login") as login:
            platform.return_value.collect.return_value = [host]
            platform.return_value.collect_occupancy.return_value = []
            telemetry.return_value.collect.side_effect = TelemetryUnavailable("Telemetry HTTP 冷却中；本轮未发送请求")
            main(["--config", str(ROOT / "config.yaml"), "--host", "203", "--entry", "1",
                  "--live", "--no-login", "--max-cycles", "1", "--runtime-dir", directory])
            self.assertIn("跳过开机，等待重试", output.getvalue())
            statuses = [json.loads(line[len(SESSION_PREFIX):])["status"] for line in output.getvalue().splitlines()
                        if line.startswith(SESSION_PREFIX)]
            self.assertEqual(statuses, ["valid"])
            evaluate.assert_not_called()
            platform.return_value.post_api_json.assert_not_called()
            login.assert_not_called()
            telemetry.return_value.close.assert_called_once()
            platform.return_value.close.assert_called_once()
