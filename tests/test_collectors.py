from __future__ import annotations  # 采集器模块的单元测试。

import unittest
from unittest.mock import MagicMock, Mock, patch

import requests
from datetime import datetime

from autodl_watcher.collectors.platform import (
    OccupancySnapshotMismatchError,
    PlatformAuthenticationError,
    PlatformBrowserCollector,
    PlatformTransientError,
    canonical_host,
    parse_platform_payload,
    parse_occupancy_cells,
    validate_occupancy_snapshot,
)
from autodl_watcher.collectors.telemetry import (
    TelemetryApiCollector,
    filter_samples_to_platform_candidates,
    parse_telemetry_payload,
)
from autodl_watcher.config import TelemetryConfig
from autodl_watcher.models import GpuSample, OccupancyRecord, PlatformHost


class CollectorParsingTest(unittest.TestCase):
    def test_canonical_host_maps_access_entry_not_gpu_index(self) -> None:  # 验证测试场景 `canonical_host_maps_access_entry_not_gpu_index` 的预期行为。
        self.assertEqual(canonical_host("autodl-202-2"), "gpu-202")
        self.assertEqual(canonical_host("autodl-202-4"), "gpu-202")
        self.assertEqual(canonical_host("gpu-202"), "gpu-202")

    def test_any_visible_entry_with_idle_opens_gate(self) -> None:  # 验证测试场景 `any_visible_entry_with_idle_opens_gate` 的预期行为。
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
        self.assertEqual( hosts[0].source_slots, (("autodl-202-2", 0, 3), ("autodl-202-4", 3, 3)), )

    def test_all_visible_entries_zero_close_gate(self) -> None:  # 验证测试场景 `all_visible_entries_zero_close_gate` 的预期行为。
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

    def test_telemetry_filters_offline_hosts(self) -> None:  # 验证测试场景 `telemetry_filters_offline_hosts` 的预期行为。
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

    def test_only_selected_platform_host_survives(self) -> None:  # 验证测试场景 `only_selected_platform_host_survives` 的预期行为。
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

    def test_visible_host_with_closed_entry_gate_does_not_reach_stage_two(self) -> None:  # 验证测试场景 `visible_host_with_closed_entry_gate_does_not_reach_stage_two` 的预期行为。
        now = datetime(2026, 7, 20, 16, 0, 0)
        samples = [GpuSample("gpu-201", 2, "A100", 0, 300, 40960, now)]
        platform = [PlatformHost("gpu-201", 0, 1, ("autodl-201-1",))]
        accepted, blocked, unauthorized = filter_samples_to_platform_candidates(samples, platform)
        self.assertEqual(accepted, [])
        self.assertEqual(blocked, ("gpu-201",))
        self.assertEqual(unauthorized, ())


class TelemetryRetryTest(unittest.TestCase):  # Telemetry 瞬时故障重试逻辑的回归测试。

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
        self.assertEqual( collector._session.get.call_args_list[0].kwargs["timeout"], 20, )
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


