# GPU 占用日志模块 — SQLite 数据库记录各入口的 GPU 占用情况。

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from .models import OccupancyRecord


ENTRY_FIELDS = (  # 原始入口快照的字段顺序，保留平台逐入口展示的信息。
    "observed_at",
    "host",
    "machine_name",
    "gpu_index",
    "gpu_uuid",
    "gpu_name",
    "occupied",
    "instance_id",
    "user",
    "started_at",
)

EVENT_FIELDS = (  # 事件写库参数必须与 INSERT 的列顺序对应。
    "event_time",
    "event",
    "host",
    "machine_name",
    "gpu_index",
    "gpu_name",
    "user",
    "instance_id",
    "started_at",
    "ended_at",
    "duration_seconds",
)


def _parse_started_at(text: str) -> datetime | None:  # 解析 AutoDL 页面上的"启动时间"字符串。
    value = text.strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _duration_seconds(started_at: str, fallback: str, ended_at: datetime) -> int:  # 计算从 started_at 到 ended_at 的持续秒数。
    start = _parse_started_at(started_at)
    if start is None:
        try:
            start = datetime.fromisoformat(fallback)  # 页面启动时间无法解析时，退回本地第一次观察到的时间。
        except (TypeError, ValueError):
            return 0
    return max(0, int((ended_at - start).total_seconds()))  # 时钟差或异常时间不生成负使用时长；结果是观察估计而非计费账单。


def merge_duplicate_instances(records: list[OccupancyRecord]) -> list[OccupancyRecord]:  # 按 (host, instance_id) 去重，保留同一实例的最新记录。

    grouped: dict[tuple[str, str], list[OccupancyRecord]] = defaultdict(list)
    for item in records:
        if not item.occupied or not item.instance_id:
            continue
        grouped[(item.host, item.instance_id)].append(item)  # 同一实例可能由多个入口重复展示，先按实例身份去重。

    merged: list[OccupancyRecord] = []
    for (_host, _instance_id), items in sorted(grouped.items()):
        chosen = max(items, key=lambda item: item.observed_at)  # 多条重复实例记录中选最新一条作为字段来源。
        source_entries = "|".join(sorted({item.machine_name for item in items}))  # 仍保留实例出现过的全部入口，避免去重后失去来源信息。
        merged.append(
            OccupancyRecord(
                observed_at=max(item.observed_at for item in items),
                host=chosen.host,
                machine_name=source_entries,
                gpu_index=chosen.gpu_index,
                gpu_uuid=chosen.gpu_uuid,
                gpu_name=chosen.gpu_name,
                occupied=True,
                instance_id=chosen.instance_id,
                user=chosen.user,
                started_at_text=chosen.started_at_text,
            )
        )
    return merged


def aggregate_gpu_occupants(records: list[OccupancyRecord]) -> list[dict[str, Any]]:  # 统计每张物理 GPU INDEX 上的并发占用情况。

    instances = merge_duplicate_instances(records)  # 并发统计先去除跨入口重复实例，不能把重复展示算成多个人。
    all_gpu_rows: dict[tuple[str, int], list[OccupancyRecord]] = defaultdict(list)
    metadata: dict[tuple[str, int], OccupancyRecord] = {}

    for item in records:
        key = (item.host, item.gpu_index)
        metadata[key] = max(
            (metadata.get(key), item),
            key=lambda value: value.observed_at if value is not None else datetime.min,
        )
    for item in instances:
        all_gpu_rows[(item.host, item.gpu_index)].append(item)

    result: list[dict[str, Any]] = []
    for key in sorted(metadata):  # 也遍历无人占用的卡，使空闲 GPU 仍有快照记录。
        host, gpu_index = key
        meta = metadata[key]
        occupants = sorted(
            all_gpu_rows.get(key, []),
            key=lambda item: (item.user, item.instance_id, item.machine_name),
        )
        result.append(
            {
                "observed_at": max( [meta.observed_at, *(item.observed_at for item in occupants)] ).isoformat(timespec="seconds"),
                "host": host,
                "gpu_index": gpu_index,
                "gpu_uuid": meta.gpu_uuid,
                "gpu_name": meta.gpu_name,
                "occupant_count": len(occupants),  # 这里统计去重后的实例数，未必等于唯一用户名数量。
                "active_users": json.dumps(
                    sorted({item.user for item in occupants if item.user}),
                    ensure_ascii=False,
                ),
                "active_instance_ids": json.dumps(
                    sorted({item.instance_id for item in occupants if item.instance_id}),
                    ensure_ascii=False,
                ),
                "active_entries": json.dumps(
                    sorted(
                        {
                            entry
                            for item in occupants
                            for entry in item.machine_name.split("|")
                            if entry
                        }
                    ),
                    ensure_ascii=False,
                ),
            }
        )
    return result


