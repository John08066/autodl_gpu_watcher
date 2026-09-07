from __future__ import annotations  # 告警格式化 — 将 AvailabilityAlert 转换为人类可读的文本。

from ..models import AvailabilityAlert


def format_alert(alert: AvailabilityAlert) -> str:  # 将 AvailabilityAlert 格式化为结构化的多行文本。
    lines = [
        "【AutoDL 开机达标】",
        "",
        f"物理主机：{alert.host}",
        f"平台可分配 GPU ID：{alert.platform_free_count}/{alert.platform_total_count}",
        f"物理 GPU INDEX 数：{alert.physical_total_count}",
        f"显存达标 GPU INDEX 数：{alert.actionable_count}",
    ]
    if alert.platform_sources:
        lines.append(f"可见平台入口：{', '.join(alert.platform_sources)}")
    lines.append("")

    for gpu in alert.confirmed_gpus:
        free_mb = max(0, gpu.memory_total_mb - gpu.memory_used_mb)
        lines.extend(
            [
                f"GPU INDEX：#{gpu.gpu_index} {gpu.gpu_name}",
                f"可用显存：{free_mb} / {gpu.memory_total_mb} MB",
                f"已用显存：{gpu.memory_used_mb} MB",
                f"GPU Util（仅参考）：{gpu.util_pct:.0f}%",
                f"持续满足容量条件：{gpu.idle_seconds} 秒",
                f"采样时间：{gpu.observed_at:%Y-%m-%d %H:%M:%S}",
                "",
            ]
        )
    return "\n".join(lines).rstrip()
