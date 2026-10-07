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
        self.destroy_root()
        self.location.stop()
        self.refresh.stop()
        self.temp.cleanup()

    def destroy_root(self):
        for timer in self.root.tk.call("after", "info"):
            self.root.after_cancel(timer)  # 测试反复创建解释器，先取消旧窗口回调再销毁。
        self.root.destroy()

    def pump(self, condition, timeout=5):
        deadline = time.monotonic() + timeout
        while not condition() and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.02)
        self.assertTrue(condition(), "UI 子进程未在期限内完成")


    def test_ai_settings_is_global_outside_server_pages(self):
        self.root.update()
        button=self.window.ai_settings_button
        self.assertIs(button.master,self.window.notebook.master)
        self.assertNotEqual(button.master,self.window.auto_page)
        self.assertIs(self.window.training.ai.service,self.window.training)
        self.assertEqual(button.cget("text"),"全局 AI 设置")

    def test_icon_settings_is_global_and_does_not_overlap_ai_button(self):
        self.root.deiconify();self.root.update()
        button=self.window.icon_settings_button
        ai=self.window.ai_settings_button
        self.assertIs(button.master,ai.master)
        self.assertLessEqual(button.winfo_rootx()+button.winfo_width(),ai.winfo_rootx())
        dialog=self.window.icons.configure();self.root.update()
        self.assertIn('所有服务器共用',dialog.title())
        dialog.destroy()

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
        self.assertTrue(self.window.save())
        settings = json.loads((self.folder / ".ui/preferences.json").read_text(encoding="utf-8"))
        self.assertEqual(settings["entry"], "autodl-202-4")
        self.assertEqual(settings["user"], "任意用户名")
        self.assertEqual(settings["poll"], "0.5")
        self.assertFalse(self.window.live.get())

    def test_two_line_report_colors_whole_group_and_has_no_statistics_controls(self):
        self.assertFalse(hasattr(self.window, "export_button"))
        self.assertFalse(hasattr(self.window, "usage_check"))
        self.assertEqual(self.window.log.frame.winfo_manager(), "pack")
        headline = "[12:32:02] 监控203-1 | 实例状态：有卡运行 | 动作：继续监控 | 平台空闲：203-1=0/2"
        self.window.events.put(("line", gui.REPORT_PREFIX + json.dumps({"lines": [headline, "  203-1[#0:失败也是名字]"], "level": "success"})))
        self.window._drain()
        self.assertEqual(self.window.status.get(), headline)
        self.assertEqual(self.window.log.get("1.0", "end").count(headline), 1)
        self.assertIn("success", self.window.log.tag_names("1.0"))
        self.assertIn("success", self.window.log.tag_names("2.0"))
        self.assertEqual(str(self.window.status_label.cget("foreground")), "#18733a")
        self.assertFalse(hasattr(self.window, "usage"))
        self.assertEqual(len(self.window.inputs), 2)
        self.assertTrue(self.window.save())
        settings = json.loads((self.folder / ".ui/preferences.json").read_text(encoding="utf-8"))
        self.assertNotIn("usage", settings)

    def test_report_error_and_normal_levels_cover_both_lines(self):
        for level in ("error", "normal"):
            self.window.events.put(("line", gui.REPORT_PREFIX + json.dumps({"lines": [
                "[12:32:02] 监控203-1 | 实例状态：无卡运行 | 动作：等待", "  203-1[#0:成功]"], "level": level})))
        self.window._drain()
        for row, level in ((1, "error"), (2, "error"), (3, "normal"), (4, "normal")):
            self.assertIn(level, self.window.log.tag_names(f"{row}.0"))
        self.assertEqual(self.window.log.cget("wrap"), "none")

    def test_default_interval_and_legacy_preferences_migrate_once(self):
        self.assertEqual(float(self.window.poll.get()), 60)
        preferences = self.folder / ".ui/preferences.json"
        preferences.write_text(json.dumps({"poll": "30", "usage": "300", "user": "保留用户名", "entry": "autodl-203-1"}), encoding="utf-8")
        for expected in ("60", "120"):
            self.destroy_root()
            self.root = tk.Tk()
            self.root.withdraw()
            self.window = gui.WatcherWindow(self.root, debug=True)
            self.assertEqual(self.window.poll.get(), expected)
            self.assertEqual(self.window.user.get(), "保留用户名")
            self.window.poll.set("120")
            self.assertTrue(self.window.save())
        self.assertNotIn("usage", json.loads(preferences.read_text(encoding="utf-8")))

    def test_gpu_bars_use_physical_host_and_mark_stale_or_missing_data(self):
        from datetime import datetime, timedelta
        def descendants(widget):
            return [child for item in widget.winfo_children() for child in [item, *descendants(item)]]
        samples = [{"host": host, "gpu_index": index, "gpu_name": "测试GPU", "util_pct": 30 + index,
                    "memory_used_mb": 16000, "memory_total_mb": 32000, "observed_at": datetime.now().isoformat()}
                   for host, count in (("gpu-203", 2), ("gpu-201", 4)) for index in range(count)]
        self.window.gpu_snapshot = {"samples": samples, "stale_after_seconds": 90}
        for entry, count in (("autodl-203-1", 2), ("autodl-203-2", 2), ("autodl-201-1", 4)):
            self.window.table.selection_set(entry)
            self.window._render_gpus()
            bars = [item for item in descendants(self.window.gpu_panel) if isinstance(item, gui.ttk.Progressbar)]
            self.assertEqual(len(bars), count * 2)
            self.assertEqual([float(item["value"]) for item in bars], [v for i in range(count) for v in (30 + i, 50)])
            self.assertEqual(self.window.selected_entry(), entry)
        self.root.deiconify()
        self.root.update()
        import tkinter.font as tkfont
        font = tkfont.Font(self.root, font=self.window.log.cget("font"))
        self.assertGreaterEqual(self.window.log.winfo_height() - 2 * int(self.window.log.cget("pady")) - 8, 6 * font.metrics("linespace"))
        for sample in samples:
            sample["observed_at"] = (datetime.now() - timedelta(seconds=91)).isoformat()
        self.window._render_gpus()
        self.assertFalse(any(isinstance(item, gui.ttk.Progressbar) for item in descendants(self.window.gpu_panel)))
        self.assertIn("数据已过期", [item.cget("text") for item in descendants(self.window.gpu_panel) if isinstance(item, gui.ttk.Label)])
        self.window.gpu_snapshot = {"samples": [], "error": "采集失败"}
        self.window._render_gpus()
        self.assertEqual(self.window.gpu_panel.winfo_children()[0].cget("text"), "采集失败")

    def test_initial_layout_has_room_for_three_complete_two_line_cycles(self):
        import tkinter.font as tkfont
        self.root.deiconify()
        self.root.update()
        font = tkfont.Font(self.root, font=self.window.log.cget("font"))
        available = self.window.log.winfo_height() - 2 * int(self.window.log.cget("pady")) - 8
        self.assertGreaterEqual(available, 6 * font.metrics("linespace"))
        self.assertEqual((self.root.winfo_width(), self.root.winfo_height()), (1000, 800))
        self.assertLess(self.window.table.winfo_width(), 350)
        self.assertLess(self.window.table.winfo_height(), 170)

    def test_normal_mode_defaults_to_live_and_debug_defaults_to_readonly(self):
        self.assertTrue(self.window.live.get())
        self.assertTrue(self.window.convert_no_gpu.get())
        self.assertIn("--dry-run", self.window._command())  # 未核验真实实例前不启用电源。
        self.window.table.set(self.window.selected_entry(), "target", "可开机")
        self.assertIn("--live", self.window._command())
        self.assertIn("全部无卡实例", self.window.convert_check["text"])
        self.window.live_check.invoke()  # 取消真实开机也取消无卡关机，避免不一致的只读参数。
        self.assertFalse(self.window.live.get())
        self.assertFalse(self.window.convert_no_gpu.get())
        self.assertIn("--dry-run", self.window._command())
        self.destroy_root()
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
