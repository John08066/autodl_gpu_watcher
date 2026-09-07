from __future__ import annotations  # CLI 参数解析模块的单元测试。

import unittest

from autodl_watcher.cli import normalize_entry, normalize_host, select_cli_target
from autodl_watcher.config import AutoStartConfig, AutoStartTarget


class CliTest(unittest.TestCase):
    def setUp(self) -> None:  # 为当前测试用例准备共享配置、时间基准或测试对象。
        self.a = AutoStartTarget("gpu-203", "autodl-203-1", "a", "gpu", 10, True)
        self.b = AutoStartTarget("gpu-203", "autodl-203-2", "b", "gpu", 20, True)
        self.config = AutoStartConfig(True, "gpu-203", False, True, 1, 5, 1, (self.b, self.a))

    def test_normalizes_host_forms(self) -> None:  # 验证测试场景 `normalizes_host_forms` 的预期行为。
        self.assertEqual(normalize_host("203"), "gpu-203")
        self.assertEqual(normalize_host("gpu-203"), "gpu-203")
        self.assertEqual(normalize_host("autodl-203-2"), "gpu-203")

    def test_normalizes_entry_and_checks_host(self) -> None:  # 验证测试场景 `normalizes_entry_and_checks_host` 的预期行为。
        self.assertEqual(normalize_entry("203-2", "gpu-203"), "autodl-203-2")
        self.assertEqual(normalize_entry("2", "gpu-203"), "autodl-203-2")
        with self.assertRaises(ValueError):
            normalize_entry("202-2", "gpu-203")

    def test_default_target_uses_priority(self) -> None:  # 验证测试场景 `default_target_uses_priority` 的预期行为。
        self.assertEqual(select_cli_target(self.config, "gpu-203"), self.a)
        self.assertEqual(select_cli_target(self.config, "gpu-203", "autodl-203-2"), self.b)


if __name__ == "__main__":
    unittest.main()
