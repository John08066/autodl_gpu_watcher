from __future__ import annotations  # CLI 参数解析模块的单元测试。

import unittest

from autodl_watcher.cli import normalize_entry, normalize_host


class CliTest(unittest.TestCase):
    def test_normalizes_host_forms(self) -> None:  # 验证测试场景 `normalizes_host_forms` 的预期行为。
        self.assertEqual(normalize_host("203"), "gpu-203")
        self.assertEqual(normalize_host("gpu-203"), "gpu-203")
        self.assertEqual(normalize_host("autodl-203-2"), "gpu-203")

    def test_normalizes_entry_and_checks_host(self) -> None:  # 验证测试场景 `normalizes_entry_and_checks_host` 的预期行为。
        self.assertEqual(normalize_entry("203-2", "gpu-203"), "autodl-203-2")
        self.assertEqual(normalize_entry("2", "gpu-203"), "autodl-203-2")
        with self.assertRaises(ValueError):
            normalize_entry("202-2", "gpu-203")

if __name__ == "__main__":
    unittest.main()
