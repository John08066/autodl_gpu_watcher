from __future__ import annotations  # 自动开机协调器 — AutoStartCoordinator

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable

from .collectors import (
    PlatformAuthenticationError,
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
        convert_no_gpu: bool = False,
    ) -> None:  # 初始化自动开机协调器，并注入配置、平台采集器与物理 GPU 采集器。
        self.config = config          # 自动开机配置（含 dry_run 模式）
        self.monitor = monitor        # 监控参数（stale_after_seconds 等）
        self.thresholds = thresholds  # 显存阈值
        self.platform = platform      # 平台采集器（用于二次确认）
        self.telemetry = telemetry    # Telemetry 采集器（用于二次确认）
        self.convert_no_gpu = convert_no_gpu  # 固定入口显式启用无卡转有卡。
        self.pending_switch: AutoStartTarget | None = None  # 关机受理后锁定原实例 UUID。
        self._pending_alert: AvailabilityAlert | None = None
        self._switch_power_on_sent = False  # 响应丢失时不重复发送有卡开机。

    def attempt(self, alert: AvailabilityAlert, stop_requested: Callable[[], bool] = lambda: False,
                target_override: AutoStartTarget | None = None) -> StartAttemptResult:  # 执行一次自动开机尝试。
        target = target_override or select_target( self.config, alert.host, platform_slots=alert.platform_slots, )
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

            if target_override is not None:  # 关机后的有卡开机只允许原入口、原 UUID。
                slots = _slot_map(host_state.source_slots)
                target = target_override if slots.get(target_override.machine_name, (0, 0))[0] > 0 else None
            else:
                target = select_target(  # 普通开机继续按最新入口空位自动选择。
                    self.config, alert.host, platform_slots=host_state.source_slots,
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

        if stop_requested():  # 停止信号到达后不再改变实例状态。
            return StartAttemptResult("cancelled", alert.host, target.instance_uuid, target.machine_name, "监控已请求停止。")

        if self.convert_no_gpu:
            state = self.platform.get_instance_state(target.instance_uuid, target.machine_name)  # UUID、入口和同主机账号实例均由完整列表核对。
            status, mode = state["status"], state["start_mode"]
            if state.get("host_account_gpu_clear") is not True:  # 同账号其他入口仍有卡或列表不完整时禁止关机、开机。
                return StartAttemptResult("instance_state_blocked", alert.host, target.instance_uuid,
                                          target.machine_name, "同主机账号有卡实例尚未排除，暂缓切换。")
            if status == "running" and mode == "non_gpu":
                if target_override is not None:  # 已经发过关机，绝不在后续轮询中重复发送。
                    return StartAttemptResult("instance_state_blocked", alert.host, target.instance_uuid,
                                              target.machine_name, "实例又处于无卡运行状态，取消本次自动切换。")
                if stop_requested():
                    return StartAttemptResult("cancelled", alert.host, target.instance_uuid, target.machine_name, "监控已请求停止。")
                self.pending_switch = target  # 先记目标；请求超时也可能已被服务端受理。
                self._pending_alert = alert
                try:
                    response = self.platform.post_api_json(  # 私有云页面对无卡实例使用 release=now。
                        "/api/v2/instance/power_off", {"instance_uuid": target.instance_uuid, "release": "now"})
                except PlatformAuthenticationError:  # 明确 401/403 时正常走登录恢复，不留下待关机状态。
                    self.pending_switch = None
                    self._pending_alert = None
                    raise
                except Exception as exc:
                    return StartAttemptResult("shutdown_uncertain", alert.host, target.instance_uuid,
                                              target.machine_name, f"关机响应未确认：{exc}；继续只读查询原实例。")
                code, msg = str(response.get("code", "")), str(response.get("msg", ""))
                if code != "Success":
                    self.pending_switch = None
                    self._pending_alert = None
                    return StartAttemptResult("shutdown_failed", alert.host, target.instance_uuid,
                                              target.machine_name, "AutoDL 拒绝了无卡关机请求。", code or None, msg or None)
                return StartAttemptResult("shutdown_requested", alert.host, target.instance_uuid,
                                          target.machine_name, "无卡关机已受理，等待实例变为已关机。", code, msg or None)
            if status != "shutdown":  # 已有卡运行、正在开关机或未知状态都不重复开机。
                return StartAttemptResult("instance_state_blocked", alert.host, target.instance_uuid,
                                          target.machine_name, f"实例当前状态为 {status}/{mode}，暂缓有卡开机。")
        else:
            try:
                state = self.platform.get_instance_state(target.instance_uuid, target.machine_name)  # 普通开机也须核实配置 UUID、入口及本账号同主机占用。
            except PlatformAuthenticationError:
                raise
            except Exception as exc:
                return StartAttemptResult("instance_state_blocked", alert.host, target.instance_uuid,
                                          target.machine_name, f"实例列表无法核对目标：{exc}")
            if (not isinstance(state, dict) or state.get("status") != "shutdown"
                    or state.get("host_account_gpu_clear") is not True):
                return StartAttemptResult("instance_state_blocked", alert.host, target.instance_uuid,
                                          target.machine_name, "目标实例未确认已关机，或同主机账号有卡占用尚未排除，暂缓开机。")

        if stop_requested():
            return StartAttemptResult("cancelled", alert.host, target.instance_uuid, target.machine_name, "监控已请求停止。")
        if target_override is not None:
            self._switch_power_on_sent = True  # 请求可能到达服务端，即使稍后网络报错也不盲目重发。
        try:
            response = self.platform.post_api_json(  # 这里才是真正改变平台状态的开机请求，前面均为选择或只读检查。
                "/api/v2/instance/power_on",
                { "instance_uuid": target.instance_uuid, "start_mode": target.start_mode, },
            )
        except PlatformAuthenticationError:  # 明确未认证时服务端没有受理，可在登录恢复后重试。
            self._switch_power_on_sent = False
            raise
        code = str(response.get("code", ""))  # HTTP 成功不等于业务成功，还要检查 AutoDL 的业务 code。
        msg = str(response.get("msg", ""))
        if code != "Success":
            self._switch_power_on_sent = False  # 明确业务拒绝后允许下轮重新检查。
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

    def continue_switch(self, stop_requested: Callable[[], bool] = lambda: False) -> StartAttemptResult | None:  # 后续轮次推进已受理的关机。
        target, alert = self.pending_switch, self._pending_alert
        if target is None or alert is None or stop_requested():
            return None
        state = self.platform.get_instance_state(target.instance_uuid, target.machine_name)  # 只追踪原 UUID 和入口。
        status, mode = state["status"], state["start_mode"]
        if status == "shutting_down" or (status == "running" and mode == "non_gpu"):
            return StartAttemptResult("shutdown_pending", alert.host, target.instance_uuid,
                                      target.machine_name, "等待无卡实例完全关机。")
        if status in {"starting", "running"} and mode == "gpu":
            self.pending_switch = None  # 同一实例已由平台或人工进入有卡启动，结束本次切换。
            self._pending_alert = None
            self._switch_power_on_sent = False
            return StartAttemptResult("gpu_start_observed", alert.host, target.instance_uuid,
                                      target.machine_name, "同一实例已进入有卡启动，等待占用确认。")
        if status != "shutdown":
            return StartAttemptResult("instance_state_blocked", alert.host, target.instance_uuid,
                                      target.machine_name, f"实例状态为 {status}/{mode}；已发送关机，不重复操作，等待人工核查。")
        if self._switch_power_on_sent:  # 上次响应不明，继续只读观察，不重复 power_on。
            return StartAttemptResult("gpu_start_uncertain", alert.host, target.instance_uuid,
                                      target.machine_name, "有卡开机响应未确认；等待状态变化或人工核查。")
        result = self.attempt(alert, stop_requested, target_override=target)  # 再次核对空位和显存，不允许换到另一实例。
        if result.status == "request_accepted":
            self.pending_switch = None
            self._pending_alert = None
            self._switch_power_on_sent = False
        return result


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
        "shutdown_requested": "无卡关机请求成功受理",
        "shutdown_uncertain": "关机请求结果待确认",
        "shutdown_pending": "等待无卡实例关机",
        "gpu_start_observed": "已观测到同一实例有卡启动",
        "gpu_start_uncertain": "有卡开机请求结果待确认",
        "shutdown_failed": "无卡关机请求失败",
        "instance_state_blocked": "实例状态不允许开机",
        "cancelled": "已取消操作",
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
