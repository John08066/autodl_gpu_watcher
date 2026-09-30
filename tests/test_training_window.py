import json
import queue
import shutil
import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

from autodl_watcher import gui
from autodl_watcher.training import TrainingServer, TrainingTracker, save_servers, parse_progress
from autodl_watcher.training_ui import COLORS
from test_training import snapshot, LOG


def descendants(widget):
    return [child for item in widget.winfo_children() for child in [item, *descendants(item)]]


class TrainingWindowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        shutil.copyfile(gui.ROOT / "config.yaml", self.folder / "config.yaml")
        self.servers = [TrainingServer("s", "203-1", "server", entry="autodl-203-1"), TrainingServer("external", "4090", "other")]
        save_servers(self.folder / ".ui/servers.json", self.servers)
        self.location = patch.object(gui, "ROOT", self.folder)
        self.location.start()
        self.refresh = patch.object(gui.WatcherWindow, "refresh")
        self.refresh.start()
        self.root = tk.Tk()
        self.root.withdraw()
        self.window = gui.WatcherWindow(self.root, debug=True)
        self.window.table.selection_set("autodl-203-1")
        self.window._render_gpus()

    def tearDown(self):
        self.window.training.stop_all()
        try:
            for timer in self.root.tk.call("after", "info"):
                self.root.after_cancel(timer)
            self.root.destroy()
        except tk.TclError:
            pass
        self.location.stop()
        self.refresh.stop()
        self.temp.cleanup()

    def texts(self, pane):
        return [str(item.cget("text")) for item in descendants(pane) if isinstance(item, (tk.Label, gui.ttk.Label, gui.ttk.Button))]

    def test_tabs_and_selection_do_not_start_network_or_power(self):
        self.assertEqual([self.window.notebook.tab(tab, "text") for tab in self.window.notebook.tabs()], ["AutoDL 平台", "4090"])
        self.assertEqual(self.window.title_text.get(), "203-1")
        self.assertEqual(self.window.training_pane.server_id, "s")
        self.window.notebook.select(self.window.external_panes["external"][0])
        self.assertFalse(any(state["active"] for state in self.window.training.states.values()))
        self.assertFalse(any("开机" in text for text in self.texts(self.window.external_panes["external"][2])))
        self.window.table.selection_set("autodl-203-2")
        self.window._render_gpus()
        self.assertEqual(self.window.title_text.get(), "203-2")
        self.assertIsNone(self.window.training_pane.server_id)

    def test_card_states_preserve_metrics_and_disconnection_is_not_idle(self):
        pane = self.window.training_pane
        state = self.window.training.states["s"]
        state["active"] = True
        for text, alive, level, label in [(LOG, True, "success", "正在测试"),
                                         (LOG + "CUDA out of memory", False, "error", "异常中止"),
                                         (LOG + "Stop Training on best Testing metric", False, "idle", "已完成")]:
            with self.subTest(label=label):
                state["snapshot"] = TrainingTracker(self.servers[0]).update(snapshot(text, alive, now=time.time()))
                state["snapshot"]["cards"][0]["advanced_at"] = time.time()
                if level == "success":
                    state["snapshot"]["cards"][0].update(level="success", status="正在测试")
                pane.render()
                self.assertIn(label, self.texts(pane))
                card = pane.content.winfo_children()[-1]
                self.assertEqual(card.cget("background"), COLORS[level][0])
                self.assertTrue(any("AUC 0.8800" in value for value in self.texts(pane)))
                if level == "idle":
                    bar = next(item for item in descendants(card) if isinstance(item, gui.ttk.Progressbar))
                    self.assertEqual(float(bar["value"]), float(bar["maximum"]))
        self.window.training.receive(("s", state["generation"], None, "SSH连接失败"))
        pane.render()
        self.assertIn("SSH连接失败", pane.summary.cget("text"))
        self.assertEqual(pane.content.winfo_children()[-1].cget("background"), COLORS["warning"][0])
        self.assertNotIn("无匹配训练进程", self.texts(pane))
        state["snapshot"] = TrainingTracker(self.servers[0]).update(dict(snapshot(), tasks=[]))
        pane.render()
        self.assertIn("无匹配训练进程", self.texts(pane))

    def test_stop_one_page_does_not_stop_another_and_close_waits_for_read(self):
        for state in self.window.training.states.values():
            state.update(active=True, due=float("inf"))
        self.window.training_pane.stop()
        self.assertFalse(self.window.training.states["s"]["active"])
        self.assertTrue(self.window.training.states["external"]["active"])
        state = self.window.training.states["external"]
        state["busy"] = True
        generation = state["generation"]
        self.window.close()
        self.assertTrue(self.window.closing)
        self.assertTrue(self.root.winfo_exists())
        self.window.training.receive(("external", generation, snapshot(), ""))
        self.assertFalse(state["busy"])
        self.assertIsNone(state["snapshot"])
        self.assertFalse(any(state["active"] for state in self.window.training.states.values()))

    def test_black_gpu_log_and_failure_card_stay_visible_after_disconnect_and_stop(self):
        pane = self.window.training_pane
        state = self.window.training.states["s"]
        state["active"] = True
        value = snapshot(LOG + "RuntimeError: CUDA out of memory")
        value["tasks"][0]["gpu_memory_mb"] = 6144
        state["snapshot"] = state["tracker"].update(value)
        pane.render()
        self.assertTrue(any("6.00 GiB" in text for text in self.texts(pane)))
        self.assertEqual(pane.gpu_log.cget("wrap"), "none")
        self.assertIn("VRAM整卡", pane.gpu_log.get("1.0", "end"))
        first = pane.gpu_log.get("1.0", "end")
        state["busy"] = True
        pane.render()
        self.assertEqual(pane.gpu_log.get("1.0", "end"), first)  # 请求进行中不能重复追加旧快照。
        self.window.training.receive(("s", state["generation"], None, "SSH断连"))
        pane.render()
        self.assertEqual(pane.content.winfo_children()[-1].cget("background"), COLORS["error"][0])
        pane.stop()
        self.assertEqual(pane.content.winfo_children()[-1].cget("background"), COLORS["error"][0])
        self.assertIn("上次数据不作为当前状态", pane.gpu_log.get("1.0", "end"))

    def test_instance_and_ssh_columns_are_independent_and_unknown_is_readonly(self):
        window = self.window
        window._populate([("autodl-203-1", "0/2", "可开机"), ("autodl-203-2", "0/2", "可开机"), ("autodl-202-2", "3/3", "无实例")])
        self.assertEqual(window.table.set("autodl-203-1", "training"), "待连接")
        self.assertEqual(window.table.set("autodl-203-2", "training"), "未配置")
        window.live.set(True); window.convert_no_gpu.set(True)
        window.table.selection_set("autodl-202-2")
        self.assertIn("--dry-run", window._command())
        self.assertNotIn("--convert-no-gpu", window._command())
        window.table.selection_set("autodl-203-1")
        self.assertIn("--live", window._command())
        state = window.training.states["s"]
        state.update(active=True, snapshot=state["tracker"].update(snapshot()))
        window._training_columns()
        self.assertEqual(window.table.set("autodl-203-1", "training"), "已连接")

    def test_malformed_json_metrics_cannot_break_following_progress(self):
        text = '{"event":"train","metrics":null}\n' + '{"event":"train","epoch":3,"metrics":{"loss":0.5}}'
        result = parse_progress(text)
        self.assertEqual(result["train"]["metrics"], {"loss": .5})
        self.assertEqual(result["epoch"], 3)


if __name__ == "__main__":
    unittest.main()
