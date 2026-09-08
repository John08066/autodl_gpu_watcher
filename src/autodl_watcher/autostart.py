from __future__ import annotations  # 自动开机协调器 — AutoStartCoordinator

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
class StartAttemptResult:  # 一次开机尝试的结果。
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

def _slot_map(
    platform_slots: Iterable[tuple[str, int, int]],
) -> dict[str, tuple[int, int]]:  # 将 source_slots 转换为 {入口名: (空闲数, 总数)} 的字典。
    return {name: (int(idle), int(total)) for name, idle, total in platform_slots}


def rank_targets_for_slots(
    config: AutoStartConfig,
    host: str,
    platform_slots: Iterable[tuple[str, int, int]],
    machine_name: str | None = None,
) -> list[AutoStartTarget]:  # 根据 AutoDL 入口实时空位排序可开机的固定实例。

    candidates = [item for item in config.targets if item.enabled and item.host == host]  # 配置决定允许操作的实例范围，实时平台数据只在此范围内筛选。
    if machine_name is not None:  # 固定入口模式进一步缩小候选集，不能借其他入口的空位开机。
        candidates = [item for item in candidates if item.machine_name == machine_name]

    slots = _slot_map(platform_slots)  # 平台空位按入口统计，同一物理主机的入口不能直接相加。
    if slots:  # 有实时空位信息时，必须剔除不可见或无空位的配置入口。
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

    return sorted( candidates, key=lambda item: (item.priority, item.machine_name, item.instance_uuid), )


def select_target(
    config: AutoStartConfig,
    host: str,
    machine_name: str | None = None,
    platform_slots: Iterable[tuple[str, int, int]] = (),
) -> AutoStartTarget | None:  # 从 rank_targets_for_slots 的排序结果中取第一个，即最优入口。
    candidates = rank_targets_for_slots( config, host, platform_slots, machine_name=machine_name, )
    return candidates[0] if candidates else None  # 未找到可用实例时返回 None，由调用方暂缓操作。


def is_instant_idle(
    sample: GpuSample,
    thresholds: IdleThresholds,
    stale_after_seconds: int,
    now: datetime,
) -> bool:  # 快检：一张 GPU 当前是否"即时空闲"（新鲜度 + 显存条件）。
    age = (now - sample.observed_at).total_seconds()
    if age < -5 or age > stale_after_seconds:
        return False  # 采样过期或来自未来
    return sample_meets_capacity(sample, thresholds)


def _host_lookup(platform_hosts: list[PlatformHost], host: str) -> PlatformHost | None:  # 在平台主机列表中按规范化物理主机名查找对应状态。
    return next((item for item in platform_hosts if item.host == host), None)


