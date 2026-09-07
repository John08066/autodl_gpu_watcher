"""
自动开机协调器 — AutoStartCoordinator

职责：
    收到 AvailabilityAlert 后，执行以下流程：
    1. 根据各入口实时空位，自动选择最优的固定实例
    2. 若为 dry-run 模式，只判定不开机
    3. 若配置了 verify_before_start，执行二次确认（重新采集平台+物理数据）
    4. 通过 Playwright 捕获的 Authorization 发送 POST power_on 请求
    5. 开机后可选地验证平台空位变化

入口选择策略（rank_targets_for_slots）：
    排序键：(该入口空闲 GPU ID 数降序, 配置优先级升序, 机器名, UUID)
    即：优先选择实时空位最多的入口；空位相同时优先级高的优先。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from .collectors import (
    PlatformBrowserCollector,
    TelemetryApiCollector,
    filter_samples_to_platform_candidates,
)
from .config import AutoStartConfig, AutoStartTarget, IdleThresholds, MonitorConfig
from .evaluator import sample_meets_capacity
from .models import AvailabilityAlert, GpuSample, PlatformHost


@dataclass(frozen=True, slots=True)
class StartAttemptResult:
    """一次开机尝试的结果。"""
    status: str                     # dry_run / request_accepted / request_failed / recheck_failed / no_target
    host: str
    instance_uuid: str | None
    machine_name: str | None
    message: str                    # 人类可读的描述
    api_code: str | None = None     # AutoDL API 返回的 code
    api_msg: str | None = None      # AutoDL API 返回的 msg
    platform_free_before: int | None = None   # 开机前平台空闲 GPU ID 数
    platform_total_before: int | None = None
    platform_free_after: int | None = None    # 开机后平台空闲 GPU ID 数
    platform_total_after: int | None = None

    @property
    def accepted(self) -> bool:
        """功能：
            是否被系统接受（dry-run 或请求成功都算"接受"）。

        参数：
            无。

        返回：
            bool：当前结果是否可视为已接受；DRY-RUN 和真实请求成功均返回 `True`。
        """
        return self.status in {"dry_run", "request_accepted"}


def _slot_map(
    platform_slots: Iterable[tuple[str, int, int]],
) -> dict[str, tuple[int, int]]:
    """功能：
        将 source_slots 转换为 {入口名: (空闲数, 总数)} 的字典。

    参数：
        platform_slots (Iterable[tuple[str, int, int]])：入口空位三元组序列，元素形式为 `(入口名, 空闲 GPU ID 数, 总 GPU ID 数)`。

    返回：
        dict[str, tuple[int, int]]：以入口名为键、`(空闲数, 总数)` 为值的字典。
    """
    return {name: (int(idle), int(total)) for name, idle, total in platform_slots}


def rank_targets_for_slots(
    config: AutoStartConfig,
    host: str,
    platform_slots: Iterable[tuple[str, int, int]],
    machine_name: str | None = None,
) -> list[AutoStartTarget]:
    """功能：
        根据 AutoDL 入口实时空位排序可开机的固定实例。

    参数：
        config (AutoStartConfig)：当前模块对应的强类型配置对象。
        host (str)：规范化物理主机名，例如 `gpu-203`。
        platform_slots (Iterable[tuple[str, int, int]])：入口空位三元组序列，元素形式为 `(入口名, 空闲 GPU ID 数, 总 GPU ID 数)`。
        machine_name (str | None)：可选的强制入口；提供后只在该入口的固定实例中排序。

    返回：
        list[AutoStartTarget]：按实时空位和配置优先级排序后的可用固定实例列表。

    补充说明：
        排序策略：
            1. 该入口当前空闲 GPU ID 数（降序）— 空位越多越优先
            2. 配置的 priority（升序）— 数字越小优先级越高
            3. 机器名 + UUID（字典序，保证稳定排序）
            4. 空闲数为 0 的入口直接排除
    """

    candidates = [item for item in config.targets if item.enabled and item.host == host]
    if machine_name is not None:
        candidates = [item for item in candidates if item.machine_name == machine_name]

    slots = _slot_map(platform_slots)
    if slots:
        candidates = [
            item
            for item in candidates
            if item.machine_name in slots and slots[item.machine_name][0] > 0
        ]
        return sorted(
            candidates,
            key=lambda item: (
                -slots[item.machine_name][0],  # 空闲数降序
                item.priority,                  # 优先级升序
                item.machine_name,              # 稳定排序
                item.instance_uuid,
            ),
        )

    return sorted(
        candidates,
        key=lambda item: (item.priority, item.machine_name, item.instance_uuid),
    )


def select_target(
    config: AutoStartConfig,
    host: str,
    machine_name: str | None = None,
    platform_slots: Iterable[tuple[str, int, int]] = (),
) -> AutoStartTarget | None:
    """功能：
        从 rank_targets_for_slots 的排序结果中取第一个，即最优入口。

    参数：
        config (AutoStartConfig)：当前模块对应的强类型配置对象。
        host (str)：规范化物理主机名，例如 `gpu-203`。
        machine_name (str | None)：可选强制入口；`None` 表示根据实时空位自动选。
        platform_slots (Iterable[tuple[str, int, int]])：入口空位三元组序列，元素形式为 `(入口名, 空闲 GPU ID 数, 总 GPU ID 数)`。

    返回：
        AutoStartTarget | None：最优固定实例；没有入口可用时返回 `None`。
    """
    candidates = rank_targets_for_slots(
        config,
        host,
        platform_slots,
        machine_name=machine_name,
    )
    return candidates[0] if candidates else None


def is_instant_idle(
    sample: GpuSample,
    thresholds: IdleThresholds,
    stale_after_seconds: int,
    now: datetime,
) -> bool:
    """功能：
        快检：一张 GPU 当前是否"即时空闲"（新鲜度 + 显存条件）。

    参数：
        sample (GpuSample)：单张物理 GPU 的实时采样对象。
        thresholds (IdleThresholds)：显存容量与可选 GPU 利用率阈值配置。
        stale_after_seconds (int)：允许采样保持有效的最大秒数。
        now (datetime)：本轮评估使用的当前本地时间。

    返回：
        bool：样本是否新鲜且满足当前容量阈值。

    补充说明：
        用于二次确认阶段，区别于 evaluator 完整状态机的一次性判定。
    """
    age = (now - sample.observed_at).total_seconds()
    if age < -5 or age > stale_after_seconds:
        return False  # 采样过期或来自未来
    return sample_meets_capacity(sample, thresholds)


def _host_lookup(platform_hosts: list[PlatformHost], host: str) -> PlatformHost | None:
    """功能：
        在平台主机列表中按规范化物理主机名查找对应状态。

    参数：
        platform_hosts (list[PlatformHost])：当前账号可见的 AutoDL 平台主机聚合状态列表。
        host (str)：规范化物理主机名，例如 `gpu-203`。

    返回：
        PlatformHost | None：匹配的 PlatformHost；未找到时返回 `None`。
    """
    return next((item for item in platform_hosts if item.host == host), None)


class AutoStartCoordinator:
    """自动开机协调器。

    职责范围：
        1. 按实时空位选择最优入口
        2. 可选二次确认（重新采集平台+物理数据）
        3. 发送 POST power_on 请求
        4. 可选开机后验证

    不依赖 evaluator 的状态机，每次 attempt 独立执行。
    """

    def __init__(
        self,
        config: AutoStartConfig,
        monitor: MonitorConfig,
        thresholds: IdleThresholds,
        platform: PlatformBrowserCollector,
        telemetry: TelemetryApiCollector,
    ) -> None:
        """功能：
            初始化自动开机协调器，并注入配置、平台采集器与物理 GPU 采集器。

        参数：
            config (AutoStartConfig)：当前模块对应的强类型配置对象。
            monitor (MonitorConfig)：轮询周期、确认样本数和采样过期时间等监控配置。
            thresholds (IdleThresholds)：显存容量与可选 GPU 利用率阈值配置。
            platform (PlatformBrowserCollector)：AutoDL 平台浏览器采集器。
            telemetry (TelemetryApiCollector)：物理 GPU Telemetry API 采集器。

        返回：
            None：函数通过副作用完成初始化、输出、持久化或资源管理。
        """
        self.config = config          # 自动开机配置（含 dry_run 模式）
        self.monitor = monitor        # 监控参数（stale_after_seconds 等）
        self.thresholds = thresholds  # 显存阈值
        self.platform = platform      # 平台采集器（用于二次确认）
        self.telemetry = telemetry    # Telemetry 采集器（用于二次确认）

    def attempt(self, alert: AvailabilityAlert) -> StartAttemptResult:
        """功能：
            执行一次自动开机尝试。

        参数：
            alert (AvailabilityAlert)：评估器生成的开机达标事件，包含目标主机、平台空位和达标 GPU。

        返回：
            StartAttemptResult：包含状态、入口、实例 UUID 和 API 返回值的 StartAttemptResult。

        补充说明：
            流程：
                1. select_target 根据入口实时空位选择最优入口
                2. dry_run 模式 → 直接返回结果
                3. verify_before_start → 二次确认（重新采集 + 检查显存）
                4. POST power_on → 发送开机请求
                5. post_start_check → 可选开机后验证
        """
        target = select_target(
            self.config,
            alert.host,
            platform_slots=alert.platform_slots,
        )
        if target is None:
            return StartAttemptResult(
                status="no_target",
                host=alert.host,
                instance_uuid=None,
                machine_name=None,
                message="已配置入口中没有当前可开机的固定实例。",
            )

        if self.config.dry_run:
            return StartAttemptResult(
                status="dry_run",
                host=alert.host,
                instance_uuid=target.instance_uuid,
                machine_name=target.machine_name,
                message="已按实时入口空位自动选中实例；当前是 DRY-RUN。",
                platform_free_before=alert.platform_free_count,
                platform_total_before=alert.platform_total_count,
            )

        # ── 二次确认阶段（可选） ──
        platform_free_before = alert.platform_free_count
        platform_total_before = alert.platform_total_count
        if self.config.verify_before_start:
            if self.config.recheck_delay_seconds > 0:
                time.sleep(self.config.recheck_delay_seconds)

            # 重新采集平台数据
            platform_hosts = self.platform.collect()
            host_state = _host_lookup(platform_hosts, alert.host)
            if host_state is None:
                # 账号可能已登出或主机被移除
                return StartAttemptResult(
                    status="recheck_failed",
                    host=alert.host,
                    instance_uuid=target.instance_uuid,
                    machine_name=target.machine_name,
                    message="二次确认失败：当前账号已经看不到该物理主机。",
                )
            platform_free_before = host_state.free_count
            platform_total_before = host_state.total_count
            if host_state.free_count <= 0:
                # 平台空位已消失 → 说明有其他用户抢占了
                return StartAttemptResult(
                    status="recheck_failed",
                    host=alert.host,
                    instance_uuid=target.instance_uuid,
                    machine_name=target.machine_name,
                    message="二次确认失败：所有已配置入口都没有平台空位。",
                    platform_free_before=host_state.free_count,
                    platform_total_before=host_state.total_count,
                )

            # 用最新 source_slots 重新选择入口（而非沿用 evaluator 的旧数据）
            target = select_target(
                self.config,
                alert.host,
                platform_slots=host_state.source_slots,
            )
            if target is None:
                return StartAttemptResult(
                    status="recheck_failed",
                    host=alert.host,
                    instance_uuid=None,
                    machine_name=None,
                    message="二次确认失败：已配置入口均无平台空位。",
                    platform_free_before=host_state.free_count,
                    platform_total_before=host_state.total_count,
                )

            # 重新采集物理显存数据，检查是否仍有 GPU 满足条件
            all_samples = self.telemetry.collect()
            candidate_samples, _, _ = filter_samples_to_platform_candidates(
                all_samples,
                platform_hosts,
            )
            now = datetime.now()
            still_ready = [
                sample
                for sample in candidate_samples
                if sample.host == alert.host
                and is_instant_idle(
                    sample,
                    self.thresholds,
                    self.monitor.stale_after_seconds,
                    now,
                )
            ]
            if not still_ready:
                # 显存条件不再满足（可能被其他任务占用了）
                return StartAttemptResult(
                    status="recheck_failed",
                    host=alert.host,
                    instance_uuid=target.instance_uuid,
                    machine_name=target.machine_name,
                    message="二次确认失败：没有 GPU INDEX 满足最低可用显存。",
                    platform_free_before=host_state.free_count,
                    platform_total_before=host_state.total_count,
                )

        # ── Step 2: 发送 POST power_on 请求 ──
        response = self.platform.post_api_json(
            "/api/v2/instance/power_on",
            {
                "instance_uuid": target.instance_uuid,
                "start_mode": target.start_mode,
            },
        )
        code = str(response.get("code", ""))
        msg = str(response.get("msg", ""))
        if code != "Success":
            # AutoDL 拒绝了请求（可能是并发冲突或权限不足）
            return StartAttemptResult(
                status="request_failed",
                host=alert.host,
                instance_uuid=target.instance_uuid,
                machine_name=target.machine_name,
                message="AutoDL 拒绝了开机请求。",
                api_code=code or None,
                api_msg=msg or None,
                platform_free_before=platform_free_before,
                platform_total_before=platform_total_before,
            )

        # ── Step 3: 开机后验证（可选） ──
        platform_free_after: int | None = None
        platform_total_after: int | None = None
        if self.config.post_start_check_seconds >= 0:
            if self.config.post_start_check_seconds > 0:
                time.sleep(self.config.post_start_check_seconds)
            try:
                # 重新采集平台数据，检查平台空位是否确实减少了
                refreshed = self.platform.collect()
                refreshed_host = _host_lookup(refreshed, alert.host)
                if refreshed_host is not None:
                    platform_free_after = refreshed_host.free_count
                    platform_total_after = refreshed_host.total_count
            except Exception:
                platform_free_after = None
                platform_total_after = None

        return StartAttemptResult(
            status="request_accepted",
            host=alert.host,
            instance_uuid=target.instance_uuid,
            machine_name=target.machine_name,
            message="power_on 已成功受理；入口由实时空位自动选择。",
            api_code=code,
            api_msg=msg or None,
            platform_free_before=platform_free_before,
            platform_total_before=platform_total_before,
            platform_free_after=platform_free_after,
            platform_total_after=platform_total_after,
        )


def _slot_text(free: int | None, total: int | None) -> str | None:
    """功能：
        格式化平台空位文本，如 '3/3'。

    参数：
        free (int | None)：平台当前可分配的 GPU ID 数；允许为 `None`。
        total (int | None)：平台 GPU ID 总数；允许为 `None`。

    返回：
        str | None：格式化后的 `空闲/总数` 文本；无法表示时返回 `None`。
    """
    if free is None:
        return None
    if total is None:
        return str(free)
    return f"{free}/{total}"


def format_start_result(result: StartAttemptResult) -> str:
    """功能：
        将开机尝试结果格式化为人类可读的控制台输出文本。

    参数：
        result (StartAttemptResult)：一次自动开机尝试的结构化结果。

    返回：
        str：适合终端输出的多行开机结果文本。
    """
    status_text = {
        "dry_run": "DRY-RUN，未开机",
        "request_accepted": "开机请求成功受理",
        "request_failed": "开机请求失败",
        "recheck_failed": "二次确认未通过",
        "no_target": "没有可用固定实例",
    }.get(result.status, result.status)

    lines = [
        "【AutoDL 自动开机结果】",
        f"结果：{status_text}",
        f"物理主机：{result.host}",
    ]
    if result.machine_name:
        lines.append(f"自动选中入口：{result.machine_name}")
    if result.instance_uuid:
        lines.append(f"实例：{result.instance_uuid}")

    before = _slot_text(result.platform_free_before, result.platform_total_before)
    after = _slot_text(result.platform_free_after, result.platform_total_after)
    if before is not None:
        lines.append(f"平台可分配 GPU ID：开机前 {before}")
    if after is not None:
        lines[-1] += f"，开机后 {after}"
    if result.api_code:
        lines.append(f"接口：{result.api_code}")
    if result.api_msg:
        lines.append(f"接口消息：{result.api_msg}")
    lines.append(f"说明：{result.message}")
    return "\n".join(lines)
