from __future__ import annotations  # v0.5.1 发布目录与一键脚本回归测试。

import unittest
import os
import subprocess
import tempfile
from pathlib import Path


class PackageLayoutV051Test(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "CMD 启动入口仅在 Windows 执行")
    def test_start_here_reaches_gui_after_successful_launcher(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tools").mkdir()
            (root / "START_HERE.cmd").write_bytes((self.root / "START_HERE.cmd").read_bytes())
            (root / "tools/launcher.cmd").write_text(
                '@echo off\nset "PYTHON_EXE=%~dp0python_stub.cmd"\nexit /b 0\n', encoding="ascii")
            (root / "tools/python_stub.cmd").write_text('@echo GUI_ENTRY %*\n@exit /b 0\n', encoding="ascii")
            result = subprocess.run(["cmd.exe", "/d", "/c", str(root / "START_HERE.cmd")],
                                    input="", capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("GUI_ENTRY -m autodl_watcher.gui", result.stdout)

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
