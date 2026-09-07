"""
状态持久化模块 — 使用 JSON 存储 evaluator 的运行时状态。

设计意图：
    监控进程重启后，evaluator 需要恢复上次的连续采样记录，
    否则会丢失"已经持续空闲了 X 秒"的上下文，导致误判。
    本模块通过 atomic write（先写 .tmp 再 rename）保证写入的安全性。

存储内容：
    - saved_at:             最近一次保存的时间戳
    - capacity_fingerprint: 配置的哈希指纹，配置变更时自动忽略旧状态
    - evaluator:            evaluator 内部状态（各 GPU 的 idle_samples 队列等）
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


class JsonStateStore:
    """JSON 状态文件的读写器，支持原子写入。"""

    def __init__(self, path: Path) -> None:
        """功能：
            初始化 JSON 状态存储器并记录目标状态文件路径。

        参数：
            path (Path)：文件路径、API 相对路径或目标输出路径，具体含义由函数上下文决定。

        返回：
            None：函数通过副作用完成初始化、输出、持久化或资源管理。
        """
        self.path = path

    def load(self) -> dict[str, Any]:
        """功能：
            读取状态文件。文件不存在或损坏时返回空字典。

        参数：
            无。

        返回：
            dict[str, Any]：状态文件内容；文件不存在或无效时返回空字典。
        """
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def save(self, state: dict[str, Any]) -> None:
        """功能：
            原子写入状态文件。先写 .tmp 再 os.replace 重命名。

        参数：
            state (dict[str, Any])：需要恢复或保存的状态字典。

        返回：
            None：函数通过副作用完成初始化、输出、持久化或资源管理。
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temp, self.path)  # 原子替换，避免写入中断损坏文件
