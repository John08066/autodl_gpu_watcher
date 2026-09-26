from __future__ import annotations  # 核心数据模型 — 定义了整个系统流转的所有数据结构。

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class PlatformHost:  # AutoDL 平台视角的一台物理主机。
    host: str
    free_count: int    # 聚合后可分配的 GPU ID 数
    total_count: int   # 聚合后总 GPU ID 数
    source_names: tuple[str, ...] = ()                          # 所有入口名
    source_slots: tuple[tuple[str, int, int], ...] = ()         # 各入口 (name, idle, total)

    def __post_init__(self) -> None:  # 构造后检查平台计数范围；入口元组由采集器组装。
        if self.free_count < 0 or self.total_count <= 0:
            raise ValueError("GPU count must be non-negative and total_count > 0")
        if self.free_count > self.total_count:
            raise ValueError("free_count cannot exceed total_count")


@dataclass(frozen=True, slots=True)
class GpuSample:  # Telemetry API 回报的一张物理 GPU 的实时快照（单点数据）。
    host: str
    gpu_index: int
    gpu_name: str
    util_pct: float           # GPU 利用率（%）
    memory_used_mb: int       # 已用显存（MB）
    memory_total_mb: int      # 总显存（MB）
    observed_at: datetime     # 采样时间

    @property
    def memory_used_ratio(self) -> float:  # 已用显存占比。
        if self.memory_total_mb <= 0:
            return 1.0
        return self.memory_used_mb / self.memory_total_mb

    @property
    def memory_free_mb(self) -> int:  # 当前可用显存（MB）。
        return max(0, self.memory_total_mb - self.memory_used_mb)  # 对异常负差值按零处理，不能凭空得到可用容量。

    @property
    def memory_free_ratio(self) -> float:  # 当前可用显存占比。
        if self.memory_total_mb <= 0:
            return 0.0
        return self.memory_free_mb / self.memory_total_mb


@dataclass(frozen=True, slots=True)
class ConfirmedGpu:  # 经 evaluator 确认已连续满足容量条件的 GPU。
    host: str
    gpu_index: int
    gpu_name: str
    util_pct: float
    memory_used_mb: int
    memory_total_mb: int
    idle_seconds: int         # 已连续满足显存条件的秒数
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class AvailabilityAlert:  # 触发开机的完整事件 — 两层门控同时满足时的产物。
    host: str
    platform_free_count: int                         # 平台当前可分配 GPU ID 数
    platform_total_count: int                        # 平台总 GPU ID 数
    physical_total_count: int                        # 物理 GPU INDEX 总数
    actionable_count: int                            # 本次事件涉及的达标 GPU 数
    confirmed_gpus: tuple[ConfirmedGpu, ...]         # 已确认达标的 GPU 列表
    platform_sources: tuple[str, ...] = ()           # 可见入口名列表
    platform_slots: tuple[tuple[str, int, int], ...] = ()   # 各入口原始空位


@dataclass(frozen=True, slots=True)
class OccupancyRecord:  # 某个 AutoDL 入口内一张 GPU 的占用详情。
    observed_at: datetime
    host: str
    machine_name: str         # 所属入口（如 autodl-203-1）
    gpu_index: int
    gpu_uuid: str
    gpu_name: str
    occupied: bool            # 是否被占用（"是" / "否"）
    instance_id: str          # 实例 ID
    user: str                 # 使用者标识
    started_at_text: str      # 任务启动时间的原始字符串
    display_name: str = ""   # 网页展示名字，不作为本人归属或实例身份依据。
