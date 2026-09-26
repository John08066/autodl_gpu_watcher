from __future__ import annotations  # v0.5.1 发布目录与一键脚本回归测试。

import unittest
import os
import subprocess
import sys
import runpy
import tempfile
from pathlib import Path
from unittest.mock import patch


class PackageLayoutTest(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "无控制台启动异常通过 Windows 窗口提示")
    def test_startup_error_is_logged_and_reported_without_console(self):
        entry = runpy.run_path(str(self.root / "tools/gui_entry.py"))["main"]
        previous_directory, previous_path = Path.cwd(), sys.path[:]
        original_stdout, original_stderr = sys.stdout, sys.stderr
        try:
            with tempfile.TemporaryDirectory() as directory:
                entry.__globals__["__file__"] = str(Path(directory) / "tools/gui_entry.py")
                with patch("autodl_watcher.gui.main", side_effect=RuntimeError("startup-test")), \
                     patch("ctypes.windll.user32.MessageBoxW") as dialog:
                    entry()
                    dialog.assert_called_once()
                    self.assertIn("startup-test", dialog.call_args.args[1])
                    self.assertIn("startup-test", (Path(directory) / ".ui/startup.log").read_text(encoding="utf-8"))
                    self.assertIs(sys.stdout, original_stdout)
                    self.assertIs(sys.stderr, original_stderr)
                os.chdir(previous_directory)
        finally:
            os.chdir(previous_directory)
            sys.path[:] = previous_path

    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]

    def test_clean_edge_script_does_not_contain_cmd_caret_pipe(self) -> None:
        script = (self.root / "tools" / "clean_watcher_edge.ps1").read_text( encoding="ascii" )
        self.assertNotIn("^|", script)
        self.assertIn("Get-CimInstance", script)
        self.assertIn("Stop-Process", script)

    def test_main_does_not_restore_live_ownership_from_sqlite(self) -> None:
        source = (self.root / "src" / "autodl_watcher" / "main.py").read_text(encoding="utf-8")
        self.assertNotIn("current_instances()", source)
        self.assertNotIn("restored_owned_hint", source)

    def test_frozen_startup_uses_executable_folder_for_logs(self):
        entry = runpy.run_path(str(self.root / "tools/gui_entry.py"))["main"]
        previous_directory, previous_path = Path.cwd(), sys.path[:]
        try:
            with tempfile.TemporaryDirectory() as directory:
                with patch.object(sys, "frozen", True, create=True), \
                     patch.object(sys, "executable", str(Path(directory) / "AutoDLWatcher.exe")), \
                     patch("autodl_watcher.gui.main", side_effect=RuntimeError("frozen-startup")), \
                     patch("ctypes.windll.user32.MessageBoxW") as dialog:
                    entry()
                    self.assertIn("frozen-startup", (Path(directory) / ".ui/startup.log").read_text(encoding="utf-8"))
                    dialog.assert_called_once()
                os.chdir(previous_directory)
        finally:
            os.chdir(previous_directory)
            sys.path[:] = previous_path

    def test_build_entry_uses_explicit_files_without_personal_data(self):
        source = (self.root / "tools/build_exe.py").read_text(encoding="utf-8")
        self.assertIn("datas=[]", source)
        self.assertIn("gui_entry.py", source)
        self.assertIn("worker_entry.py", source)
        self.assertNotIn("--collect-all", source)

    def test_rebuild_preserves_user_configuration_and_login_data(self):
        build = runpy.run_path(str(self.root / "tools/build_exe.py"))["main"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config.yaml").write_text("new defaults", encoding="utf-8")
            release = root / "dist/AutoDLWatcher"
            (release / "runtime/browser_profile").mkdir(parents=True)
            profile = release / "runtime/browser_profile/preserved.txt"
            profile.write_text("user data", encoding="utf-8")
            (release / "config.yaml").write_text("user settings", encoding="utf-8")
            def fake_build(*args, **kwargs):
                staged = root / ".tools/build/release/AutoDLWatcher"
                (staged / "_internal").mkdir(parents=True)
                (staged / "AutoDLWatcher.exe").write_bytes(b"gui")
                (staged / "_internal/AutoDLWorker.exe").write_bytes(b"worker")
            with patch.dict(build.__globals__, ROOT=root), patch("subprocess.run", side_effect=fake_build):
                build()
            self.assertEqual(profile.read_text(encoding="utf-8"), "user data")
            self.assertEqual((release / "config.yaml").read_text(encoding="utf-8"), "user settings")
            self.assertEqual((release / "_internal/AutoDLWorker.exe").read_bytes(), b"worker")

    @unittest.skipUnless(os.name == "nt", "Windows EXE 验收")
    def test_built_worker_runs_without_python_and_never_samples_after_stop(self):
        import struct
        release = self.root / "dist/AutoDLWatcher"
        gui, worker = release / "AutoDLWatcher.exe", release / "_internal/AutoDLWorker.exe"
        if not gui.is_file() or not worker.is_file():
            self.skipTest("尚未生成 EXE；构建后再运行本验收")
        for executable, subsystem in ((gui, 2), (worker, 3)):
            data = executable.read_bytes()
            header = struct.unpack_from("<I", data, 0x3c)[0]
            self.assertEqual(struct.unpack_from("<H", data, header + 24 + 68)[0], subsystem)
        with tempfile.TemporaryDirectory(prefix="autodl exe test ") as directory:
            folder = Path(directory)
            (folder / "config.yaml").write_bytes((self.root / "config.yaml").read_bytes())
            stop = folder / "stop"
            stop.touch()
            env = os.environ.copy()
            env["AUTODL_APP_ROOT"] = str(folder)
            env["PATH"] = str(Path(os.environ["SystemRoot"]) / "System32")
            env.pop("PYTHONPATH", None)
            env.pop("PYTHONHOME", None)
            result = subprocess.run([str(worker), "monitor", "--dry-run", "--no-login",
                                     "--stop-file", str(stop), "--runtime-dir", str(folder)],
                                    cwd=folder, env=env, capture_output=True, text=True,
                                    encoding="utf-8", timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue((folder / "state.json").is_file())
            self.assertIn(str(folder / "config.yaml"), result.stdout)
            self.assertFalse((folder / "runtime/browser_profile").exists())
            self.assertFalse((release / "_internal/runtime").exists())
            self.assertFalse((release / "_internal/.ui").exists())
            self.assertFalse((release / "_internal/.env").exists())


if __name__ == "__main__":
    unittest.main()
