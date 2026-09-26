from __future__ import annotations  # 登录启动方式的单元测试。

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autodl_watcher.login import edge_executable_candidates, find_edge_executable


class LoginTest(unittest.TestCase):
    def test_candidates_include_standard_program_files_location(self) -> None:
        with patch.dict( os.environ, {"PROGRAMFILES(X86)": r"C:\Program Files (x86)"}, clear=True, ), patch("autodl_watcher.login.shutil.which", return_value=None):
            candidates = edge_executable_candidates()

        self.assertIn(
            Path(r"C:\Program Files (x86)")
            / "Microsoft"
            / "Edge"
            / "Application"
            / "msedge.exe",
            candidates,
        )

    def test_find_edge_returns_first_existing_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            edge = Path(temp_dir) / "msedge.exe"
            edge.write_bytes(b"")
            with patch(
                "autodl_watcher.login.edge_executable_candidates",
                return_value=(Path(temp_dir) / "missing.exe", edge),
            ):
                self.assertEqual(find_edge_executable(), edge)

    def test_find_edge_returns_none_when_not_installed(self) -> None:
        with patch(
            "autodl_watcher.login.edge_executable_candidates",
            return_value=(Path("missing-edge.exe"),),
        ):
            self.assertIsNone(find_edge_executable())


class LoginProfileCleanupV051Test(unittest.TestCase):
    def test_remove_stale_profile_locks(self) -> None:
        from autodl_watcher.login import remove_stale_profile_locks

        with tempfile.TemporaryDirectory() as temp_dir:
            profile = Path(temp_dir)
            for name in ("SingletonLock", "SingletonCookie", "lockfile", "DevToolsActivePort"):
                (profile / name).write_text("stale", encoding="utf-8")
            (profile / "Cookies").write_text("keep", encoding="utf-8")

            remove_stale_profile_locks(profile)

            self.assertFalse((profile / "SingletonLock").exists())
            self.assertFalse((profile / "SingletonCookie").exists())
            self.assertFalse((profile / "lockfile").exists())
            self.assertFalse((profile / "DevToolsActivePort").exists())
            self.assertTrue((profile / "Cookies").exists())


if __name__ == "__main__":
    unittest.main()

class LoginV051Test(unittest.TestCase):
    def test_login_profile_is_separate_from_browser_profile(self) -> None:
        from autodl_watcher.login import login_profile_dir

        browser = Path("runtime") / "browser_profile"
        self.assertEqual(login_profile_dir(browser), Path("runtime") / "native_login_profile")

    def test_automation_flags_detected_from_edge_command_lines(self) -> None:
        from autodl_watcher.login import automation_flags_present

        with patch(
            "autodl_watcher.login.edge_process_command_lines",
            return_value=[ r'"msedge.exe" --user-data-dir=D:\runtime\login_profile --new-window' ],
        ):
            self.assertFalse(automation_flags_present(Path(r"D:\runtime\login_profile")))

        with patch(
            "autodl_watcher.login.edge_process_command_lines",
            return_value=[
                r'"msedge.exe" --no-sandbox --remote-debugging-pipe --user-data-dir=D:\runtime\login_profile'
            ],
        ):
            self.assertTrue(automation_flags_present(Path(r"D:\runtime\login_profile")))

