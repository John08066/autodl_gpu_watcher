from __future__ import annotations  # 项目入口脚本 — 直接运行源码树，避免读取过期的 editable 安装包。

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent  # 项目根目录
SRC = ROOT / "src"                     # 源码包所在目录
sys.path.insert(0, str(SRC))           # 优先加载源码，而非 site-packages 中的旧版

from autodl_watcher.main import main  # noqa: E402 — import 在 sys.path 修改之后


if __name__ == "__main__":
    main()
