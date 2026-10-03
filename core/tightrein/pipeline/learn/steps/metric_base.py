"""learn 指标的公共部分：指标值、计算上下文、维度写法，以及本周以已修复关闭的 Issue。"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, tzinfo
from typing import Any

from tightrein.config.project import ProjectConfig
from tightrein.domain.clock import format_iso, parse_iso
from tightrein.domain.enums import CloseReason, IssueStatus
from tightrein.pipeline.learn.steps.weeks import Window
from tightrein.store.files.layout import WorkspaceLayout


@dataclass(frozen=True)
class MetricValue:
    metric: str
    dimension: str
    value: float | None
    numerator: float | None
    denominator: float | None
    sample_size: int

    @classmethod
    def ratio(cls, metric: str, dimension: str, numerator: float, denominator: float | None,
              sample_size: int | None = None) -> MetricValue:
        value = numerator / denominator if denominator else None
        size = sample_size if sample_size is not None else int(denominator or 0)
        return cls(metric, dimension, value, numerator, denominator, size)

    @classmethod
    def count(cls, metric: str, dimension: str, value: float, sample_size: int | None = None) -> MetricValue:
        return cls(metric, dimension, value, None, None, int(value) if sample_size is None else sample_size)

    def to_dict(self) -> dict[str, Any]:
        return {"metric": self.metric, "dimension": self.dimension, "value": self.value, "numerator": self.numerator,
                "denominator": self.denominator, "sampleSize": self.sample_size}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> MetricValue:
        return cls(data["metric"], data["dimension"], data["value"], data["numerator"], data["denominator"],
                   data["sampleSize"])


@dataclass(frozen=True)
class MetricContext:
    conn: sqlite3.Connection
    layout: WorkspaceLayout
    config: ProjectConfig
    window: Window
    now: datetime
    zone: tzinfo | None

    def between(self) -> tuple[str, str]:
        return format_iso(self.window.start), format_iso(self.window.end)


def dim(key: str, value: object) -> str:
    return f"{key}={value}"


def fixed_closes(conn: sqlite3.Connection, since: datetime, until: datetime) -> dict[str, datetime]:
    """[since, until) 内完成(PR 合并，关闭原因已修复)的 Issue 与时间(同一 Issue 多次完成取最后一次)。"""
    rows = conn.execute(
        "SELECT issue_id, at FROM issue_events WHERE to_status = ? AND close_reason = ? AND at >= ? AND at < ? "
        "ORDER BY at", (IssueStatus.DONE.value, CloseReason.FIXED.value, format_iso(since), format_iso(until)))
    return {row["issue_id"]: parse_iso(row["at"]) for row in rows}
