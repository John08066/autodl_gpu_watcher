from __future__ import annotations  # 占用报表生成器 — 从 SQLite 数据库导出 CSV 报表并统计 GPU 使用时长。

import csv
import json
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from .config import load_config


def _parse_time(value: str) -> datetime:  # 解析 SQLite 或 CSV 中使用的 ISO 时间字符串。
    return datetime.fromisoformat(value)


def _write_csv(path: Path, rows: list[dict[str, object]], fields: list[str]) -> None:  # 按指定字段顺序把字典行写入 UTF-8 BOM CSV，便于 WPS/Excel 直接打开。
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:  # BOM 便于 Windows 表格软件识别中文；newline 留给 csv 模块管理。
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})  # 输出列由 fields 控制，缺失字段留空，而不是依赖字典内部顺序。


def _rows(conn: sqlite3.Connection, query: str) -> list[dict[str, object]]:  # 执行只读 SQL 查询，并把结果转换为普通字典列表。
    conn.row_factory = sqlite3.Row
    return [dict(row) for row in conn.execute(query).fetchall()]


def _duration_to_next(
    series: list[dict[str, object]],
    index: int,
    interval: int,
    max_gap: int,
) -> int:  # 估计某条快照持续到下一条快照的有效秒数，并限制异常采样间隔。
    current = _parse_time(str(series[index]["observed_at"]))
    if index + 1 >= len(series):  # 最后一条快照没有后续观察，只按配置间隔估计时长。
        return interval
    next_time = _parse_time(str(series[index + 1]["observed_at"]))
    return max(0, min(int((next_time - current).total_seconds()), max_gap))  # 长时间停采不全部归入上一用户；最多计入 max_gap 秒。


