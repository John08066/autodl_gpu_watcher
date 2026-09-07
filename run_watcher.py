"""
项目入口脚本 — 直接运行源码树，避免读取过期的 editable 安装包。

用法：
    python run_watcher.py [--host HOST] [--entry ENTRY] [--dry-run | --live]

设计意图：
    pip install -e . 后，源码变更不会自动反映到安装包。
    本脚本将 src/ 加入 sys.path，确保始终运行最新的源码，
    与开发调试场景（频繁改代码）完全兼容。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent  # 项目根目录
SRC = ROOT / "src"                     # 源码包所在目录
sys.path.insert(0, str(SRC))           # 优先加载源码，而非 site-packages 中的旧版

from autodl_watcher.main import main  # noqa: E402 — import 在 sys.path 修改之后


if __name__ == "__main__":
    main()
