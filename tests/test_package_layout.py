from __future__ import annotations  # v0.5.1 发布目录与一键脚本回归测试。

import unittest
import os
import subprocess
import sys
import runpy
import tempfile
from pathlib import Path
from unittest.mock import patch


class PackageLayoutV051Test(unittest.TestCase):
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

    @unittest.skipUnless(os.name == "nt", "CMD 启动入口仅在 Windows 执行")
    def test_windowless_entry_reaches_pythonw_with_spaced_path(self):
        with tempfile.TemporaryDirectory(prefix="watcher startup ") as directory:
            root = Path(directory)
            (root / "tools").mkdir()
            (root / "START_HERE.vbs").write_bytes((self.root / "START_HERE.vbs").read_bytes())
            (root / "tools/start_gui.cmd").write_bytes((self.root / "tools/start_gui.cmd").read_bytes())
            (root / "tools/launcher.cmd").write_text(
                f'@echo off\nset "PYTHON_EXE={sys.executable}"\nset "PROJECT_DIR=%~dp0..\\"\nexit /b 0\n', encoding="utf-8")
            (root / "tools/gui_entry.py").write_text(
                'from pathlib import Path\nimport sys\n'
                'Path(__file__).with_name("started.txt").write_text(Path(sys.executable).name)\n', encoding="ascii")
            result = subprocess.run(["cscript.exe", "//B", "//Nologo", str(root / "START_HERE.vbs")],
                                    capture_output=True, text=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / "tools/started.txt").read_text(), "pythonw.exe")

    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]

    def test_only_start_here_cmd_is_exposed_at_root(self) -> None:
        root_cmds = sorted(path.name for path in self.root.glob("*.cmd"))
        self.assertEqual(root_cmds, ["START_HERE.cmd"])

    def test_clean_edge_script_does_not_contain_cmd_caret_pipe(self) -> None:
        script = (self.root / "tools" / "clean_watcher_edge.ps1").read_text( encoding="ascii" )
        self.assertNotIn("^|", script)
        self.assertIn("Get-CimInstance", script)
        self.assertIn("Stop-Process", script)

    def test_command_files_are_ascii_and_bom_free(self) -> None:
        command_files = list(self.root.glob("*.cmd")) + list((self.root / "tools").glob("*.cmd"))
        for path in command_files:
            raw = path.read_bytes()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), path.name)
            raw.decode("ascii")

    def test_main_does_not_restore_live_ownership_from_sqlite(self) -> None:
        source = (self.root / "src" / "autodl_watcher" / "main.py").read_text(encoding="utf-8")
        self.assertNotIn("current_instances()", source)
        self.assertNotIn("restored_owned_hint", source)

    def test_start_menu_uses_explicit_labels_not_chained_if_commands(self) -> None:
        source = (self.root / "START_HERE.cmd").read_text(encoding="ascii")
        self.assertIn(":option7", source)
        self.assertNotIn("if errorlevel 7 powershell", source.lower())

    def test_launcher_bypasses_autodl_from_http_proxy(self) -> None:
        source = (self.root / "tools" / "launcher.cmd").read_text(encoding="ascii")
        self.assertIn("private.autodl.com,.autodl.com", source)


if __name__ == "__main__":
    unittest.main()