class LoginWorkflowV051Test(unittest.TestCase):
    def test_interactive_login_uses_clean_profile_then_syncs(self) -> None:
        from types import SimpleNamespace
        from autodl_watcher.login import interactive_login

        with tempfile.TemporaryDirectory() as temp_dir:
            browser_profile = Path(temp_dir) / "browser_profile"
            config = SimpleNamespace(
                platform=SimpleNamespace(
                    user_data_dir=browser_profile,
                    page_url="https://private.autodl.test/landing",
                )
            )
            edge_path = Path(temp_dir) / "msedge.exe"
            edge_path.write_bytes(b"")

            with patch("autodl_watcher.login.find_edge_executable", return_value=edge_path), \
                 patch("autodl_watcher.login.terminate_profile_edge_processes") as terminate, \
                 patch("autodl_watcher.login.launch_native_edge") as launch, \
                 patch("autodl_watcher.login.automation_flags_present", return_value=False), \
                 patch("autodl_watcher.login.sync_login_profile_to_browser") as sync, \
                 patch("autodl_watcher.login.time.sleep"), \
                 patch("builtins.input", return_value=""):
                interactive_login(config, reason="manual")

            clean_profile = browser_profile.parent / "native_login_profile"
            launch.assert_called_once_with(edge_path, clean_profile, config.platform.page_url)
            sync.assert_called_once_with(clean_profile, browser_profile)
            self.assertGreaterEqual(terminate.call_count, 3)

    def test_sync_login_profile_replaces_browser_profile_without_lock_files(self) -> None:
        from autodl_watcher.login import sync_login_profile_to_browser

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            login_profile = root / "login_profile"
            browser_profile = root / "browser_profile"
            login_profile.mkdir()
            browser_profile.mkdir()
            (login_profile / "Cookies").write_text("fresh", encoding="utf-8")
            (login_profile / "SingletonLock").write_text("lock", encoding="utf-8")
            (browser_profile / "Cookies").write_text("stale", encoding="utf-8")

            with patch("autodl_watcher.login.terminate_profile_edge_processes"), \
                 patch("autodl_watcher.login.time.sleep"):
                sync_login_profile_to_browser(login_profile, browser_profile)

            self.assertEqual((browser_profile / "Cookies").read_text(encoding="utf-8"), "fresh")
            self.assertFalse((browser_profile / "SingletonLock").exists())


class LoginSessionVerificationTest(unittest.TestCase):
    def verify_login(self, error=None, exit_code=None):
        import io
        import json
        from contextlib import redirect_stdout
        from types import SimpleNamespace
        from autodl_watcher.login import main
        from autodl_watcher.session import SESSION_PREFIX

        config = SimpleNamespace(platform=SimpleNamespace())
        with redirect_stdout(io.StringIO()) as output, \
             patch("autodl_watcher.login.load_config", return_value=config), \
             patch("autodl_watcher.login.interactive_login") as login, \
             patch("autodl_watcher.collectors.PlatformBrowserCollector") as collector:
            collector.return_value.collect.side_effect = error
            if exit_code is None:
                main()
            else:
                with self.assertRaises(SystemExit) as stopped:
                    main()
                self.assertEqual(stopped.exception.code, exit_code)
            collector.return_value.collect.assert_called_once()
            login.assert_called_once_with(config, reason="manual")
            collector.return_value.close.assert_called_once()
        return [json.loads(line[len(SESSION_PREFIX):]) for line in output.getvalue().splitlines()
                if line.startswith(SESSION_PREFIX)]

    def test_copied_profile_requires_successful_api_verification(self):
        statuses = self.verify_login()
        self.assertEqual([item["status"] for item in statuses], ["checking", "valid"])
        self.assertTrue(statuses[-1]["checked_at"])

    def test_expired_profile_reports_invalid_without_claiming_success(self):
        from autodl_watcher.collectors import PlatformAuthenticationError
        statuses = self.verify_login(PlatformAuthenticationError("HTTP 401"), 3)
        self.assertEqual([item["status"] for item in statuses], ["checking", "invalid"])

    def test_network_failure_cannot_be_classified_as_expired_login(self):
        from autodl_watcher.collectors import PlatformTransientError
        statuses = self.verify_login(PlatformTransientError("response body unavailable"), 4)
        self.assertEqual([item["status"] for item in statuses], ["checking", "unknown"])


    def test_profile_sync_failure_reports_unknown_without_unhandled_worker_error(self):
        import io
        from contextlib import redirect_stdout
        from types import SimpleNamespace
        from autodl_watcher.login import main
        with redirect_stdout(io.StringIO()) as output, \
             patch("autodl_watcher.login.load_config", return_value=SimpleNamespace(platform=SimpleNamespace())), \
             patch("autodl_watcher.login.interactive_login", side_effect=EOFError("login confirmation closed")), \
             patch("autodl_watcher.collectors.PlatformBrowserCollector") as collector:
            with self.assertRaises(SystemExit) as stopped:
                main()
            self.assertEqual(stopped.exception.code, 1)
            self.assertIn('"status": "unknown"', output.getvalue())
            self.assertNotIn('"status": "valid"', output.getvalue())
            collector.assert_not_called()
