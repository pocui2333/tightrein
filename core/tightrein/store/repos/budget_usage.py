"""budget_usage 表：各环节每天的费用累计(design 11.3)，键为(环节、日期)。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime

from tightrein.domain.clock import Clock, format_iso
from tightrein.domain.enums import Stage
from tightrein.store.repos.table import BOOL, DATE, TIME, Table, enum_codec, given


@dataclass(frozen=True)
class BudgetUsage:
    stage: Stage
    date: date
    cost_usd: float
    input_tokens: int
    output_tokens: int
    estimated: bool
    updated_at: datetime


TABLE = Table(
    "budget_usage",
    BudgetUsage,
    ("stage", "date"),
    {"stage": enum_codec(Stage), "date": DATE, "estimated": BOOL, "updated_at": TIME},
    order_by="date, stage",
)


def add(
    conn: sqlite3.Connection,
    stage: Stage,
    day: date,
    cost_usd: float,
    input_tokens: int,
    output_tokens: int,
    estimated: bool,
    clock: Clock,
) -> BudgetUsage:
    """把一次调用的用量累加到当天；任何一次含估算费用，当天即记为含估算。返回累加后的记录。"""
    conn.execute(
        "INSERT INTO budget_usage (stage, date, cost_usd, input_tokens, output_tokens, estimated, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (stage, date) DO UPDATE SET "
        "cost_usd = cost_usd + excluded.cost_usd, input_tokens = input_tokens + excluded.input_tokens, "
        "output_tokens = output_tokens + excluded.output_tokens, estimated = MAX(estimated, excluded.estimated), "
        "updated_at = excluded.updated_at",
        (stage.value, day.isoformat(), cost_usd, input_tokens, output_tokens, BOOL.encode(estimated),
         format_iso(clock.now())),
    )
    usage = get(conn, stage, day)
    if usage is None:
        raise LookupError(f"累加后读不到 {stage.value} {day} 的用量")
    return usage


def get(conn: sqlite3.Connection, stage: Stage, day: date) -> BudgetUsage | None:
    return TABLE.get(conn, stage=stage, date=day)


def find(conn: sqlite3.Connection, *, stage: Stage | None = None, since: date | None = None) -> list[BudgetUsage]:
    """按日期升序；since 给出时只取该日及以后。"""
    usages = TABLE.find(conn, **given({"stage": stage}))
    return [usage for usage in usages if since is None or usage.date >= since]


def total(conn: sqlite3.Connection, since: date | None = None) -> float:
    """全部环节的费用合计；since 给出时只计该日及以后。"""
    return sum(usage.cost_usd for usage in find(conn, since=since))
