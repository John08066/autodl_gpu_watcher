"""
采集器模块的单元测试。

测试覆盖：
    - canonical_host 映射（入口名 → 物理主机名）
    - 平台 payload 解析（多入口聚合、空闲/总数计算）
    - Telemetry payload 解析（时区处理、离线主机过滤）
    - 两层门控过滤（仅平台有空位的主机通过）
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, Mock, patch

import requests
from datetime import datetime

from autodl_watcher.collectors.platform import (
    PlatformAuthenticationError,
    PlatformBrowserCollector,
    PlatformTransientError,
    canonical_host,
    parse_platform_payload,
)
from autodl_watcher.collectors.telemetry import (
    TelemetryApiCollector,
    filter_samples_to_platform_candidates,
    parse_telemetry_payload,
)
from autodl_watcher.config import TelemetryConfig
from autodl_watcher.models import GpuSample, PlatformHost


class CollectorParsingTest(unittest.TestCase):
    def test_canonical_host_maps_access_entry_not_gpu_index(self) -> None:
        """功能：
            验证测试场景 `canonical_host_maps_access_entry_not_gpu_index` 的预期行为。

        参数：
            无。

        返回：
            None：无返回值。
        """
        self.assertEqual(canonical_host("autodl-202-2"), "gpu-202")
        self.assertEqual(canonical_host("autodl-202-4"), "gpu-202")
        self.assertEqual(canonical_host("gpu-202"), "gpu-202")

    def test_any_visible_entry_with_idle_opens_gate(self) -> None:
        """功能：
            验证测试场景 `any_visible_entry_with_idle_opens_gate` 的预期行为。

        参数：
            无。

        返回：
            None：无返回值。
        """
        payload = {
            "data": {
                "list": [
                    {"machine_name": "autodl-202-2", "gpu": {"total": 3, "idle": 0}},
                    {"machine_name": "autodl-202-4", "gpu": {"total": 3, "idle": 3}},
                ]
            }
        }
        hosts = parse_platform_payload(payload, aggregation="max")
        self.assertEqual(len(hosts), 1)
        self.assertEqual(hosts[0].host, "gpu-202")
        self.assertEqual(hosts[0].free_count, 3)
        self.assertEqual(hosts[0].total_count, 3)
        self.assertEqual(
            hosts[0].source_slots,
            (("autodl-202-2", 0, 3), ("autodl-202-4", 3, 3)),
        )

    def test_all_visible_entries_zero_close_gate(self) -> None:
        """功能：
            验证测试场景 `all_visible_entries_zero_close_gate` 的预期行为。

        参数：
            无。

        返回：
            None：无返回值。
        """
        payload = {
            "data": {
                "list": [
                    {"machine_name": "autodl-203-1", "gpu": {"total": 2, "idle": 0}},
                    {"machine_name": "autodl-203-2", "gpu": {"total": 2, "idle": 0}},
                ]
            }
        }
        host = parse_platform_payload(payload, aggregation="max")[0]
        self.assertEqual(host.free_count, 0)

    def test_telemetry_filters_offline_hosts(self) -> None:
        """功能：
            验证测试场景 `telemetry_filters_offline_hosts` 的预期行为。

        参数：
            无。

        返回：
            None：无返回值。
        """
        payload = {
            "items": [
                {
                    "host": "gpu-202",
                    "gpu_index": 0,
                    "host_status": "online",
                    "name": "NVIDIA TITAN RTX",
                    "util_gpu": 0,
                    "mem_used_mb": 312,
                    "mem_total_mb": 24576,
                    "received_at": "2026-07-20T08:00:53Z",
                },
                {
                    "host": "gpu-203",
                    "gpu_index": 0,
                    "host_status": "offline",
                    "name": "Tesla V100",
                    "util_gpu": 100,
                    "mem_used_mb": 20000,
                    "mem_total_mb": 32768,
                    "received_at": "2026-07-20T07:22:29Z",
                },
            ]
        }
        samples = parse_telemetry_payload(payload, only_online=True)
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0].host, "gpu-202")

    def test_only_selected_platform_host_survives(self) -> None:
        """功能：
            验证测试场景 `only_selected_platform_host_survives` 的预期行为。

        参数：
            无。

        返回：
            None：无返回值。
        """
        now = datetime(2026, 7, 20, 16, 0, 0)
        samples = [
            GpuSample("gpu-202", 0, "TITAN RTX", 0, 300, 24576, now),
            GpuSample("gpu-176", 0, "A100", 0, 300, 40960, now),
        ]
        authorized = [PlatformHost("gpu-202", 1, 1, ("autodl-202-2",))]
        accepted, blocked, unauthorized = filter_samples_to_platform_candidates(samples, authorized)
        self.assertEqual([(item.host, item.gpu_index) for item in accepted], [("gpu-202", 0)])
        self.assertEqual(blocked, ())
        self.assertEqual(unauthorized, ("gpu-176",))

    def test_visible_host_with_closed_entry_gate_does_not_reach_stage_two(self) -> None:
        """功能：
            验证测试场景 `visible_host_with_closed_entry_gate_does_not_reach_stage_two` 的预期行为。

        参数：
            无。

        返回：
            None：无返回值。
        """
        now = datetime(2026, 7, 20, 16, 0, 0)
        samples = [GpuSample("gpu-201", 2, "A100", 0, 300, 40960, now)]
        platform = [PlatformHost("gpu-201", 0, 1, ("autodl-201-1",))]
        accepted, blocked, unauthorized = filter_samples_to_platform_candidates(samples, platform)
        self.assertEqual(accepted, [])
        self.assertEqual(blocked, ("gpu-201",))
        self.assertEqual(unauthorized, ())


class TelemetryRetryTest(unittest.TestCase):
    """Telemetry 瞬时故障重试逻辑的回归测试。"""

    @staticmethod
    def _config() -> TelemetryConfig:
        return TelemetryConfig(
            endpoint="https://example.test/api/latest",
            timeout_seconds=20,
            max_attempts=2,
            retry_delay_seconds=2.0,
            only_online=True,
        )

    @staticmethod
    def _success_response() -> Mock:
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "items": [
                {
                    "host": "gpu-203",
                    "gpu_index": 0,
                    "host_status": "online",
                    "name": "Tesla V100",
                    "util_gpu": 0,
                    "mem_used_mb": 1024,
                    "mem_total_mb": 32768,
                    "received_at": "2026-07-29T20:00:00+08:00",
                }
            ]
        }
        return response

    @patch("autodl_watcher.collectors.telemetry.time.sleep")
    def test_timeout_once_then_retry_succeeds(self, sleep_mock: Mock) -> None:
        collector = TelemetryApiCollector(self._config())
        collector._session = Mock()
        collector._session.get.side_effect = [
            requests.ReadTimeout("slow response"),
            self._success_response(),
        ]

        samples = collector.collect()

        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0].host, "gpu-203")
        self.assertEqual(collector._session.get.call_count, 2)
        self.assertEqual(
            collector._session.get.call_args_list[0].kwargs["timeout"],
            20,
        )
        sleep_mock.assert_called_once_with(2.0)

    @patch("autodl_watcher.collectors.telemetry.time.sleep")
    def test_two_timeouts_raise_only_after_second_attempt(self, sleep_mock: Mock) -> None:
        collector = TelemetryApiCollector(self._config())
        collector._session = Mock()
        collector._session.get.side_effect = [
            requests.ReadTimeout("first"),
            requests.ReadTimeout("second"),
        ]

        with self.assertRaisesRegex(requests.ReadTimeout, "second"):
            collector.collect()

        self.assertEqual(collector._session.get.call_count, 2)
        sleep_mock.assert_called_once_with(2.0)

    @patch("autodl_watcher.collectors.telemetry.time.sleep")
    def test_http_400_is_not_retried(self, sleep_mock: Mock) -> None:
        collector = TelemetryApiCollector(self._config())
        collector._session = Mock()
        response = Mock()
        response.status_code = 400
        http_error = requests.HTTPError("bad request", response=response)
        response.raise_for_status.side_effect = http_error
        collector._session.get.return_value = response

        with self.assertRaises(requests.HTTPError):
            collector.collect()

        collector._session.get.assert_called_once()
        sleep_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()

class PlatformBrowserRecoveryV050Test(unittest.TestCase):
    def test_failed_browser_launch_releases_playwright_state(self) -> None:
        from pathlib import Path
        from autodl_watcher.collectors.platform import PlatformBrowserCollector
        from autodl_watcher.config import PlatformConfig

        config = PlatformConfig(
            page_url="https://private.example.test",
            api_path_contains="/machine/list",
            api_base_url="https://private.example.test",
            browser_channel="msedge",
            user_data_dir=Path("runtime/browser_profile"),
            headless=True,
            response_timeout_seconds=20,
            max_attempts=2,
            retry_delay_seconds=2.0,
            aggregation="max",
        )
        collector = PlatformBrowserCollector(config)
        playwright = Mock()
        playwright.chromium.launch_persistent_context.side_effect = RuntimeError("profile locked")
        manager = Mock()
        manager.start.return_value = playwright

        with patch("autodl_watcher.collectors.platform.sync_playwright", return_value=manager):
            with self.assertRaisesRegex(RuntimeError, "profile locked"):
                collector.start()

        playwright.stop.assert_called_once()
        self.assertIsNone(collector._playwright)
        self.assertIsNone(collector._context)
        self.assertIsNone(collector._page)


class PlatformClassificationV052Test(unittest.TestCase):
    @staticmethod
    def _config():
        from pathlib import Path
        from autodl_watcher.config import PlatformConfig

        return PlatformConfig(
            page_url="https://private.autodl.com/console/machine",
            api_path_contains="/api/v2/machine/list",
            api_base_url="https://private.autodl.com",
            browser_channel="msedge",
            user_data_dir=Path("runtime/browser_profile"),
            headless=True,
            response_timeout_seconds=20,
            max_attempts=2,
            retry_delay_seconds=0.0,
            aggregation="max",
        )

    def test_console_timeout_is_transient_not_authentication_failure(self) -> None:
        collector = PlatformBrowserCollector(self._config())
        collector.start = Mock()
        page = Mock()
        page.url = "https://private.autodl.com/console/machine"
        cm = MagicMock()
        cm.__enter__.side_effect = __import__("playwright.sync_api", fromlist=["TimeoutError"]).TimeoutError("slow")
        page.expect_response.return_value = cm
        collector._page = page

        with self.assertRaises(PlatformTransientError):
            collector._collect_machine_list_via_browser()

    def test_login_url_timeout_is_authentication_failure(self) -> None:
        collector = PlatformBrowserCollector(self._config())
        collector.start = Mock()
        page = Mock()
        page.url = "https://private.autodl.com/login"
        cm = MagicMock()
        cm.__enter__.side_effect = __import__("playwright.sync_api", fromlist=["TimeoutError"]).TimeoutError("slow")
        page.expect_response.return_value = cm
        collector._page = page

        with self.assertRaises(PlatformAuthenticationError):
            collector._collect_machine_list_via_browser()

    def test_cached_machine_list_uses_direct_api_without_page_reload(self) -> None:
        collector = PlatformBrowserCollector(self._config())
        collector._authorization = "Bearer token"
        collector._machine_list_payload = {"page_index": 1}
        expected = [PlatformHost("gpu-203", 1, 2, ("autodl-203-2",))]
        with patch.object(collector, "_collect_machine_list_direct", return_value=expected) as direct, \
             patch.object(collector, "_collect_machine_list_via_browser") as browser, \
             patch.object(collector, "start"):
            actual = collector.collect()

        self.assertEqual(actual, expected)
        direct.assert_called_once_with()
        browser.assert_not_called()

    def test_request_listener_captures_token_and_payload_before_response(self) -> None:
        collector = PlatformBrowserCollector(self._config())
        request = Mock()
        request.url = "https://private.autodl.com/api/v2/machine/list"
        request.method = "POST"
        request.all_headers.return_value = {"authorization": "Bearer abc"}
        request.post_data_json = {"page_index": 1, "page_size": 10}

        collector._capture_platform_request(request)

        self.assertEqual(collector._authorization, "Bearer abc")
        self.assertEqual(collector._machine_list_payload, {"page_index": 1, "page_size": 10})
