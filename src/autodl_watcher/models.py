"""
核心数据模型 — 定义了整个系统流转的所有数据结构。

数据流（按生成顺序）：
    PlatformHost       ← 平台采集器（Playwright 从 AutoDL 控制台拉取）
    GpuSample          ← Telemetry API 采集器（HTTP GET 物理 GPU 快照）
         ↓
    ConfirmedGpu       ← evaluator 确认"连续满足显存条件"后的结论
    AvailabilityAlert  ← evaluator 综合平台+物理条件后生成的告警/开机事件
         ↓
    OccupancyRecord    ← 占用快照采集器（点击"查看占用"弹窗解析得到）
         ↓
    StartAttemptResult ← autostart 返回的开机尝试结果（定义在 autostart.py 中）
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class PlatformHost:
    """AutoDL 平台视角的一台物理主机。

    free_count / total_count 表示该主机在当前账号下"可分配的 GPU ID"数，
    不是物理 GPU 数量，也不是显存容量。
    同一物理主机可能有多个入口（如 autodl-203-1、autodl-203-2），
    此时 free_count 按配置的 aggregation 策略（max/min/sum）聚合。
    每个入口的原始 (name, idle, total) 保留在 source_slots，
    供 autostart 的入口排序逻辑使用。
    """
    host: str
    free_count: int    # 聚合后可分配的 GPU ID 数
    total_count: int   # 聚合后总 GPU ID 数
    source_names: tuple[str, ...] = ()                          # 所有入口名
    source_slots: tuple[tuple[str, int, int], ...] = ()         # 各入口 (name, idle, total)

    def __post_init__(self) -> None:
        """功能：
            校验平台主机统计字段，并将入口列表规范化为不可变元组。

        参数：
            无。

        返回：
            None：无返回值。
        """
        if self.free_count < 0 or self.total_count <= 0:
            raise ValueError("GPU count must be non-negative and total_count > 0")
        if self.free_count > self.total_count:
            raise ValueError("free_count cannot exceed total_count")


@dataclass(frozen=True, slots=True)
class GpuSample:
    """Telemetry API 回报的一张物理 GPU 的实时快照（单点数据）。

    这是最原始的物理层数据点，经过 evaluator 的连续采样判定后，
    可以升格为 ConfirmedGpu。
    """
    host: str
    gpu_index: int
    gpu_name: str
    util_pct: float           # GPU 利用率（%）
    memory_used_mb: int       # 已用显存（MB）
    memory_total_mb: int      # 总显存（MB）
    observed_at: datetime     # 采样时间

    @property
    def memory_used_ratio(self) -> float:
        """功能：
            已用显存占比。

        参数：
            无。

        返回：
            float：已用显存占总显存的比例。
        """
        if self.memory_total_mb <= 0:
            return 1.0
        return self.memory_used_mb / self.memory_total_mb

    @property
    def memory_free_mb(self) -> int:
        """功能：
            当前可用显存（MB）。

        参数：
            无。

        返回：
            int：剩余可用显存 MB。
        """
        return max(0, self.memory_total_mb - self.memory_used_mb)

    @property
    def memory_free_ratio(self) -> float:
        """功能：
            当前可用显存占比。

        参数：
            无。

        返回：
            float：剩余显存占总显存的比例。
        """
        if self.memory_total_mb <= 0:
            return 0.0
        return self.memory_free_mb / self.memory_total_mb


@dataclass(frozen=True, slots=True)
class ConfirmedGpu:
    """经 evaluator 确认已连续满足容量条件的 GPU。

    与 GpuSample 的本质区别：
        GpuSample 是单点快照，"现在这一刻"的数据；
        ConfirmedGpu 是 evaluator 经过连续采样后判断"已持续达标"的结论。
    """
    host: str
    gpu_index: int
    gpu_name: str
    util_pct: float
    memory_used_mb: int
    memory_total_mb: int
    idle_seconds: int         # 已连续满足显存条件的秒数
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class AvailabilityAlert:
    """触发开机的完整事件 — 两层门控同时满足时的产物。

    包含：
        - 平台层信息（free_count / total_count）
        - 物理层信息（physical_total_count / confirmed_gpus）
        - 各入口原始空位（platform_slots），供 autostart 选择具体入口
    """
    host: str
    platform_free_count: int                         # 平台当前可分配 GPU ID 数
    platform_total_count: int                        # 平台总 GPU ID 数
    physical_total_count: int                        # 物理 GPU INDEX 总数
    actionable_count: int                            # 本次事件涉及的达标 GPU 数
    confirmed_gpus: tuple[ConfirmedGpu, ...]         # 已确认达标的 GPU 列表
    platform_sources: tuple[str, ...] = ()           # 可见入口名列表
    platform_slots: tuple[tuple[str, int, int], ...] = ()   # 各入口原始空位


@dataclass(frozen=True, slots=True)
class OccupancyRecord:
    """某个 AutoDL 入口内一张 GPU 的占用详情。

    来源：点击页面上的"查看占用"按钮，从弹窗表格中解析得到。
    用途：写入 SQLite 数据库，用于后续的 usage_report 统计和审计。

    与 GpuSample 的区别：
        GpuSample 是物理显存指标（由 Telemetry API 回报）；
        OccupancyRecord 是平台视角的"谁在用哪张卡"（由页面 DOM 解析）。
    """
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
