"""AccessLogSource(redesign/01-collect.md 第 1 节，可选，默认不启用)。

sources.access-log.query 有值且配置了 extensions.log-platform 时启用：按时间窗口经日志平台取访问日志原文，解析为请求，
按接口统计后与基线比较；退化的接口各产出一条信号(source 为 performance，check 为 latency-regression 或
error-rate-regression，location 为「方法 路由」)。基线保存在读取位置(cursor.baseline)中，随信号在同一事务中更新。
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from typing import Any

from tightrein.config.project import ProjectConfig
from tightrein.domain.clock import format_iso
from tightrein.domain.enums import ExtensionPoint, ProbeLevel, RunStatus, Source
from tightrein.domain.enums import Probe as ProbeKind
from tightrein.domain.run import Coverage
from tightrein.extensions.client import ExtensionClient
from tightrein.sources.access_log import parse, stats
from tightrein.sources.base import PROBE_LEVELS, ProbeOptions, ProbeOutcome, ProbeTarget, failed, skipped
from tightrein.sources.common import window
from tightrein.sources.common.redact import ProbeRedactor
from tightrein.sources.common.signals import RandomBytes, SignalFactory
from tightrein.sources.platform_errors.logs import ReleaseAt
from tightrein.sources.platform_errors.source import failure_text

SETTINGS = "sources.access-log"
SOURCE_NAME = "access-log"
BASELINE = "baseline"
DISABLED = "未启用：没有配置 sources.access-log.query 与 extensions.log-platform"


@dataclass(frozen=True)
class AccessLogDependencies:
    config: ProjectConfig
    client: ExtensionClient
    conn: sqlite3.Connection
    redactor: ProbeRedactor
    release_at: ReleaseAt
    randomness: RandomBytes = os.urandom


class AccessLogSource:
    name = ProbeKind.ACCESS_LOG
    levels = PROBE_LEVELS[ProbeKind.ACCESS_LOG]

    def __init__(self, dependencies: AccessLogDependencies) -> None:
        self.deps = dependencies

    def _setting(self, key: str) -> Any:
        return self.deps.config.get(f"{SETTINGS}.{key}")

    def run(self, target: ProbeTarget, level: ProbeLevel | None, options: ProbeOptions) -> ProbeOutcome:
        query = self.deps.config.data.get("sources", {}).get("access-log", {}).get("query")
        if not query or not self.deps.client.configured(ExtensionPoint.LOG_PLATFORM):
            return skipped(DISABLED)
        now = target.clock.now()
        span = window.plan(self.deps.conn, SOURCE_NAME, now, int(self._setting("initialLookbackHours")))
        fetched = self.deps.client.log_platform(query, span.since, span.until, int(self._setting("limit")))
        extensions = {fetched.point.value: fetched.stats_entry()}
        if fetched.output is None:
            return failed(failure_text(fetched), *fetched.notes, extensions=extensions)
        lines = [line for chunk in fetched.output["chunks"] for line in chunk["text"].splitlines()]
        requests, unparsed = parse.parse(lines, dict(self._setting("fields")), self._setting("pattern"))
        current = stats.summarize(requests)
        previous = span.previous.cursor.get(BASELINE, {}) if span.previous is not None else {}
        baseline = {endpoint: stats.EndpointStats.from_dict(item) for endpoint, item in previous.items()}
        found = stats.compare(current, baseline, min_requests=int(self._setting("minRequests")),
                              latency_ratio=float(self._setting("latencyRatio")),
                              error_rate_delta=float(self._setting("errorRateDelta")))
        factory = SignalFactory(target, self.name, self.deps.redactor, randomness=self.deps.randomness)
        signals = [factory.create(
            source=Source.PERFORMANCE, check=item.check, location=item.endpoint,
            message=f"{item.endpoint} 退化：{item.describe()}", occurred_at=span.until,
            release=self.deps.release_at(span.until),
            context={"sourceName": SOURCE_NAME, "window": {"since": format_iso(span.since),
                                                           "until": format_iso(span.until)},
                     "current": item.current.to_dict(), "baseline": item.baseline.to_dict()})
            for item in found]
        notes = list(fetched.notes)
        gap = span.gap(fetched.output["oldestAvailable"])
        if gap:
            notes.append(gap)
        if fetched.output["truncated"]:
            notes.append(f"访问日志超过 {SETTINGS}.limit，本次只按读到的部分统计")
        if unparsed:
            notes.append(f"{unparsed} 行访问日志无法解析，检查 {SETTINGS}.fields 或 pattern")
        if not baseline:
            notes.append("还没有基线，本次统计作为基线")
        merged = stats.update(baseline, current, float(self._setting("baselineWeight")))
        cursor = span.advance(now, {BASELINE: {endpoint: item.to_dict() for endpoint, item in merged.items()}})
        result_stats = {"requests": len(requests), "endpoints": len(current), "unparsedLines": unparsed,
                        "signals": len(signals)}
        return ProbeOutcome(RunStatus.OK, tuple(signals), Coverage(sources=(SOURCE_NAME,) if requests else ()),
                            stats=result_stats, notes=tuple(notes), extensions=extensions, cursors=(cursor,))
