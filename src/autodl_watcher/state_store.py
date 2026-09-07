from __future__ import annotations  # 状态持久化模块 — 使用 JSON 存储 evaluator 的运行时状态。

import json
import os
from pathlib import Path
from typing import Any


class JsonStateStore:  # JSON 状态文件的读写器，支持原子写入。

    def __init__(self, path: Path) -> None:  # 初始化 JSON 状态存储器并记录目标状态文件路径。
        self.path = path

    def load(self) -> dict[str, Any]:  # 读取状态文件。文件不存在或损坏时返回空字典。
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def save(self, state: dict[str, Any]) -> None:  # 原子写入状态文件。先写 .tmp 再 os.replace 重命名。
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text( json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8", )
        os.replace(temp, self.path)  # 原子替换，避免写入中断损坏文件
