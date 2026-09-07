"""v0.5.1 发布目录与一键脚本回归测试。"""
from __future__ import annotations

import unittest
from pathlib import Path


class PackageLayoutV051Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]

    def test_only_start_here_cmd_is_exposed_at_root(self) -> None:
        root_cmds = sorted(path.name for path in self.root.glob("*.cmd"))
        self.assertEqual(root_cmds, ["START_HERE.cmd"])

    def test_clean_edge_script_does_not_contain_cmd_caret_pipe(self) -> None:
        script = (self.root / "tools" / "clean_watcher_edge.ps1").read_text(
            encoding="ascii"
        )
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


if __name__ == "__main__":
    unittest.main()
