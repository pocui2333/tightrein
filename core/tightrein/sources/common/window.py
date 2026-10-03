"""平台来源的增量读取(redesign/01-collect.md 第 1 节)：按时间窗口读取上次之后的新增内容、记录读取位置、检测保留期缺口。

- 窗口起点为上次保存的终点(source_cursors 的 cursor.until)；没有记录时取 now 往前 initialLookbackHours 小时；终点为 now。
- 平台给出的最早可查时间(oldestAvailable)晚于窗口起点时，中间这段已超出平台的保留期，写入运行说明。
- 读取位置随信号在同一事务中保存(ProbeOutcome.cursors)；读取失败时不保存，下次从原位置重读。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from tightrein.domain.clock import format_iso, parse_iso
from tightrein.store.repos import source_cursors
from tightrein.store.repos.source_cursors import SourceCursor

UNTIL = "until"


@dataclass(frozen=True)
class Window:
    source: str
    since: datetime
    until: datetime
    previous: SourceCursor | None = None

    def describe(self) -> str:
        return f"{format_iso(self.since)} 到 {format_iso(self.until)}"

    def gap(self, oldest: str | None) -> str | None:
        """平台的最早可查时间晚于窗口起点时的说明。"""
        if oldest is None:
            return None
        available = parse_iso(oldest)
        if available <= self.since:
            return None
        return (f"可能漏读：{self.source} 在 {format_iso(self.since)} 到 {format_iso(available)} 之间的内容已超出平台保留期；"
                "两次运行的间隔应短于保留期")

    def advance(self, now: datetime, extra: Mapping[str, Any] | None = None,
                parse_state: Mapping[str, Any] | None = None, until: datetime | None = None) -> SourceCursor:
        """until 给出时(读满条数上限，只读到窗口中间)读取位置停在它，其余下次继续读取。"""
        end = self.until if until is None else min(until, self.until)
        return SourceCursor(self.source, {UNTIL: format_iso(end), **(extra or {})}, now,
                            None if parse_state is None else dict(parse_state))


def plan(conn: sqlite3.Connection, source: str, now: datetime, lookback_hours: int) -> Window:
    previous = source_cursors.get(conn, source)
    until = now.replace(microsecond=0)
    if previous is not None and previous.cursor.get(UNTIL):
        since = parse_iso(previous.cursor[UNTIL])
    else:
        since = until - timedelta(hours=lookback_hours)
    return Window(source, min(since, until), until, previous)