def main() -> None:  # 从 SQLite 主库导出原始快照、事件、当前实例与用户 GPU 时间占比报表。
    config = load_config(Path("config.yaml"))
    database = config.usage_tracking.database_path
    if not database.exists():
        raise SystemExit(f"找不到占用数据库：{database}")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    export_dir = config.usage_tracking.export_dir / timestamp
    export_dir.mkdir(parents=True, exist_ok=False)  # 每次导出新建时间戳目录，不覆盖可能正被 Excel 打开的报表。

    conn = sqlite3.connect(database)
    try:
        entry_rows = _rows(  # 入口原始记录可用于回看采集结果，不能直接累加为实例使用时长。
            conn,
            """
            SELECT observed_at, host, machine_name, gpu_index, gpu_uuid, gpu_name,
                   occupied, instance_id, user, started_at
            FROM entry_snapshots ORDER BY observed_at, machine_name, gpu_index
            """,
        )
        instance_rows = _rows(  # 去重后的实例快照用于计算实例分配时间。
            conn,
            """
            SELECT observed_at, host, machine_name, gpu_index, gpu_uuid, gpu_name,
                   instance_id, user, started_at
            FROM instance_snapshots ORDER BY observed_at, host, gpu_index, user
            """,
        )
        gpu_rows = _rows(  # 每张物理卡上的用户集合用于计算共享占用的折算时间。
            conn,
            """
            SELECT observed_at, host, gpu_index, gpu_uuid, gpu_name, occupant_count,
                   active_users, active_instance_ids, active_entries
            FROM gpu_snapshots ORDER BY observed_at, host, gpu_index
            """,
        )
        event_rows = _rows(  # 事件是观察到的开始/结束变化，并非平台的精确计费事件。
            conn,
            """
            SELECT event_time, event, host, machine_name, gpu_index, gpu_name,
                   user, instance_id, started_at, ended_at, duration_seconds
            FROM occupancy_events ORDER BY event_time, host, gpu_index, user
            """,
        )
        current_rows = _rows(
            conn,
            """
            SELECT host, machine_name, gpu_index, gpu_name, user, instance_id,
                   started_at, first_seen_at, last_seen_at
            FROM current_instances ORDER BY host, gpu_index, user
            """,
        )
    finally:
        conn.close()

    _write_csv(
        export_dir / "occupancy_entry_snapshots.csv",
        entry_rows,
        [
            "observed_at", "host", "machine_name", "gpu_index", "gpu_uuid",
            "gpu_name", "occupied", "instance_id", "user", "started_at",
        ],
    )
    _write_csv(
        export_dir / "occupancy_instance_snapshots.csv",
        instance_rows,
        [
            "observed_at", "host", "machine_name", "gpu_index", "gpu_uuid",
            "gpu_name", "instance_id", "user", "started_at",
        ],
    )
    _write_csv(
        export_dir / "occupancy_gpu_snapshots.csv",
        gpu_rows,
        [
            "observed_at", "host", "gpu_index", "gpu_uuid", "gpu_name",
            "occupant_count", "active_users", "active_instance_ids", "active_entries",
        ],
    )
    _write_csv(
        export_dir / "occupancy_events.csv",
        event_rows,
        [
            "event_time", "event", "host", "machine_name", "gpu_index", "gpu_name",
            "user", "instance_id", "started_at", "ended_at", "duration_seconds",
        ],
    )
    _write_csv(
        export_dir / "current_instances.csv",
        current_rows,
        [
            "host", "machine_name", "gpu_index", "gpu_name", "user", "instance_id",
            "started_at", "first_seen_at", "last_seen_at",
        ],
    )

    interval = max(1, config.usage_tracking.interval_seconds)
    max_gap = interval * 2  # 最多把两倍配置间隔计为连续观察，避免断线夸大统计。

    allocated_seconds: dict[str, int] = defaultdict(int)  # Metric 1: platform allocation time.  Each distinct instance contributes its own GPU time, even when several instances share one physical GPU.
    sample_count: dict[str, int] = defaultdict(int)
    instances_by_user: dict[str, set[str]] = defaultdict(set)
    by_instance: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    for row in instance_rows:
        by_instance[(str(row["host"]), str(row["instance_id"]))].append(row)
    for series in by_instance.values():
        series.sort(key=lambda row: _parse_time(str(row["observed_at"])))
        for index, row in enumerate(series):
            user = str(row.get("user", ""))
            if not user:
                continue
            duration = _duration_to_next(series, index, interval, max_gap)
            allocated_seconds[user] += duration  # 同用户的不同实例各自累计，多个实例共享一张卡时可超过物理时长。
            sample_count[user] += 1
            instances_by_user[user].add(str(row["instance_id"]))  # 集合用于最终统计该用户出现过多少个不同实例 ID。

    physical_seconds: dict[str, float] = defaultdict(float)  # Metric 2: physical-equivalent time.  If N users concurrently share one physical GPU, each receives 1/N of that interval.  Shares therefore sum to the actual physical GPU capacity and are appropriate for pie charts.
    by_gpu: dict[tuple[str, int], list[dict[str, object]]] = defaultdict(list)
    for row in gpu_rows:
        by_gpu[(str(row["host"]), int(row["gpu_index"]))].append(row)
    for series in by_gpu.values():
        series.sort(key=lambda row: _parse_time(str(row["observed_at"])))
        for index, row in enumerate(series):
            try:
                users = sorted(set(json.loads(str(row["active_users"]))))  # 数据库内用 JSON 保存用户列表；先去重，按用户而非实例分摊。
            except (json.JSONDecodeError, TypeError):
                users = []
            users = [str(user) for user in users if str(user)]
            if not users:
                continue
            duration = _duration_to_next(series, index, interval, max_gap)
            share = duration / len(users)  # N 个用户共同占用时每人计入 1/N；这是均摊估计，不是实测算力占比。
            for user in users:
                physical_seconds[user] += share

    users = sorted(
        set(allocated_seconds) | set(physical_seconds),
        key=lambda user: (-allocated_seconds.get(user, 0), user),
    )
    allocated_total = sum(allocated_seconds.values())  # 实例分配占比的分母，与物理折算口径的分母分别计算。
    physical_total = sum(physical_seconds.values())
    summary_rows: list[dict[str, object]] = []
    for user in users:
        alloc = allocated_seconds.get(user, 0)
        physical = physical_seconds.get(user, 0.0)
        summary_rows.append(
            {
                "user": user,
                "allocated_gpu_hours": f"{alloc / 3600:.4f}",
                "allocated_share_pct": f"{(alloc / allocated_total * 100) if allocated_total else 0:.2f}",
                "physical_equivalent_gpu_hours": f"{physical / 3600:.4f}",
                "physical_share_pct": f"{(physical / physical_total * 100) if physical_total else 0:.2f}",
                "distinct_instances": len(instances_by_user.get(user, set())),
                "snapshot_count": sample_count.get(user, 0),
            }
        )
    _write_csv(
        export_dir / "user_gpu_share.csv",
        summary_rows,
        [
            "user", "allocated_gpu_hours", "allocated_share_pct",
            "physical_equivalent_gpu_hours", "physical_share_pct",
            "distinct_instances", "snapshot_count",
        ],
    )

    latest_file = config.usage_tracking.export_dir / "LATEST_EXPORT.txt"
    latest_file.parent.mkdir(parents=True, exist_ok=True)
    latest_file.write_text(str(export_dir), encoding="utf-8")  # 记录最近导出目录，辅助脚本可据此打开结果。

    print(f"已从 SQLite 导出：{export_dir}")
    print(f"原始入口行：{len(entry_rows)}")
    print(f"实例快照行：{len(instance_rows)}")
    print(f"物理 GPU 快照行：{len(gpu_rows)}")
    print(f"事件行：{len(event_rows)}")
    print(f"用户数：{len(summary_rows)}")
    print("比例图建议使用 user_gpu_share.csv 的 physical_share_pct。")


if __name__ == "__main__":
    main()