class UsageSqliteLogger:  # SQLite 占用日志记录器。

    def __init__(self, database_path: Path) -> None:  # 初始化 SQLite 占用日志器，创建父目录并建立所需数据表。
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:  # 创建并配置一个 SQLite 连接。
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row  # 查询结果既能按列名访问，也便于转换为导出所需的字典。
        connection.execute("PRAGMA journal_mode=WAL")  # 使用 WAL 日志，让写入监控与只读报表更容易并存。
        connection.execute("PRAGMA synchronous=NORMAL")  # 采用 SQLite 的 NORMAL 同步策略，兼顾写入开销；不是每次都完整同步数据库文件。
        connection.execute("PRAGMA busy_timeout=30000")  # 发生锁竞争时最多等 30 秒，超时仍会报错而不是无限阻塞。
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:  # 上下文管理器：确保数据库连接在使用后关闭。

        connection = self._connect()
        try:
            with connection:  # 正常离开时提交事务，异常离开时回滚；外层 finally 再负责关闭连接。
                yield connection
        finally:
            connection.close()

    def _init_schema(self) -> None:  # 创建占用快照、实例状态、GPU 并发和上下机事件等 SQLite 表与索引。
        with self._connection() as conn:
            conn.executescript(  # 五类表分别保存入口快照、去重实例、物理卡汇总、事件和最近观察状态。
                """
                CREATE TABLE IF NOT EXISTS entry_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    capture_id TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    host TEXT NOT NULL,
                    machine_name TEXT NOT NULL,
                    gpu_index INTEGER NOT NULL,
                    gpu_uuid TEXT NOT NULL,
                    gpu_name TEXT NOT NULL,
                    occupied INTEGER NOT NULL,
                    instance_id TEXT NOT NULL,
                    user TEXT NOT NULL,
                    started_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_entry_snapshots_capture
                    ON entry_snapshots(capture_id);
                CREATE INDEX IF NOT EXISTS idx_entry_snapshots_user
                    ON entry_snapshots(user, observed_at);

                CREATE TABLE IF NOT EXISTS instance_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    capture_id TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    host TEXT NOT NULL,
                    machine_name TEXT NOT NULL,
                    gpu_index INTEGER NOT NULL,
                    gpu_uuid TEXT NOT NULL,
                    gpu_name TEXT NOT NULL,
                    instance_id TEXT NOT NULL,
                    user TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    UNIQUE(capture_id, host, instance_id)
                );

                CREATE INDEX IF NOT EXISTS idx_instance_snapshots_instance
                    ON instance_snapshots(host, instance_id, observed_at);
                CREATE INDEX IF NOT EXISTS idx_instance_snapshots_gpu
                    ON instance_snapshots(host, gpu_index, observed_at);

                CREATE TABLE IF NOT EXISTS gpu_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    capture_id TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    host TEXT NOT NULL,
                    gpu_index INTEGER NOT NULL,
                    gpu_uuid TEXT NOT NULL,
                    gpu_name TEXT NOT NULL,
                    occupant_count INTEGER NOT NULL,
                    active_users TEXT NOT NULL,
                    active_instance_ids TEXT NOT NULL,
                    active_entries TEXT NOT NULL,
                    UNIQUE(capture_id, host, gpu_index)
                );

                CREATE INDEX IF NOT EXISTS idx_gpu_snapshots_gpu
                    ON gpu_snapshots(host, gpu_index, observed_at);

                CREATE TABLE IF NOT EXISTS occupancy_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_time TEXT NOT NULL,
                    event TEXT NOT NULL,
                    host TEXT NOT NULL,
                    machine_name TEXT NOT NULL,
                    gpu_index INTEGER NOT NULL,
                    gpu_name TEXT NOT NULL,
                    user TEXT NOT NULL,
                    instance_id TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT NOT NULL,
                    duration_seconds INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS current_instances (
                    state_key TEXT PRIMARY KEY,
                    host TEXT NOT NULL,
                    machine_name TEXT NOT NULL,
                    gpu_index INTEGER NOT NULL,
                    gpu_name TEXT NOT NULL,
                    user TEXT NOT NULL,
                    instance_id TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                );
                """
            )

    @staticmethod
    def _capture_id(records: list[OccupancyRecord]) -> tuple[str, datetime]:  # 根据一批占用记录生成本轮采集批次 ID 和统一采集时间。
        observed_at = max(item.observed_at for item in records)
        return observed_at.isoformat(timespec="microseconds"), observed_at  # Microseconds make consecutive captures unique without external UUIDs.

    def record(
        self,
        records: list[OccupancyRecord],
        *,
        complete_snapshot: bool = True,
        snapshot_hosts: set[str] | None = None,
    ) -> list[dict[str, Any]]:  # 将一轮所有入口的占用事实写入 SQLite，并根据实例变化生成上下机事件。
        if not records:
            return []  # 没有原始行时无法生成快照时间。主流程正常情况下每个 GPU 都会有一行， 因此这里保持无操作；“完整/不完整”判定由调用方负责。

        capture_id, capture_at = self._capture_id(records)  # 同批次多入口记录共用批次 ID，后续可以还原一次完整观察。
        instance_records = merge_duplicate_instances(records)  # 原始视图与去重实例视图分开保存，服务不同的统计口径。
        gpu_rows = aggregate_gpu_occupants(records)  # 把实例视图再次按物理卡聚合，用于共享 GPU 的占用统计。
        events: list[dict[str, Any]] = []

        with self._connection() as conn:
            conn.executemany(
                """
                INSERT INTO entry_snapshots (
                    capture_id, observed_at, host, machine_name, gpu_index,
                    gpu_uuid, gpu_name, occupied, instance_id, user, started_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        capture_id,
                        item.observed_at.isoformat(timespec="seconds"),
                        item.host,
                        item.machine_name,
                        item.gpu_index,
                        item.gpu_uuid,
                        item.gpu_name,
                        int(item.occupied),
                        item.instance_id,
                        item.user,
                        item.started_at_text,
                    )
                    for item in records
                ],
            )

            conn.executemany(
                """
                INSERT OR IGNORE INTO instance_snapshots (
                    capture_id, observed_at, host, machine_name, gpu_index,
                    gpu_uuid, gpu_name, instance_id, user, started_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        capture_id,
                        item.observed_at.isoformat(timespec="seconds"),
                        item.host,
                        item.machine_name,
                        item.gpu_index,
                        item.gpu_uuid,
                        item.gpu_name,
                        item.instance_id,
                        item.user,
                        item.started_at_text,
                    )
                    for item in instance_records
                ],
            )

            conn.executemany(
                """
                INSERT OR IGNORE INTO gpu_snapshots (
                    capture_id, observed_at, host, gpu_index, gpu_uuid, gpu_name,
                    occupant_count, active_users, active_instance_ids, active_entries
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        capture_id,
                        row["observed_at"],
                        row["host"],
                        row["gpu_index"],
                        row["gpu_uuid"],
                        row["gpu_name"],
                        row["occupant_count"],
                        row["active_users"],
                        row["active_instance_ids"],
                        row["active_entries"],
                    )
                    for row in gpu_rows
                ],
            )

            if not complete_snapshot:  # v0.5.0：部分入口采集失败时，只记录“看见了什么”，绝不据此推断 “没看见的实例已经结束”。旧版会在部分失败时制造伪 END_SEEN， 并污染 current_instances。
                return []

            scoped_hosts = set(snapshot_hosts or {item.host for item in records})  # 只更新此次实际观察的主机，不能把其他服务器上的实例判为结束。
            if not scoped_hosts:
                return []
            placeholders = ",".join("?" for _ in scoped_hosts)  # 主机值通过 SQL 参数传入，不直接拼接到查询文本中。
            previous_rows = conn.execute(
                f"SELECT * FROM current_instances WHERE host IN ({placeholders})",
                tuple(sorted(scoped_hosts)),
            ).fetchall()
            previous = {str(row["state_key"]): dict(row) for row in previous_rows}  # 这份历史状态仅用于推导统计事件，不是本轮实时占用事实。
            current: dict[str, OccupancyRecord] = {
                f"{item.host}|{item.instance_id}": item
                for item in instance_records
                if item.host in scoped_hosts
            }
            now_text = capture_at.isoformat(timespec="seconds")

            for key, item in current.items():
                prior = previous.get(key)
                signature_changed = bool(  # 同一实例 ID 的用户、GPU 或来源发生变化时，结束旧记录并建立新记录。
                    prior
                    and (
                        prior["user"] != item.user
                        or int(prior["gpu_index"]) != item.gpu_index
                        or prior["machine_name"] != item.machine_name
                    )
                )

                if prior is None or signature_changed:  # 首次观察或身份变化才产生 ACTIVE_SEEN，持续存在只更新最近观察时间。
                    if signature_changed and prior is not None:
                        end_event = {
                            "event_time": now_text,
                            "event": "END_SEEN",
                            "host": prior["host"],
                            "machine_name": prior["machine_name"],
                            "gpu_index": int(prior["gpu_index"]),
                            "gpu_name": prior["gpu_name"],
                            "user": prior["user"],
                            "instance_id": prior["instance_id"],
                            "started_at": prior["started_at"],
                            "ended_at": now_text,
                            "duration_seconds": _duration_seconds(
                                prior["started_at"], prior["first_seen_at"], capture_at
                            ),
                        }
                        events.append(end_event)
                    events.append(
                        {
                            "event_time": now_text,
                            "event": "ACTIVE_SEEN",
                            "host": item.host,
                            "machine_name": item.machine_name,
                            "gpu_index": item.gpu_index,
                            "gpu_name": item.gpu_name,
                            "user": item.user,
                            "instance_id": item.instance_id,
                            "started_at": item.started_at_text,
                            "ended_at": "",
                            "duration_seconds": 0,
                        }
                    )

                first_seen_at = (
                    prior["first_seen_at"]
                    if prior is not None and not signature_changed
                    else now_text
                )
                conn.execute(
                    """
                    INSERT INTO current_instances (
                        state_key, host, machine_name, gpu_index, gpu_name, user,
                        instance_id, started_at, first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(state_key) DO UPDATE SET
                        host=excluded.host,
                        machine_name=excluded.machine_name,
                        gpu_index=excluded.gpu_index,
                        gpu_name=excluded.gpu_name,
                        user=excluded.user,
                        instance_id=excluded.instance_id,
                        started_at=excluded.started_at,
                        first_seen_at=excluded.first_seen_at,
                        last_seen_at=excluded.last_seen_at
                    """,
                    (
                        key,
                        item.host,
                        item.machine_name,
                        item.gpu_index,
                        item.gpu_name,
                        item.user,
                        item.instance_id,
                        item.started_at_text,
                        first_seen_at,
                        now_text,
                    ),
                )

            ended_keys = sorted(set(previous) - set(current))  # 只有完整快照才执行集合差：上次存在、本次未见的实例记录为 END_SEEN。
            for key in ended_keys:
                prior = previous[key]
                events.append(
                    {
                        "event_time": now_text,
                        "event": "END_SEEN",
                        "host": prior["host"],
                        "machine_name": prior["machine_name"],
                        "gpu_index": int(prior["gpu_index"]),
                        "gpu_name": prior["gpu_name"],
                        "user": prior["user"],
                        "instance_id": prior["instance_id"],
                        "started_at": prior["started_at"],
                        "ended_at": now_text,
                        "duration_seconds": _duration_seconds(
                            prior["started_at"], prior["first_seen_at"], capture_at
                        ),
                    }
                )
                conn.execute("DELETE FROM current_instances WHERE state_key = ?", (key,))  # 删除统计状态表中的旧行，不是在 AutoDL 上删除或关闭实例。

            if events:
                conn.executemany(
                    """
                    INSERT INTO occupancy_events (
                        event_time, event, host, machine_name, gpu_index, gpu_name,
                        user, instance_id, started_at, ended_at, duration_seconds
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        tuple(event[field] for field in EVENT_FIELDS)
                        for event in events
                    ],
                )

        return events  # 快照与事件已在事务中保存，返回值只供主循环汇总显示。

    def current_instances(self) -> list[dict[str, Any]]:  # 读取数据库中当前仍处于占用状态的实例快照。
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM current_instances ORDER BY host, gpu_index, user, instance_id"
            ).fetchall()
            return [dict(row) for row in rows]