class PlatformApiResponseCompatibilityV053Test(unittest.TestCase):  # v0.5.3: direct Playwright APIResponse does not expose response.request.

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

    def test_parse_direct_api_response_without_request_attribute(self) -> None:
        collector = PlatformBrowserCollector(self._config())
        collector._authorization = "Bearer cached-token"
        collector._machine_list_payload = {"page_index": 1, "page_size": 10}

        class FakeApiResponse:
            status = 200
            ok = True

            @staticmethod
            def json():
                return {
                    "code": "Success",
                    "data": {
                        "list": [ { "machine_name": "autodl-203-2", "gpu": {"idle": 1, "total": 2}, } ]
                    },
                    "msg": "",
                }

        response = FakeApiResponse()
        self.assertFalse(hasattr(response, "request"))

        hosts = collector._parse_machine_list_response(response)

        self.assertEqual(len(hosts), 1)
        self.assertEqual(hosts[0].host, "gpu-203")
        self.assertEqual(hosts[0].free_count, 1)
        self.assertEqual(hosts[0].total_count, 2)
        self.assertEqual(collector._authorization, "Bearer cached-token")
        self.assertEqual(collector._machine_list_payload, {"page_index": 1, "page_size": 10})

    def test_direct_api_collect_with_apiresponse_without_request_attribute(self) -> None:
        collector = PlatformBrowserCollector(self._config())
        collector.start = Mock()
        collector._authorization = "Bearer cached-token"
        collector._machine_list_payload = {"page_index": 1, "page_size": 10}
        collector._context = Mock()

        class FakeApiResponse:
            status = 200
            ok = True

            @staticmethod
            def json():
                return {
                    "code": "Success",
                    "data": {
                        "list": [ { "machine_name": "autodl-203-2", "gpu": {"idle": 1, "total": 2}, } ]
                    },
                    "msg": "",
                }

        collector._context.request.post.return_value = FakeApiResponse()
        hosts = collector._collect_machine_list_direct()

        self.assertEqual(len(hosts), 1)
        self.assertEqual(hosts[0].host, "gpu-203")
        collector._context.request.post.assert_called_once()


class OccupancyValidationV054Test(unittest.TestCase):
    @staticmethod
    def _record(machine: str, gpu_index: int, occupied: bool, user: str = "") -> OccupancyRecord:
        return OccupancyRecord(
            observed_at=datetime(2026, 8, 9, 15, 0, 0),
            host="gpu-203",
            machine_name=machine,
            gpu_index=gpu_index,
            gpu_uuid=f"gpu-{gpu_index}",
            gpu_name="Tesla V100",
            occupied=occupied,
            instance_id=(f"inst-{gpu_index}" if occupied else ""),
            user=(user if occupied else ""),
            started_at_text=("2026-08-09 14:00:00" if occupied else ""),
        )

    def test_rejects_cross_entry_occupancy_pattern(self) -> None:
        records = [  # 203-1 平台显示 2/2 空闲，却读到了 203-2 的两张占用数据。
            self._record("autodl-203-1", 0, True, "炼丹师6912"),
            self._record("autodl-203-1", 1, True, "何太急"),
        ]
        with self.assertRaises(OccupancySnapshotMismatchError):
            validate_occupancy_snapshot(
                records,
                machine_name="autodl-203-1",
                expected_idle=2,
                expected_total=2,
            )

    def test_accepts_matching_empty_entry(self) -> None:
        records = [ self._record("autodl-203-1", 0, False), self._record("autodl-203-1", 1, False), ]
        actual = validate_occupancy_snapshot(
            records,
            machine_name="autodl-203-1",
            expected_idle=2,
            expected_total=2,
        )
        self.assertEqual(actual, records)

    def test_rejects_wrong_row_count(self) -> None:
        records = [self._record("autodl-203-2", 0, True, "何太急")]
        with self.assertRaises(OccupancySnapshotMismatchError):
            validate_occupancy_snapshot(
                records,
                machine_name="autodl-203-2",
                expected_idle=0,
                expected_total=2,
            )

    def test_extra_empty_row_requires_matching_telemetry_indices(self):
        records = [self._record("autodl-202-4", index, False) for index in range(4)]
        with self.assertRaises(OccupancySnapshotMismatchError):
            validate_occupancy_snapshot(records, machine_name="autodl-202-4", expected_idle=3, expected_total=3)
        actual = validate_occupancy_snapshot(records, machine_name="autodl-202-4", expected_idle=3,
                                             expected_total=3, expected_gpu_indices={0, 1, 2})
        self.assertEqual([item.gpu_index for item in actual], [0, 1, 2])

    def test_extra_occupied_row_is_never_discarded(self):
        records = [self._record("autodl-202-4", index, index == 3, "user") for index in range(4)]
        with self.assertRaises(OccupancySnapshotMismatchError):
            validate_occupancy_snapshot(records, machine_name="autodl-202-4", expected_idle=3,
                                        expected_total=3, expected_gpu_indices={0, 1, 2})

    def test_incomplete_telemetry_cannot_relax_row_validation(self):
        records = [self._record("autodl-202-4", index, False) for index in range(4)]
        with self.assertRaises(OccupancySnapshotMismatchError):
            validate_occupancy_snapshot(records, machine_name="autodl-202-4", expected_idle=3,
                                        expected_total=3, expected_gpu_indices={0, 1})


