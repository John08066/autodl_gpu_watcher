import json
from pathlib import Path
import shutil
import sys
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

from autodl_watcher import gui


class GuiWindowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        shutil.copyfile(gui.ROOT / "config.yaml", self.folder / "config.yaml")
        self.location = patch.object(gui, "ROOT", self.folder)
        self.location.start()
        self.refresh = patch.object(gui.WatcherWindow, "refresh")
        self.refresh_mock = self.refresh.start()  # 窗口启动会自动只读刷新；本测试不得连接真实服务。
        self.root = tk.Tk()
        self.root.withdraw()
        self.window = gui.WatcherWindow(self.root)

    def tearDown(self):
        if self.window.process is not None:
            self.window.stop()
            self.pump(lambda: self.window.process is None)
        self.root.destroy()
        self.location.stop()
        self.refresh.stop()
        self.temp.cleanup()

    def pump(self, condition, timeout=5):
        deadline = time.monotonic() + timeout
        while not condition() and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.02)
        self.assertTrue(condition(), "UI 子进程未在期限内完成")


    def test_startup_schedules_readonly_session_check(self):
        self.root.update()
        self.refresh_mock.assert_called_once()
        self.assertEqual(self.window.session_status, "unknown")
        self.assertEqual(self.window.session_label.winfo_manager(), "pack")

    def test_authentication_and_transient_failures_restore_buttons_and_preserve_reason(self):
        for status, message in [("invalid", "登录已失效，请重新登录"),
                                ("unknown", "网络或响应错误，无法确认会话")]:
            with self.subTest(status=status):
                payload = json.dumps({"status": status, "message": message, "checked_at": "2026-09-26T16:00:00"})
                code = f"import sys; print({gui.SESSION_PREFIX + payload!r},flush=True); sys.exit(3)"
                self.window._launch([sys.executable, "-u", "-c", code], "discover")
                self.pump(lambda: self.window.process is None)
                self.assertEqual(self.window.session_status, status)
                self.assertEqual(self.window.status.get(), message)
                self.assertIn("2026-09-26T16:00:00", self.window.session_text.get())
                self.assertEqual(str(self.window.login_button["state"]), "normal")
                self.assertEqual(str(self.window.refresh_button["state"]), "normal")

    def test_pipe_read_and_cleanup_errors_cannot_leave_buttons_locked(self):
        class BrokenPipe:
            def __iter__(self):
                raise OSError("pipe read failed")
            def close(self):
                raise OSError("pipe close failed")
        process = Mock(stdout=BrokenPipe(), stdin=BrokenPipe())
        process.poll.return_value = None
        def stopped_wait():
            process.terminate.assert_called_once()
            return 7
        process.wait.side_effect = stopped_wait
        self.window.process, self.window.job = process, "discover"
        self.window._session("checking", "正在核验")
        self.window._busy(True)
        self.window._read(process)
        self.window._drain()
        self.assertIsNone(self.window.process)
        self.assertEqual(str(self.window.refresh_button["state"]), "normal")
        self.assertIn("后台日志读取失败", self.window.log.get("1.0", "end"))
        self.assertEqual(self.window.session_status, "unknown")
        self.assertIn("核验未完成", self.window.session_text.get())
        process.wait.assert_called_once()

    def test_worker_exit_without_validation_cannot_leave_session_checking(self):
        for job, code in [("discover", 3), ("discover", 0), ("monitor", 0), ("login", 4)]:
            with self.subTest(job=job, code=code):
                self.window.process, self.window.job = Mock(), job
                self.window._session("checking", "正在核验")
                self.window._busy(True)
                self.window.events.put(("exit", code))
                self.window._drain()
                self.assertEqual(self.window.session_status, "unknown")
                self.assertIn("核验未完成", self.window.status.get())
                self.assertEqual(str(self.window.login_button["state"]), "normal")

    def test_export_exit_does_not_change_session_status(self):
        self.window.process, self.window.job = Mock(), "export"
        self.window._session("checking", "先前的核验状态")
        self.window._busy(True)
        self.window.events.put(("exit", 1))
        self.window._drain()
        self.assertEqual(self.window.session_status, "checking")
        self.assertEqual(self.window.session_message, "先前的核验状态")

    def test_monitor_pipe_failure_requests_safe_stop_before_waiting(self):
        stdout = Mock()
        stdout.__iter__ = Mock(side_effect=OSError("pipe read failed"))
        process = Mock(stdout=stdout)
        def stopped_wait():
            self.assertTrue(self.window.stop_file.exists())
            stdout.close.assert_called_once()
            return 7
        process.wait.side_effect = stopped_wait
        self.window.job = "monitor"
        self.window._read(process)
        process.terminate.assert_not_called()
        self.assertEqual(self.window.events.get()[0], "line")
        self.assertEqual(self.window.events.get(), ("exit", 7))

    def test_login_pipe_failure_does_not_interrupt_profile_sync(self):
        stdout = Mock()
        stdout.__iter__ = Mock(side_effect=OSError("pipe read failed"))
        process = Mock(stdout=stdout)
        process.wait.return_value = 7
        self.window.job = "login"
        self.window._read(process)
        process.terminate.assert_not_called()
        stdout.close.assert_not_called()
        self.assertFalse(self.window.stop_file.exists())

    def test_bad_message_does_not_stop_event_pump_or_lose_exit(self):
        self.window.process, self.window.job = Mock(), "discover"
        self.window._busy(True)
        self.window.events.put(("line", gui.DATA_PREFIX + "{broken"))
        self.window._drain()
        self.window.events.put(("line", gui.SESSION_PREFIX + json.dumps(
            {"status": "invalid", "message": "登录已失效", "checked_at": "2026-09-26T16:00:00"})))
        self.window.events.put(("exit", 3))
        self.pump(lambda: self.window.process is None)
        self.assertEqual(self.window.status.get(), "登录已失效")
        self.assertEqual(str(self.window.login_button["state"]), "normal")

    def test_login_profile_sync_is_pending_until_real_validation_message(self):
        checking = gui.SESSION_PREFIX + json.dumps(
            {"status": "checking", "message": "资料已同步，等待核验", "checked_at": "2026-09-26T16:00:00"})
        valid = gui.SESSION_PREFIX + json.dumps(
            {"status": "valid", "message": "主机接口核验成功", "checked_at": "2026-09-26T16:00:01"})
        code = f"print({checking!r},flush=True); input(); print({valid!r},flush=True)"
        self.window._launch([sys.executable, "-u", "-c", code], "login")
        self.pump(lambda: "资料已同步" in self.window.session_text.get())
        self.assertEqual(self.window.session_status, "checking")
        self.window.finish_login()
        self.pump(lambda: self.window.process is None)
        self.assertEqual(self.window.session_status, "valid")
        self.assertIn("16:00:01", self.window.session_text.get())

    def test_selection_and_custom_settings_are_saved(self):
        self.window._populate([("autodl-202-4", "3/3", "只读监控")])
        self.window.live.set(False)
        self.window.convert_no_gpu.set(False)
        self.window.user.set("任意用户名")
        self.window.poll.set("0.5")
        self.window.usage.set("75")
        self.assertTrue(self.window.save())
        settings = json.loads((self.folder / ".ui/preferences.json").read_text(encoding="utf-8"))
        self.assertEqual(settings["entry"], "autodl-202-4")
        self.assertEqual(settings["user"], "任意用户名")
        self.assertEqual(settings["poll"], "0.5")
        self.assertFalse(self.window.live.get())

    def test_statistics_switch_preserves_log_output_and_saves_preference(self):
        self.assertTrue(self.window.usage_tracking.get())
        self.assertEqual(self.window.log.frame.winfo_manager(), "pack")
        self.assertEqual(self.window.export_button.winfo_manager(), "pack")
        self.window.usage_tracking.set(False)
        self.window._toggle_usage()
        self.assertEqual(self.window.log.frame.winfo_manager(), "pack")
        self.assertEqual(self.window.export_button.winfo_manager(), "")
        self.window.events.put(("line", "连接正常，实时输出仍然可见\n"))
        self.window._drain()
        self.assertIn("实时输出仍然可见", self.window.log.get("1.0", "end"))
        self.assertIn("success", self.window.log.tag_names("1.0"))
        self.assertIn("--no-usage-report", self.window._command())
        self.assertTrue(self.window.save())
        settings = json.loads((self.folder / ".ui/preferences.json").read_text(encoding="utf-8"))
        self.assertFalse(settings["usage_tracking"])
        self.root.destroy()
        self.root = tk.Tk()
        self.root.withdraw()
        self.window = gui.WatcherWindow(self.root)
        self.assertFalse(self.window.usage_tracking.get())
        self.assertEqual(self.window.log.frame.winfo_manager(), "pack")
        self.assertEqual(self.window.export_button.winfo_manager(), "")

    def test_normal_mode_defaults_to_live_and_debug_defaults_to_readonly(self):
        self.assertTrue(self.window.live.get())
        self.assertTrue(self.window.convert_no_gpu.get())
        self.assertIn("--live", self.window._command())
        self.assertIn("全部无卡实例", self.window.convert_check["text"])
        self.window.live_check.invoke()  # 取消真实开机也取消无卡关机，避免不一致的只读参数。
        self.assertFalse(self.window.live.get())
        self.assertFalse(self.window.convert_no_gpu.get())
        self.assertIn("--dry-run", self.window._command())
        self.root.destroy()
        self.root = tk.Tk()
        self.root.withdraw()
        self.window = gui.WatcherWindow(self.root, debug=True)
        self.assertFalse(self.window.live.get())
        self.assertFalse(self.window.convert_no_gpu.get())
        self.assertIn("--dry-run", self.window._command())
        self.assertNotIn("--convert-no-gpu", self.window._command())

    def test_checked_indicator_is_tick_and_output_has_event_colors(self):
        self.assertEqual(self.window.live_check["style"], "Watcher.TCheckbutton")
        style = gui.ttk.Style(self.root)
        self.assertIn("Watcher.indicator", style.element_names())
        unchecked, checked = self.window.check_images
        self.assertEqual(unchecked.get(5, 9), (255, 255, 255))
        self.assertEqual(checked.get(5, 9), (36, 92, 196))
        self.assertEqual(checked.get(5, 5), (255, 255, 255))  # 左上角无斜线：不是原先的X图形。
        self.assertEqual(checked.get(12, 6), (36, 92, 196))
        self.window.events.put(("line", "Telemetry容错：单次超时20秒；瞬时失败重试\n"))
        self.window.events.put(("line", "本人 GPU 占用 有（本轮弹窗）\n"))
        self.window.events.put(("line", "连接断开，采集失败\n"))
        self.window._drain()
        self.assertNotIn("error", self.window.log.tag_names("1.0"))
        self.assertIn("success", self.window.log.tag_names("2.0"))
        self.assertIn("error", self.window.log.tag_names("3.0"))
        self.assertEqual(self.window.log.tag_cget("success", "foreground"), "#79e69d")
        self.assertEqual(self.window.log.tag_cget("error", "foreground"), "#ff8181")
        self.assertEqual(self.window.log.cget("foreground"), "#e2e8f0")

    def test_background_output_stop_and_restart(self):
        code = ("import pathlib,sys,time; p=pathlib.Path(sys.argv[1]); "
                "print('WATCHER_STATUS test heartbeat', flush=True)\n"
                "while not p.exists(): time.sleep(0.02)\n")
        for _ in range(2):
            self.window.stop_file.unlink(missing_ok=True)
            self.window._launch([sys.executable, "-u", "-c", code, str(self.window.stop_file)], "monitor")
            self.pump(lambda: self.window.status.get() == "test heartbeat")
            self.assertEqual(str(self.window.start_button["state"]), "disabled")
            self.window.stop()
            self.pump(lambda: self.window.process is None)
            self.assertEqual(str(self.window.start_button["state"]), "normal")


if __name__ == "__main__":
    unittest.main()
