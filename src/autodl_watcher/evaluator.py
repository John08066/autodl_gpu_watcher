"""
GPU 容量评估模块 — 判断物理 GPU 是否已连续满足显存条件。

核心逻辑：
    1. 每张 GPU 维护一个 idle_samples 双端队列，记录连续满足条件的采样
    2. 显存条件：可用显存 >= max(配置的 MB 下限, 总显存 x 比例下限)
    3. GPU Util 默认仅记录，不参与门控（可配置为硬条件）
    4. 当某 GPU 连续达标且平台有空位 -> 生成 AvailabilityAlert
    5. 同一 GPU 只 alert 一次，直到连续 busy reset_busy_samples 次后重置

状态持久化：
    export_state() / import_state() 用于进程重启后恢复连续采样上下文。
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .config import IdleThresholds, MonitorConfig
from .models import AvailabilityAlert, ConfirmedGpu, GpuSample, PlatformHost


@dataclass(slots=True)
class _GpuRuntime:
    """每张 GPU 的运行时状态。"""
    idle_samples: deque[GpuSample]  # 连续满足条件的采样队列
    consecutive_busy: int = 0       # 连续不满足条件的次数
    alerted: bool = False           # 是否已告警（去重）


def required_free_memory_mb(sample: GpuSample, thresholds: IdleThresholds) -> int:
    """功能：
        计算该 GPU 需要的最低可用显存。

    参数：
        sample (GpuSample)：单张物理 GPU 的实时采样对象。
        thresholds (IdleThresholds)：显存容量与可选 GPU 利用率阈值配置。

    返回：
        int：该 GPU 必须保留的最低可用显存 MB。

    补充说明：
        公式：max(配置的固定 MB 数, 总显存 x 配置的比例)
        例如 24576MB + 25% + 8192MB = max(8192, 6144) = 8192MB。
    """
    ratio_requirement = int(sample.memory_total_mb * thresholds.memory_free_min_ratio)
    return max(thresholds.memory_free_min_mb, ratio_requirement)


def sample_meets_capacity(sample: GpuSample, thresholds: IdleThresholds) -> bool:
    """功能：
        判断一张 GPU 当前快照是否满足显存容量条件。

    参数：
        sample (GpuSample)：单张物理 GPU 的实时采样对象。
        thresholds (IdleThresholds)：显存容量与可选 GPU 利用率阈值配置。

    返回：
        bool：样本是否满足显存容量和可选利用率条件。

    补充说明：
        设计原则：
            GPU Util 默认不是硬门控。一张卡可能计算繁忙（高 util）
            但仍有足够的剩余显存来跑一个小 batch 的实验。
            只有显存不足才是硬性拦截条件。
    """

    memory_ok = sample.memory_free_mb >= required_free_memory_mb(sample, thresholds)
    if not memory_ok:
        return False
    if thresholds.gpu_util_check_enabled:
        return sample.util_pct <= thresholds.gpu_util_max_pct
    return True


class AvailabilityEvaluator:
    """容量评估器 — 结合平台 GPU ID 空位和物理 GPU 显存，判断是否可以开机。

    工作流程：
        1. _record() 将每个 GpuSample 记录到对应 GPU 的运行状态中
        2. _confirmed() 检查该 GPU 是否已连续达标足够长时间
        3. evaluate() 汇总所有主机的平台+物理条件，生成 AvailabilityAlert 列表
        4. 同一 GPU 的 alert 是去重的（alerted 标志），直到 busy 次数超限才重置
    """

    def __init__(self, monitor: MonitorConfig, thresholds: IdleThresholds) -> None:
        """功能：
            初始化当前类实例并保存后续方法需要的配置或依赖。

        参数：
            monitor (MonitorConfig)：轮询周期、确认样本数和采样过期时间等监控配置。
            thresholds (IdleThresholds)：显存容量与可选 GPU 利用率阈值配置。

        返回：
            None：函数通过副作用完成初始化、输出、持久化或资源管理。
        """
        self.monitor = monitor
        self.thresholds = thresholds
        # _runtime[(host, gpu_index)] -> _GpuRuntime
        self._runtime: dict[tuple[str, int], _GpuRuntime] = defaultdict(
            lambda: _GpuRuntime(idle_samples=deque())
        )

    def _record(self, sample: GpuSample) -> None:
        """功能：
            记录一条 GPU 采样到运行时状态中。

        参数：
            sample (GpuSample)：单张物理 GPU 的实时采样对象。

        返回：
            None：无返回值。

        补充说明：
            若满足容量条件：追加到 idle_samples 队列，检查采样间隔是否过大。
            若不满足：清空 idle_samples，递增 consecutive_busy。
        """
        key = (sample.host, sample.gpu_index)
        runtime = self._runtime[key]

        if sample_meets_capacity(sample, self.thresholds):
            # 满足条件 → 追加到空闲队列
            if runtime.idle_samples:
                gap = (sample.observed_at - runtime.idle_samples[-1].observed_at).total_seconds()
                if gap > self.monitor.max_sample_gap_seconds:
                    # 间隔过大 → 重置（说明中间有数据缺失）
                    runtime.idle_samples.clear()
            runtime.idle_samples.append(sample)
            # 限制队列长度，避免内存无限增长
            while len(runtime.idle_samples) > max(self.monitor.min_idle_samples + 2, 8):
                runtime.idle_samples.popleft()
            runtime.consecutive_busy = 0
        else:
            # 不满足条件 → 清空空闲记录
            runtime.idle_samples.clear()
            runtime.consecutive_busy += 1
            if runtime.consecutive_busy >= self.monitor.reset_busy_samples:
                # 连续忙碌足够多次 → 重置告警状态，允许再次触发
                runtime.alerted = False

    def _confirmed(self, sample: GpuSample, now: datetime) -> ConfirmedGpu | None:
        """功能：
            检查一张 GPU 是否已确认连续满足容量条件。

        参数：
            sample (GpuSample)：单张物理 GPU 的实时采样对象。
            now (datetime)：本轮评估使用的当前本地时间。

        返回：
            ConfirmedGpu | None：已确认的 GPU 结果；连续性不足时返回 `None`。

        补充说明：
            判定条件（三者同时满足）：
                1. 采样新鲜度在 stale_after_seconds 以内
                2. idle_samples 队列长度 >= min_idle_samples
                3. 队列首尾时间间隔 >= confirmation_seconds
        """
        key = (sample.host, sample.gpu_index)
        runtime = self._runtime[key]

        age = (now - sample.observed_at).total_seconds()
        if age < -5 or age > self.monitor.stale_after_seconds:
            return None
        if len(runtime.idle_samples) < self.monitor.min_idle_samples:
            return None

        first = runtime.idle_samples[0]
        last = runtime.idle_samples[-1]
        available_seconds = int((last.observed_at - first.observed_at).total_seconds())
        if available_seconds < self.monitor.confirmation_seconds:
            return None

        return ConfirmedGpu(
            host=last.host,
            gpu_index=last.gpu_index,
            gpu_name=last.gpu_name,
            util_pct=last.util_pct,
            memory_used_mb=last.memory_used_mb,
            memory_total_mb=last.memory_total_mb,
            idle_seconds=available_seconds,
            observed_at=last.observed_at,
        )

    def evaluate(
        self,
        platform_hosts: list[PlatformHost],
        gpu_samples: list[GpuSample],
        now: datetime,
    ) -> list[AvailabilityAlert]:
        """功能：
            评估当前所有 GPU 的状态，生成开机事件列表。

        参数：
            platform_hosts (list[PlatformHost])：当前账号可见的 AutoDL 平台主机聚合状态列表。
            gpu_samples (list[GpuSample])：当前轮次参与评估的物理 GPU 采样列表。
            now (datetime)：本轮评估使用的当前本地时间。

        返回：
            list[AvailabilityAlert]：本轮新触发的 AvailabilityAlert 列表。

        补充说明：
            步骤：
                1. 记录所有 GPU 采样到运行时状态
                2. 对每张 GPU 检查是否已确认达标
                3. 过滤：仅保留平台有空位 + 未告警过的 GPU
                4. 组装 AvailabilityAlert 返回
        """
        platform_by_host = {item.host: item for item in platform_hosts}
        latest_by_gpu: dict[tuple[str, int], GpuSample] = {}

        # Step 1: 记录所有采样
        for sample in gpu_samples:
            self._record(sample)
            latest_by_gpu[(sample.host, sample.gpu_index)] = sample

        # Step 2: 统计各主机的物理 GPU INDEX 数
        physical_indices_by_host: dict[str, set[int]] = defaultdict(set)
        for host, gpu_index in latest_by_gpu:
            physical_indices_by_host[host].add(gpu_index)

        # Step 3: 检查各 GPU 是否已确认达标
        confirmed_by_host: dict[str, list[ConfirmedGpu]] = defaultdict(list)
        for key, sample in latest_by_gpu.items():
            confirmed = self._confirmed(sample, now)
            if confirmed is not None:
                confirmed_by_host[key[0]].append(confirmed)

        # Step 4: 组装告警（双层门控 + 去重）
        alerts: list[AvailabilityAlert] = []
        for host, confirmed_gpus in confirmed_by_host.items():
            platform = platform_by_host.get(host)
            if platform is None or platform.free_count <= 0:
                continue  # 门控 1: 平台必须有空位

            # 去重: 只取尚未 alert 的 GPU
            unalerted = [
                gpu
                for gpu in confirmed_gpus
                if not self._runtime[(gpu.host, gpu.gpu_index)].alerted
            ]
            if not unalerted:
                continue

            actionable_count = len(unalerted)
            if actionable_count <= 0:
                continue

            alerts.append(
                AvailabilityAlert(
                    host=host,
                    platform_free_count=platform.free_count,
                    platform_total_count=platform.total_count,
                    physical_total_count=len(physical_indices_by_host.get(host, set())),
                    actionable_count=actionable_count,
                    confirmed_gpus=tuple(sorted(unalerted, key=lambda x: x.gpu_index)),
                    platform_sources=platform.source_names,
                    platform_slots=platform.source_slots,
                )
            )
            # 标记已告警，防止重复触发
            for gpu in unalerted:
                self._runtime[(gpu.host, gpu.gpu_index)].alerted = True

        return alerts

    def rearm_host(self, host: str) -> int:
        """功能：
            清除指定物理主机上所有 GPU 的 ``alerted`` 去重标志，
            使该主机在本人实例被 K、关机或释放后可以再次触发自动开机。

        参数：
            host (str)：需要重新武装的物理主机名，例如 ``gpu-203``。

        返回：
            int：本次由 ``alerted=True`` 重置为 ``False`` 的 GPU 数量。

        补充说明：
            本方法只重置告警去重状态，不清空 ``idle_samples``。因此在平台重新
            出现空位且物理显存仍达标时，下一轮评估即可重新生成开机事件。
        """
        reset_count = 0
        for (runtime_host, _gpu_index), runtime in self._runtime.items():
            if runtime_host != host:
                continue
            if runtime.alerted:
                reset_count += 1
            runtime.alerted = False
            runtime.consecutive_busy = 0
        return reset_count

    def export_state(self) -> dict[str, Any]:
        """功能：
            将运行时状态导出为可序列化的字典（用于 state.json 持久化）。

        参数：
            无。

        返回：
            dict[str, Any]：可 JSON 序列化的评估器内部状态。
        """
        result: dict[str, Any] = {}
        for (host, gpu_index), runtime in self._runtime.items():
            result[f"{host}|{gpu_index}"] = {
                "consecutive_busy": runtime.consecutive_busy,
                "alerted": runtime.alerted,
                "idle_samples": [
                    {
                        "host": sample.host,
                        "gpu_index": sample.gpu_index,
                        "gpu_name": sample.gpu_name,
                        "util_pct": sample.util_pct,
                        "memory_used_mb": sample.memory_used_mb,
                        "memory_total_mb": sample.memory_total_mb,
                        "observed_at": sample.observed_at.isoformat(),
                    }
                    for sample in runtime.idle_samples
                ],
            }
        return result

    def import_state(self, state: dict[str, Any]) -> None:
        """功能：
            从字典恢复运行时状态（进程重启后恢复连续采样上下文）。

        参数：
            state (dict[str, Any])：需要恢复或保存的状态字典。

        返回：
            None：无返回值。
        """
        for key, raw in state.items():
            try:
                host, gpu_index_text = key.rsplit("|", 1)
                runtime = _GpuRuntime(
                    idle_samples=deque(
                        GpuSample(
                            host=item["host"],
                            gpu_index=int(item["gpu_index"]),
                            gpu_name=item["gpu_name"],
                            util_pct=float(item["util_pct"]),
                            memory_used_mb=int(item["memory_used_mb"]),
                            memory_total_mb=int(item["memory_total_mb"]),
                            observed_at=datetime.fromisoformat(item["observed_at"]),
                        )
                        for item in raw.get("idle_samples", [])
                    ),
                    consecutive_busy=int(raw.get("consecutive_busy", 0)),
                    alerted=bool(raw.get("alerted", False)),
                )
                self._runtime[(host, int(gpu_index_text))] = runtime
            except (KeyError, TypeError, ValueError):
                continue  # 单条恢复失败不影响其他 GPU