class OccupancySixColumnTest(unittest.TestCase):
    def test_six_column_row_preserves_instance_and_start_time(self):
        record = parse_occupancy_cells(
            ["0", "gpu-uuid", "Tesla V100", "\u662f", "instance-1", "2026-09-25 01:00:00"],
            observed_at=datetime(2026, 9, 25, 1, 0), host="gpu-203", machine_name="autodl-203-1",
        )
        self.assertIsNotNone(record)
        self.assertTrue(record.occupied)
        self.assertEqual(record.instance_id, "instance-1")
        self.assertEqual(record.user, "")
        self.assertEqual(record.started_at_text, "2026-09-25 01:00:00")

    def test_old_seven_column_row_preserves_user_and_start_time(self):
        record = parse_occupancy_cells(
            ["0", "gpu-uuid", "Tesla V100", "\u662f", "instance-1", "owner",
             "2026-09-25 01:00:00"],
            observed_at=datetime(2026, 9, 25, 1, 0), host="gpu-203", machine_name="autodl-203-1",
        )
        self.assertIsNotNone(record)
        self.assertEqual(record.user, "owner")
        self.assertEqual(record.started_at_text, "2026-09-25 01:00:00")

    def test_rechecks_full_rows_until_occupancy_counts_match(self):
        collector = PlatformBrowserCollector(PlatformClassificationV052Test._config())
        collector._page = Mock()
        modal = Mock()
        rows = Mock()
        rows.count.return_value = 3
        header = Mock()
        header.locator.return_value.all_inner_texts.return_value = []
        data_rows = []
        for index in (0, 1):
            row = Mock()
            row.locator.return_value.all_inner_texts.side_effect = [
                [str(index), f"gpu-{index}", "Tesla V100", "\u5426", "-", "-"],
                [str(index), f"gpu-{index}", "Tesla V100", "\u662f", f"instance-{index}",
                 "2026-09-25 01:00:00"],
            ]
            data_rows.append(row)
        rows.nth.side_effect = lambda index: [header, *data_rows][index]
        modal.locator.return_value = rows
        with patch.object(collector, "collect"), \
             patch.object(collector, "_find_occupancy_action", return_value=Mock()), \
             patch.object(collector, "_find_visible_occupancy_modal", return_value=modal), \
             patch.object(collector, "_close_occupancy_modal"):
            records = collector.collect_occupancy("autodl-203-1", expected_idle=0, expected_total=2)
        self.assertEqual([record.occupied for record in records], [True, True])
        collector._page.wait_for_timeout.assert_any_call(100)

    def test_persistent_occupancy_mismatch_raises_after_timeout(self):
        collector = PlatformBrowserCollector(PlatformClassificationV052Test._config())
        collector._page = Mock()
        modal = Mock()
        rows = Mock()
        rows.count.return_value = 3
        row_mocks = []
        for cells in ([], ["0", "gpu-0", "Tesla V100", "\u5426", "-", "-"],
                      ["1", "gpu-1", "Tesla V100", "\u5426", "-", "-"]):
            row = Mock()
            row.locator.return_value.all_inner_texts.return_value = cells
            row_mocks.append(row)
        rows.nth.side_effect = lambda index: row_mocks[index]
        modal.locator.return_value = rows
        with patch.object(collector, "collect"), \
             patch.object(collector, "_find_occupancy_action", return_value=Mock()), \
             patch.object(collector, "_find_visible_occupancy_modal", return_value=modal), \
             patch.object(collector, "_close_occupancy_modal"), \
             patch("autodl_watcher.collectors.platform.time.monotonic", side_effect=[0.0, 0.0, 21.0]):
            with self.assertRaisesRegex(OccupancySnapshotMismatchError, "应为 2"):
                collector.collect_occupancy("autodl-203-1", expected_idle=0, expected_total=2)
        collector._page.wait_for_timeout.assert_any_call(100)

    def test_missing_gpu_rows_still_fail_after_timeout(self):
        collector = PlatformBrowserCollector(PlatformClassificationV052Test._config())
        collector._page = Mock()
        modal = Mock()
        rows = Mock()
        rows.count.return_value = 1
        rows.nth.return_value.locator.return_value.all_inner_texts.return_value = []
        modal.locator.return_value = rows
        with patch.object(collector, "collect"), \
             patch.object(collector, "_find_occupancy_action", return_value=Mock()), \
             patch.object(collector, "_find_visible_occupancy_modal", return_value=modal), \
             patch.object(collector, "_close_occupancy_modal"), \
             patch("autodl_watcher.collectors.platform.time.monotonic", side_effect=[0.0, 0.0, 21.0]):
            with self.assertRaises(OccupancySnapshotMismatchError):
                collector.collect_occupancy("autodl-203-1", expected_idle=2, expected_total=2)
        collector._page.wait_for_timeout.assert_any_call(100)

    def test_waits_for_delayed_six_column_rows(self):
        collector = PlatformBrowserCollector(PlatformClassificationV052Test._config())
        collector._page = Mock()
        action = Mock()
        modal = Mock()
        rows = Mock()
        rows.count.side_effect = [1, 3]
        cell_rows = [
            [],
            ["0", "gpu-0", "Tesla V100", "\u662f", "instance-1", "2026-09-25 01:00:00"],
            ["1", "gpu-1", "Tesla V100", "\u5426", "-", "-"],
        ]
        row_mocks = []
        for cells in cell_rows:
            row = Mock()
            row.locator.return_value.all_inner_texts.return_value = cells
            row_mocks.append(row)
        rows.nth.side_effect = lambda index: row_mocks[index]
        modal.locator.return_value = rows
        with patch.object(collector, "collect"), \
             patch.object(collector, "_find_occupancy_action", return_value=action), \
             patch.object(collector, "_find_visible_occupancy_modal", return_value=modal), \
             patch.object(collector, "_close_occupancy_modal"):
            records = collector.collect_occupancy("autodl-203-1", expected_idle=1, expected_total=2)
        self.assertEqual([item.gpu_index for item in records], [0, 1])
        self.assertEqual([item.user for item in records], ["", ""])
        collector._page.wait_for_timeout.assert_any_call(100)


