from __future__ import annotations  # CLI 参数解析与主机名/入口名归一化。

import re

from .config import AutoStartConfig, AutoStartTarget


_HOST_RE = re.compile(r"^(?:gpu-|autodl-)?(\d+)(?:-\d+)?$", re.IGNORECASE)  # 匹配 203 / gpu-203 / autodl-203-1 → 提取 203
_ENTRY_RE = re.compile(r"^(?:autodl-)?(\d+)-(\d+)$", re.IGNORECASE)  # 匹配 203-2 / autodl-203-2 → 提取 (203, 2)


def normalize_host(value: str) -> str:  # 将用户输入归一化为规范主机名。
    text = value.strip()
    match = _HOST_RE.match(text)
    if not match:
        raise ValueError(f"无法识别主机参数：{value}")
    return f"gpu-{match.group(1)}"


def normalize_entry(value: str | None, host: str) -> str | None:  # 将用户输入归一化为规范入口名。
    if value is None:
        return None
    text = value.strip()
    if text.lower() == "auto":  # None 表示自动选择入口，不是一个实际服务器名称。
        return None
    if text.isdigit():
        host_number = host.removeprefix("gpu-")  # 纯数字 → 自动补全为 "主机号-入口号"
        text = f"{host_number}-{text}"
    match = _ENTRY_RE.match(text)
    if not match:
        raise ValueError(f"无法识别入口参数：{value}；示例：auto、2、203-2")
    entry_host = f"gpu-{match.group(1)}"
    if entry_host != host:  # 入口必须属于所选物理主机，拒绝跨主机组合。
        raise ValueError(f"入口 {value} 不属于目标主机 {host}")
    return f"autodl-{match.group(1)}-{match.group(2)}"


def select_cli_targets(
    config: AutoStartConfig,
    host: str,
    machine_name: str | None = None,
) -> tuple[AutoStartTarget, ...]:  # 从配置中筛选目标主机下的启用入口，按 (优先级, 机器名, UUID) 排序后返回。
    candidates = [item for item in config.targets if item.enabled and item.host == host]  # 仅使用配置中已启用的固定实例，尚未判断实时空位。
    if machine_name is not None:
        candidates = [item for item in candidates if item.machine_name == machine_name]
    if not candidates:
        suffix = f" / {machine_name}" if machine_name else ""
        raise ValueError(f"config.yaml 中没有启用的目标：{host}{suffix}")
    return tuple(
        sorted( candidates, key=lambda item: (item.priority, item.machine_name, item.instance_uuid), )
    )
