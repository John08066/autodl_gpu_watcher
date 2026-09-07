"""
旧版 CSV 数据迁移工具 — 将 v0.4.2 及之前版本的 CSV 占用日志导入 SQLite。

用法：
    python -m autodl_watcher.usage_import_legacy

背景：
    v0.4.3 起，占用日志从 CSV 切换为 SQLite 作为唯一事实源。
    此脚本用于将旧版 CSV（occupancy_entry_snapshots.csv）中的历史
    数据导入新版的 SQLite 数据库。

注意事项：
    - 如果 SQLite 数据库中已有 entry_snapshots 数据，跳过导入
    - CSV 中的记录按 5 秒窗口分批次重建（因为 203-1/203-2 的采样相差约 1 秒）
"""

from __future__ import annotations

import csv
import sqlite3
from datetime import datetime
from pathlib import Path

from .config import load_config
from .models import OccupancyRecord
from .usage import UsageSqliteLogger


def _parse_bool(value: str) -> bool:
    """功能：
        将旧 CSV 中的常见真假字符串解析为布尔值。

    参数：
        value (str)：待规范化、解析或转换的输入值。

    返回：
        bool：解析后的布尔值。
    """
    return value.strip().lower() in {"1", "true", "yes", "是"}


def _record_from_row(row: dict[str, str]) -> OccupancyRecord:
    """功能：
        把旧版 CSV 的一行字段转换为 OccupancyRecord。

    参数：
        row (dict[str, str])：CSV 行字典或占用详情表中的单行数据。

    返回：
        OccupancyRecord：由旧 CSV 行构造的 OccupancyRecord。
    """
    return OccupancyRecord(
        observed_at=datetime.fromisoformat(row["observed_at"]),
        host=row["host"],
        machine_name=row["machine_name"],
        gpu_index=int(row["gpu_index"]),
        gpu_uuid=row.get("gpu_uuid", ""),
        gpu_name=row.get("gpu_name", ""),
        occupied=_parse_bool(row.get("occupied", "0")),
        instance_id=row.get("instance_id", ""),
        user=row.get("user", ""),
        started_at_text=row.get("started_at", ""),
    )


def main() -> None:
    """功能：
        把旧版实时 CSV 快照导入新版 SQLite 数据库，保留已有历史。

    参数：
        无。

    返回：
        None：函数通过副作用完成初始化、输出、持久化或资源管理。
    """
    config = load_config(Path("config.yaml"))
    database = config.usage_tracking.database_path
    legacy = database.parent / "occupancy_entry_snapshots.csv"
    if not legacy.exists():
        raise SystemExit(f"找不到旧版原始入口 CSV：{legacy}")

    database.parent.mkdir(parents=True, exist_ok=True)
    if database.exists():
        conn = sqlite3.connect(database)
        try:
            try:
                count = conn.execute("SELECT COUNT(*) FROM entry_snapshots").fetchone()[0]
            except sqlite3.OperationalError:
                count = 0
        finally:
            conn.close()
        if count:
            raise SystemExit(
                f"数据库已有 {count} 条入口记录，为防止重复导入已停止。"
                "请在首次运行 v0.4.3 主程序之前执行迁移。"
            )

    records: list[OccupancyRecord] = []
    with legacy.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            try:
                records.append(_record_from_row(row))
            except (KeyError, ValueError):
                continue
    records.sort(key=lambda item: item.observed_at)
    if not records:
        raise SystemExit("旧版 CSV 中没有可导入记录。")

    # 203-1 / 203-2 are collected sequentially and usually differ by ~1 second.
    # Group neighbouring rows within five seconds into one logical capture.
    batches: list[list[OccupancyRecord]] = []
    current: list[OccupancyRecord] = []
    batch_start: datetime | None = None
    for item in records:
        if batch_start is None or (item.observed_at - batch_start).total_seconds() <= 5:
            current.append(item)
            batch_start = batch_start or item.observed_at
        else:
            batches.append(current)
            current = [item]
            batch_start = item.observed_at
    if current:
        batches.append(current)

    logger = UsageSqliteLogger(database)
    for batch in batches:
        logger.record(batch)

    print(f"旧版 CSV：{legacy}")
    print(f"SQLite：{database}")
    print(f"导入原始行：{len(records)}")
    print(f"重建采集批次：{len(batches)}")


if __name__ == "__main__":
    main()