class InstanceStateCollectorTest(unittest.TestCase):
    @staticmethod
    def _collector(tenant_payload=None):
        collector = PlatformBrowserCollector(PlatformClassificationV052Test._config())
        collector.start = Mock()
        collector._context = Mock()
        page = Mock()
        page.url = "https://private.autodl.com/console/instance"
        request = Mock()
        request.url = "https://private.autodl.com/api/v2/instance/list"
        request.method = "POST"
        request.post_data_json = (
            {"tenant_uuid": "tenant-123", "page_index": 1, "page_size": 10}
            if tenant_payload is None else tenant_payload
        )
        manager = MagicMock()
        manager.__enter__.return_value.value = request
        page.expect_request.return_value = manager
        collector._context.new_page.return_value = page
        return collector, page

    @staticmethod
    def _response(rows, total):
        return {"code": "Success", "data": {"list": rows, "result_total": total}}

    def test_get_account_instances_paginates_and_returns_only_state_fields(self):
        collector, page = self._collector()
        first = [
            {"instance_uuid": f"other-{index}", "machine_name": "autodl-204-1",
             "status": "shutdown", "start_mode": "gpu", "secret": "ignored"}
            for index in range(10)
        ]
        last = {"instance_uuid": "target", "machine_name": "autodl-203-1",
                "status": "running", "start_mode": "non_gpu", "secret": "ignored"}
        collector.post_api_json = Mock(side_effect=[self._response(first, 11), self._response([last], 11)])
        rows = collector.get_account_instances()
        self.assertEqual(len(rows), 11)
        self.assertEqual(rows[-1], {"instance_uuid": "target", "machine_name": "autodl-203-1",
                                    "status": "running", "start_mode": "non_gpu"})
        self.assertEqual(collector.post_api_json.call_count, 2)
        page.close.assert_called_once()

    def test_exact_uuid_on_second_page_and_close_temporary_page(self):
        collector, page = self._collector()
        first = [
            {"instance_uuid": f"other-{index}", "machine_name": "autodl-204-1",
             "status": "shutdown", "start_mode": "gpu"}
            for index in range(10)
        ]
        second = [
            {"instance_uuid": "target", "machine_name": "autodl-203-1",
             "status": "running", "start_mode": "non_gpu"},
            {"instance_uuid": "sibling", "machine_name": "autodl-203-2",
             "status": "shutdown", "start_mode": "gpu"},
        ]
        collector.post_api_json = Mock(side_effect=[
            self._response(first, 12), self._response(second, 12),
        ])

        self.assertEqual(
            collector.get_instance_state("target", "autodl-203-1"),
            {"status": "running", "start_mode": "non_gpu", "host_account_gpu_clear": True},
        )
        self.assertEqual(collector.post_api_json.call_count, 2)
        self.assertEqual(collector.post_api_json.call_args_list[1].args, (
            "/api/v2/instance/list",
            {"tenant_uuid": "tenant-123", "page_index": 2, "page_size": 10},
        ))
        predicate = page.expect_request.call_args.args[0]
        self.assertTrue(predicate(page.expect_request.return_value.__enter__.return_value.value))
        self.assertFalse(predicate(Mock(url="https://other.example/api/v2/instance/list", method="POST")))
        page.goto.assert_called_once_with(
            "https://private.autodl.com/console/instance", wait_until="domcontentloaded",
        )
        page.close.assert_called_once()

    def test_other_gpu_or_unknown_mode_on_same_host_blocks_clear(self):
        target = {"instance_uuid": "target", "machine_name": "autodl-203-1",
                  "status": "running", "start_mode": "non_gpu"}
        for status, mode in (("running", "gpu"), ("starting", "gpu"),
                             ("shutting_down", "gpu"), ("running", None)):
            with self.subTest(status=status, mode=mode):
                collector, _ = self._collector()
                sibling = {"instance_uuid": "sibling", "machine_name": "autodl-203-2",
                           "status": status, "start_mode": mode}
                collector.post_api_json = Mock(return_value=self._response([target, sibling], 2))
                self.assertEqual(
                    collector.get_instance_state("target", "autodl-203-1"),
                    {"status": "running", "start_mode": "non_gpu", "host_account_gpu_clear": False},
                )

    def test_running_gpu_on_other_host_does_not_block_clear(self):
        collector, _ = self._collector()
        rows = [
            {"instance_uuid": "target", "machine_name": "autodl-203-1",
             "status": "running", "start_mode": "non_gpu"},
            {"instance_uuid": "other-host", "machine_name": "autodl-204-1",
             "status": "running", "start_mode": "gpu"},
        ]
        collector.post_api_json = Mock(return_value=self._response(rows, 2))
        self.assertTrue(collector.get_instance_state("target", "autodl-203-1")["host_account_gpu_clear"])

    def test_duplicate_non_target_uuid_across_pages_fails_closed(self):
        collector, page = self._collector()
        first = [
            {"instance_uuid": f"other-{index}", "machine_name": "autodl-204-1",
             "status": "shutdown", "start_mode": "gpu"}
            for index in range(10)
        ]
        second = [
            {"instance_uuid": "target", "machine_name": "autodl-203-1",
             "status": "running", "start_mode": "non_gpu"},
            {"instance_uuid": "other-0", "machine_name": "autodl-203-2",
             "status": "running", "start_mode": "gpu"},
        ]
        collector.post_api_json = Mock(side_effect=[
            self._response(first, 12), self._response(second, 12),
        ])
        with self.assertRaises(PlatformTransientError):
            collector.get_instance_state("target", "autodl-203-1")
        self.assertEqual(collector.post_api_json.call_count, 2)
        page.close.assert_called_once()

    def test_active_row_without_valid_machine_name_blocks_clear(self):
        target = {"instance_uuid": "target", "machine_name": "autodl-203-1",
                  "status": "running", "start_mode": "non_gpu"}
        for bad_name in ("", "203-2", "autodl-unknown"):
            with self.subTest(bad_name=bad_name):
                collector, _ = self._collector()
                unknown = {"instance_uuid": "unknown", "machine_name": bad_name,
                           "status": "running", "start_mode": "gpu"}
                collector.post_api_json = Mock(return_value=self._response([target, unknown], 2))
                self.assertFalse(
                    collector.get_instance_state("target", "autodl-203-1")["host_account_gpu_clear"]
                )

    def test_wrong_machine_for_valid_uuid_fails_closed(self):
        collector, page = self._collector()
        collector.post_api_json = Mock(return_value=self._response([
            {"instance_uuid": "target", "machine_name": "autodl-203-2",
             "status": "running", "start_mode": "non_gpu"}], 1))
        with self.assertRaisesRegex(PlatformTransientError, "入口名不匹配"):
            collector.get_instance_state("target", "autodl-203-1")
        page.close.assert_called_once()

    def test_missing_or_duplicate_uuid_is_not_a_shutdown_signal(self):
        for rows in (
            [{"instance_uuid": "target-other", "status": "shutdown", "start_mode": "gpu"}],
            [
                {"instance_uuid": "target", "status": "shutdown", "start_mode": "gpu"},
                {"instance_uuid": "target", "status": "running", "start_mode": "non_gpu"},
            ],
        ):
            with self.subTest(rows=rows):
                collector, page = self._collector()
                collector.post_api_json = Mock(return_value=self._response(rows, len(rows)))
                with self.assertRaises(PlatformTransientError):
                    collector.get_instance_state("target", "autodl-203-1")
                page.close.assert_called_once()

    def test_bad_list_or_business_failure_fails_closed(self):
        responses = (
            {"code": "Failure", "data": {"list": [], "result_total": 0}},
            {"code": "Success", "data": {"list": [], "result_total": 1}},
            {"code": "Success", "data": {"list": [], "result_total": "1"}},
            self._response([{"instance_uuid": "target", "status": "running"}], 1),
        )
        for response in responses:
            with self.subTest(response=response):
                collector, page = self._collector()
                collector.post_api_json = Mock(return_value=response)
                with self.assertRaises(PlatformTransientError):
                    collector.get_instance_state("target", "autodl-203-1")
                page.close.assert_called_once()

    def test_missing_tenant_never_calls_instance_api(self):
        collector, page = self._collector({"page_index": 1})
        collector.post_api_json = Mock()
        with self.assertRaisesRegex(PlatformTransientError, "tenant_uuid"):
            collector.get_instance_state("target", "autodl-203-1")
        collector.post_api_json.assert_not_called()
        page.close.assert_called_once()

    def test_login_redirect_is_distinct_from_transient_timeout(self):
        timeout_type = __import__("playwright.sync_api", fromlist=["TimeoutError"]).TimeoutError
        for url, error_type in (
            ("https://private.autodl.com/login", PlatformAuthenticationError),
            ("https://private.autodl.com/console/instance", PlatformTransientError),
        ):
            with self.subTest(url=url):
                collector, page = self._collector()
                page.url = url
                page.expect_request.return_value.__enter__.side_effect = timeout_type("slow")
                collector.post_api_json = Mock()
                with self.assertRaises(error_type):
                    collector.get_instance_state("target", "autodl-203-1")
                collector.post_api_json.assert_not_called()
                page.close.assert_called_once()