class AutoStartCoordinator:  # 自动开机协调器。

    def __init__(
        self,
        config: AutoStartConfig,
        monitor: MonitorConfig,
        thresholds: IdleThresholds,
        platform: PlatformBrowserCollector,
        telemetry: TelemetryApiCollector,
    ) -> None:  # 初始化自动开机协调器，并注入配置、平台采集器与物理 GPU 采集器。
        self.config = config          # 自动开机配置（含 dry_run 模式）
        self.monitor = monitor        # 监控参数（stale_after_seconds 等）
        self.thresholds = thresholds  # 显存阈值
        self.platform = platform      # 平台采集器（用于二次确认）
        self.telemetry = telemetry    # Telemetry 采集器（用于二次确认）

    def attempt(self, alert: AvailabilityAlert) -> StartAttemptResult:  # 执行一次自动开机尝试。
        target = select_target( self.config, alert.host, platform_slots=alert.platform_slots, )
        if target is None:
            return StartAttemptResult(
                status="no_target",
                host=alert.host,
                instance_uuid=None,
                machine_name=None,
                message="已配置入口中没有当前可开机的固定实例。",
            )

        if self.config.dry_run:  # 只读模式在任何 power_on 请求之前返回，仍可展示计划选中的实例。
            return StartAttemptResult(
                status="dry_run",
                host=alert.host,
                instance_uuid=target.instance_uuid,
                machine_name=target.machine_name,
                message="已按实时入口空位自动选中实例；当前是 DRY-RUN。",
                platform_free_before=alert.platform_free_count,
                platform_total_before=alert.platform_total_count,
            )

        platform_free_before = alert.platform_free_count  # 先使用事件内快照；开启二次确认时会被更近的读数替换。
        platform_total_before = alert.platform_total_count
        if self.config.verify_before_start:  # 显存和平台空位可能在通知后变化，因此发送请求前再次确认。
            if self.config.recheck_delay_seconds > 0:
                time.sleep(self.config.recheck_delay_seconds)

            platform_hosts = self.platform.collect()  # 重新采集平台数据
            host_state = _host_lookup(platform_hosts, alert.host)
            if host_state is None:
                return StartAttemptResult(  # 账号可能已登出或主机被移除
                    status="recheck_failed",
                    host=alert.host,
                    instance_uuid=target.instance_uuid,
                    machine_name=target.machine_name,
                    message="二次确认失败：当前账号已经看不到该物理主机。",
                )
            platform_free_before = host_state.free_count
            platform_total_before = host_state.total_count
            if host_state.free_count <= 0:
                return StartAttemptResult(  # 平台空位已消失 → 说明有其他用户抢占了
                    status="recheck_failed",
                    host=alert.host,
                    instance_uuid=target.instance_uuid,
                    machine_name=target.machine_name,
                    message="二次确认失败：所有已配置入口都没有平台空位。",
                    platform_free_before=host_state.free_count,
                    platform_total_before=host_state.total_count,
                )

            target = select_target(  # 用最新 source_slots 重新选择入口（而非沿用 evaluator 的旧数据）
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

            all_samples = self.telemetry.collect()  # 重新采集物理显存数据，检查是否仍有 GPU 满足条件
            candidate_samples, _, _ = filter_samples_to_platform_candidates( all_samples, platform_hosts, )  # 二次确认仍使用平台可见性和空位过滤，不能仅凭显存余量。
            now = datetime.now()
            still_ready = [
                sample
                for sample in candidate_samples
                if sample.host == alert.host
                and is_instant_idle( sample, self.thresholds, self.monitor.stale_after_seconds, now, )
            ]
            if not still_ready:
                return StartAttemptResult(  # 显存条件不再满足（可能被其他任务占用了）
                    status="recheck_failed",
                    host=alert.host,
                    instance_uuid=target.instance_uuid,
                    machine_name=target.machine_name,
                    message="二次确认失败：没有 GPU INDEX 满足最低可用显存。",
                    platform_free_before=host_state.free_count,
                    platform_total_before=host_state.total_count,
                )

        response = self.platform.post_api_json(  # 这里才是真正改变平台状态的开机请求，前面均为选择或只读检查。
            "/api/v2/instance/power_on",
            { "instance_uuid": target.instance_uuid, "start_mode": target.start_mode, },
        )
        code = str(response.get("code", ""))  # HTTP 成功不等于业务成功，还要检查 AutoDL 的业务 code。
        msg = str(response.get("msg", ""))
        if code != "Success":
            return StartAttemptResult(  # AutoDL 拒绝了请求（可能是并发冲突或权限不足）
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

        platform_free_after: int | None = None
        platform_total_after: int | None = None
        if self.config.post_start_check_seconds >= 0:  # 非负数启用请求后的平台观察；观察失败不会推翻已受理的响应。
            if self.config.post_start_check_seconds > 0:
                time.sleep(self.config.post_start_check_seconds)
            try:
                refreshed = self.platform.collect()  # 重新采集平台数据，检查平台空位是否确实减少了
                refreshed_host = _host_lookup(refreshed, alert.host)
                if refreshed_host is not None:
                    platform_free_after = refreshed_host.free_count
                    platform_total_after = refreshed_host.total_count
            except Exception:
                platform_free_after = None
                platform_total_after = None

        return StartAttemptResult(
            status="request_accepted",  # 主循环随后等待实时占用确认，并在宽限期内抑制重复开机。
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


def _slot_text(free: int | None, total: int | None) -> str | None:  # 格式化平台空位文本，如 '3/3'。
    if free is None:
        return None
    if total is None:
        return str(free)
    return f"{free}/{total}"


def format_start_result(result: StartAttemptResult) -> str:  # 将开机尝试结果格式化为人类可读的控制台输出文本。
    status_text = {
        "dry_run": "DRY-RUN，未开机",
        "request_accepted": "开机请求成功受理",
        "request_failed": "开机请求失败",
        "recheck_failed": "二次确认未通过",
        "no_target": "没有可用固定实例",
    }.get(result.status, result.status)

    lines = [ "【AutoDL 自动开机结果】", f"结果：{status_text}", f"物理主机：{result.host}", ]
    if result.machine_name:
        lines.append(f"自动选中入口：{result.machine_name}")
    if result.instance_uuid:
        lines.append(f"实例：{result.instance_uuid}")

    before = _slot_text(result.platform_free_before, result.platform_total_before)  # 把可选读数转成文本；缺失值表示未观测到，不应当作零空位。
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
