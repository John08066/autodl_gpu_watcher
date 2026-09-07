"""
CLI 参数解析与主机名/入口名归一化。

职责：
    将用户输入的简短标识（如 "203"、"2"、"auto"）转换为
    系统内部统一的 fully-qualified 名称（如 "gpu-203"、"autodl-203-2"）。

设计意图：
    用户不需要记住完整的 machine_name 格式。本模块通过正则匹配
    和主机归属校验，避免拼写错误导致的目标失配。
"""
from __future__ import annotations

import re

from .config import AutoStartConfig, AutoStartTarget


# 匹配 203 / gpu-203 / autodl-203-1 → 提取 203
_HOST_RE = re.compile(r"^(?:gpu-|autodl-)?(\d+)(?:-\d+)?$", re.IGNORECASE)
# 匹配 203-2 / autodl-203-2 → 提取 (203, 2)
_ENTRY_RE = re.compile(r"^(?:autodl-)?(\d+)-(\d+)$", re.IGNORECASE)


def normalize_host(value: str) -> str:
    """功能：
        将用户输入归一化为规范主机名。

    参数：
        value (str)：待规范化、解析或转换的输入值。

    返回：
        str：处理后的文本。

    补充说明：
        示例：
            "203"        → "gpu-203"
            "gpu-203"    → "gpu-203"
            "autodl-203-2" → "gpu-203"
    """
    text = value.strip()
    match = _HOST_RE.match(text)
    if not match:
        raise ValueError(f"无法识别主机参数：{value}")
    return f"gpu-{match.group(1)}"


def normalize_entry(value: str | None, host: str) -> str | None:
    """功能：
        将用户输入归一化为规范入口名。

    参数：
        value (str | None)：用户输入的入口参数，例如 `auto`、`2` 或 `203-2`。
        host (str)：已经规范化的目标物理主机，用于检查入口归属。

    返回：
        str | None：函数计算或构造出的结果。

    补充说明：
        策略：
            None 或 "auto" → 返回 None，表示让 autostart 自动选择
            "2"           → 根据 host 补全为 "autodl-203-2"
            "203-2"       → 直接转为 "autodl-203-2"
            "autodl-203-2" → 保持不变

        校验：入口必须属于指定的 host，否则抛 ValueError。
    """
    if value is None:
        return None
    text = value.strip()
    if text.lower() == "auto":
        return None
    if text.isdigit():
        # 纯数字 → 自动补全为 "主机号-入口号"
        host_number = host.removeprefix("gpu-")
        text = f"{host_number}-{text}"
    match = _ENTRY_RE.match(text)
    if not match:
        raise ValueError(f"无法识别入口参数：{value}；示例：auto、2、203-2")
    entry_host = f"gpu-{match.group(1)}"
    if entry_host != host:
        raise ValueError(f"入口 {value} 不属于目标主机 {host}")
    return f"autodl-{match.group(1)}-{match.group(2)}"


def select_cli_targets(
    config: AutoStartConfig,
    host: str,
    machine_name: str | None = None,
) -> tuple[AutoStartTarget, ...]:
    """功能：
        从配置中筛选目标主机下的启用入口，按 (优先级, 机器名, UUID) 排序后返回。

    参数：
        config (AutoStartConfig)：当前模块对应的强类型配置对象。
        host (str)：规范化物理主机名，例如 `gpu-203`。
        machine_name (str | None)：AutoDL 平台入口名，例如 `autodl-203-2`；可为 `None` 表示自动选择。

    返回：
        tuple[AutoStartTarget, ...]：函数计算或构造出的结果。

    补充说明：
        如果传入了 machine_name，只返回该特定入口。
        如果没有匹配项，抛 ValueError。
    """
    candidates = [item for item in config.targets if item.enabled and item.host == host]
    if machine_name is not None:
        candidates = [item for item in candidates if item.machine_name == machine_name]
    if not candidates:
        suffix = f" / {machine_name}" if machine_name else ""
        raise ValueError(f"config.yaml 中没有启用的目标：{host}{suffix}")
    return tuple(
        sorted(
            candidates,
            key=lambda item: (item.priority, item.machine_name, item.instance_uuid),
        )
    )


def select_cli_target(
    config: AutoStartConfig,
    host: str,
    machine_name: str | None = None,
) -> AutoStartTarget:
    """功能：
        select_cli_targets 的便捷封装，只返回第一个结果。

    参数：
        config (AutoStartConfig)：当前模块对应的强类型配置对象。
        host (str)：规范化物理主机名，例如 `gpu-203`。
        machine_name (str | None)：AutoDL 平台入口名，例如 `autodl-203-2`；可为 `None` 表示自动选择。

    返回：
        AutoStartTarget：函数计算或构造出的结果。
    """
    return select_cli_targets(config, host, machine_name)[0]
