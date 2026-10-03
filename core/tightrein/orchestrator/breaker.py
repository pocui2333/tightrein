"""熔断(redesign/09-loop.md 第 3 节)：无人值守推进中，同一对象连续失败达到 thresholds.loop.breakerFailures 次，或同一步
执行后状态没有前进达到 thresholds.loop.breakerRepeats 次时，停止处理该对象并转待决定(由 trip 回调完成，编排注入为
FixService.stop_unattended)。计数存 breaker_counts；状态前进即清零，熔断后清零重新计数(用户放行后再失败才再次熔断)。
停在关口(待确认操作、等待用户、等待部署)不计数。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import replace

from tightrein.config.project import ProjectConfig
from tightrein.domain.clock import Clock
from tightrein.store.repos import breaker_counts
from tightrein.store.repos.breaker_counts import BreakerCount

FAILED = "failed"
UNCHANGED = "unchanged"
PROGRESS = "progress"
PREFIX = "熔断"

Trip = Callable[[str, str], None]


class Breaker:
    def __init__(self, conn: sqlite3.Connection, clock: Clock, config: ProjectConfig, trip: Trip) -> None:
        self.conn = conn
        self.clock = clock
        self.failures = config.whole_threshold("loop.breakerFailures")
        self.repeats = config.whole_threshold("loop.breakerRepeats")
        self.trip = trip

    def observe(self, subject_id: str, step: str, outcome: str, reason: str) -> str | None:
        """记录一步的结果；达到上限时调用 trip 并返回熔断原因。"""
        now = self.clock.now()
        if outcome == PROGRESS:
            breaker_counts.clear(self.conn, subject_id)
            return None
        count = breaker_counts.get(self.conn, subject_id) or BreakerCount(subject_id, now)
        same = count.step == step
        failures = count.failures + 1 if outcome == FAILED else 0
        repeats = (count.repeats + 1 if same else 1) if outcome == UNCHANGED else 0
        tripped = None
        if failures >= self.failures:
            tripped = f"{PREFIX}：{step} 连续失败 {failures} 次(最后一次：{reason})"
        elif repeats >= self.repeats:
            tripped = f"{PREFIX}：{step} 连续 {repeats} 次执行后没有进展(最后一次：{reason})"
        if tripped is not None:
            breaker_counts.save(self.conn, BreakerCount(subject_id, now, step, 0, 0, tripped, now))
            self.trip(subject_id, tripped)
            return tripped
        breaker_counts.save(self.conn, replace(count, updated_at=now, step=step, failures=failures, repeats=repeats,
                                               last_reason=reason))
        return None
