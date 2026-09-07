import json
from pathlib import Path
import shutil
import sys
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from autodl_watcher import gui


class GuiWindowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        shutil.copyfile(gui.ROOT / "config.yaml", self.folder / "config.yaml")
        self.location = patch.object(gui, "ROOT", self.folder)
        self.location.start()
        self.root = tk.Tk()
        self.root.withdraw()
        self.window = gui.WatcherWindow(self.root)

    def tearDown(self):
        if self.window.process is not None:
            self.window.stop()
            self.pump(lambda: self.window.process is None)
        self.root.destroy()
        self.location.stop()
        self.temp.cleanup()

    def pump(self, condition, timeout=5):
        deadline = time.monotonic() + timeout
        while not condition() and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.02)
        self.assertTrue(condition(), "UI 子进程未在期限内完成")

    def test_selection_and_custom_settings_are_saved(self):
        self.window._populate([("autodl-202-4", "3/3", "只读监控")])
        self.window.user.set("任意用户名")
        self.window.poll.set("0.5")
        self.window.usage.set("75")
        self.assertTrue(self.window.save())
        settings = json.loads((self.folder / ".ui/preferences.json").read_text(encoding="utf-8"))
        self.assertEqual(settings["entry"], "autodl-202-4")
        self.assertEqual(settings["user"], "任意用户名")
        self.assertEqual(settings["poll"], "0.5")
        self.assertFalse(self.window.live.get())

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
        self.assertIn("test heartbeat", self.window.log.get("1.0", "end"))


if __name__ == "__main__":
    unittest.main()
