"""metric_snapshots 表：每周指标快照，键为(周、指标、维度)；总计的维度为 all。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime

from tightrein.store.repos.table import DATE, TIME, Table

OVERALL = "all"


@dataclass(frozen=True)
class MetricSnapshot:
    week: date
    metric: str
    dimension: str
    sample_size: int
    computed_at: datetime
    value: float | None = None
    numerator: float | None = None
    denominator: float | None = None


TABLE = Table(
    "metric_snapshots",
    MetricSnapshot,
    ("week", "metric", "dimension"),
    {"week": DATE, "computed_at": TIME},
    order_by="week, metric, dimension",
)


def save(conn: sqlite3.Connection, snapshot: MetricSnapshot) -> None:
    TABLE.save(conn, snapshot)


def for_week(conn: sqlite3.Connection, week: date) -> list[MetricSnapshot]:
    return TABLE.find(conn, week=week)


def series(
    conn: sqlite3.Connection, metric: str, dimension: str = OVERALL, since: date | None = None
) -> list[MetricSnapshot]:
    """某指标某维度的逐周数值，按周升序；since 给出时只取该周及以后。"""
    snapshots = TABLE.find(conn, metric=metric, dimension=dimension)
    return [snapshot for snapshot in snapshots if since is None or snapshot.week >= since]
