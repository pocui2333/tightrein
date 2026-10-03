"""PlatformErrorsSource(redesign/01-collect.md 第 1 节)：内部错误，只采集应用运行报错与前端错误。

两个来源各自按时间窗口增量读取(sources/common/window.py)，读取位置随信号保存：
- 错误追踪平台(extensions.error-tracking)：窗口内有新事件的错误分组，每个分组一条信号，指纹为平台分组编号；
- 集中日志平台(extensions.log-platform 加 sources.platform-errors.logQuery)：按查询取原文，经 log-parse 映射字段，
  保留 sources.platform-errors.levels 的条目，指纹按逻辑位置计算。
两个都没有配置时为 skipped(未启用)。某个来源失败时不保存它的读取位置、写明原因，另一个照常，状态为 partial；
都失败为 failed。读到数据的来源记入 coverage.sources(覆盖运行据此判断平台问题是否已解决)。
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

from tightrein.config.project import ProjectConfig
from tightrein.domain.clock import parse_iso
from tightrein.domain.enums import ExtensionPoint, ProbeLevel, RunStatus
from tightrein.domain.enums import Probe as ProbeKind
from tightrein.domain.run import Coverage
from tightrein.extensions.client import ExtensionClient
from tightrein.extensions.result import PointResult
from tightrein.sources.base import PROBE_LEVELS, ProbeOptions, ProbeOutcome, ProbeTarget, failed, skipped
from tightrein.sources.common import window
from tightrein.sources.common.redact import ProbeRedactor
from tightrein.sources.common.signals import RandomBytes, SignalFactory
from tightrein.sources.platform_errors import logs, select, tracking
from tightrein.sources.platform_errors.logs import ReleaseAt
from tightrein.store.repos.source_cursors import SourceCursor

DISABLED = ("未启用：没有配置 extensions.error-tracking，也没有配置 extensions.log-platform 与 "
            "sources.platform-errors.logQuery")
SETTINGS = "sources.platform-errors"


def failure_text(result: PointResult) -> str:
    detail = result.failure.describe() if result.failure is not None else "没有输出"
    return f"{result.point.value} 失败：{detail}"


@dataclass(frozen=True)
class PlatformErrorsDependencies:
    config: ProjectConfig
    client: ExtensionClient
    conn: sqlite3.Connection
    redactor: ProbeRedactor
    release_at: ReleaseAt
    randomness: RandomBytes = os.urandom


@dataclass
class _Read:
    signals: list = field(default_factory=list)
    cursors: list[SourceCursor] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    failures: int = 0
    stats: dict[str, float] = field(default_factory=dict)
    extensions: dict = field(default_factory=dict)


def last_time(chunks: list[dict]) -> datetime | None:
    """片段中最晚一条的时间(片段的 modifiedAt)。"""
    found = [parse_iso(chunk["modifiedAt"]) for chunk in chunks if chunk.get("modifiedAt")]
    return max(found) if found else None


def log_query(config: ProjectConfig) -> str | None:
    return config.data.get("sources", {}).get("platform-errors", {}).get("logQuery") or None


class PlatformErrorsSource:
    name = ProbeKind.PLATFORM_ERRORS
    levels = PROBE_LEVELS[ProbeKind.PLATFORM_ERRORS]

    def __init__(self, dependencies: PlatformErrorsDependencies) -> None:
        self.deps = dependencies

    def run(self, target: ProbeTarget, level: ProbeLevel | None, options: ProbeOptions) -> ProbeOutcome:
        client = self.deps.client
        tracking_on = client.configured(ExtensionPoint.ERROR_TRACKING)
        query = log_query(self.deps.config) if client.configured(ExtensionPoint.LOG_PLATFORM) else None
        if not tracking_on and query is None:
            return skipped(DISABLED)
        factory = SignalFactory(target, self.name, self.deps.redactor, randomness=self.deps.randomness)
        read = _Read()
        now = target.clock.now()
        lookback = int(self.deps.config.get(f"{SETTINGS}.initialLookbackHours"))
        if tracking_on:
            self._tracking(read, factory, window.plan(self.deps.conn, f"{self.name.value}:{tracking.SOURCE_NAME}",
                                                      now, lookback), now)
        if query is not None:
            self._logs(read, factory, query, window.plan(self.deps.conn, f"{self.name.value}:{logs.SOURCE_NAME}",
                                                         now, lookback), now)
        attempted = int(tracking_on) + int(query is not None)
        if read.failures == attempted:
            return failed(*read.notes, extensions=read.extensions)
        status = RunStatus.PARTIAL if read.failures else RunStatus.OK
        read.stats["signals"] = len(read.signals)
        return ProbeOutcome(status, tuple(read.signals), Coverage(sources=tuple(read.sources)), stats=read.stats,
                            notes=tuple(read.notes), extensions=read.extensions, cursors=tuple(read.cursors))

    def _tracking(self, read: _Read, factory: SignalFactory, span: window.Window, now: datetime) -> None:
        result = self.deps.client.error_tracking(span.since, span.until)
        read.extensions[result.point.value] = result.stats_entry()
        read.notes += list(result.notes)
        if result.output is None:
            read.failures += 1
            read.notes.append(failure_text(result))
            return
        output = result.output
        gap = span.gap(output["oldestAvailable"])
        if gap:
            read.notes.append(gap)
        if output["truncated"]:
            read.notes.append(f"错误追踪平台在 {span.describe()} 内的分组超过条数上限，只读取了一部分")
        read.signals += tracking.to_signals(output["issues"], factory, self.deps.release_at,
                                            self.deps.redactor.limits.project_frames)
        read.stats["issues"] = len(output["issues"])
        read.sources.append(tracking.SOURCE_NAME)
        read.cursors.append(span.advance(now))

    def _logs(self, read: _Read, factory: SignalFactory, query: str, span: window.Window, now: datetime) -> None:
        client = self.deps.client
        limit = int(self.deps.config.get(f"{SETTINGS}.logLimit"))
        fetched = client.log_platform(query, span.since, span.until, limit)
        read.extensions[fetched.point.value] = fetched.stats_entry()
        read.notes += list(fetched.notes)
        if fetched.output is None:
            read.failures += 1
            read.notes.append(failure_text(fetched))
            return
        parse_state = span.previous.parse_state if span.previous is not None else None
        parsed = client.log_parse(fetched.output["chunks"], parse_state)
        read.extensions[parsed.point.value] = parsed.stats_entry()
        if parsed.output is None:
            read.failures += 1
            read.notes.append(failure_text(parsed))
            return
        gap = span.gap(fetched.output["oldestAvailable"])
        if gap:
            read.notes.append(gap)
        reached = None
        if fetched.output["truncated"]:
            reached = last_time(fetched.output["chunks"])
            read.notes.append(f"日志平台在 {span.describe()} 内的条目超过 {SETTINGS}.logLimit，其余下次继续读取")
        entries = parsed.output["entries"]
        chosen = select.apply(entries, self.deps.config.error_levels(), self.deps.redactor.limits.project_frames)
        read.signals += logs.to_signals(chosen, factory, self.deps.redactor, self.deps.release_at)
        read.stats.update(logEntries=len(entries), selectedEntries=len(chosen), unparsedLines=parsed.output["unparsed"])
        read.sources.append(logs.SOURCE_NAME)
        read.cursors.append(span.advance(now, parse_state=parsed.output["state"], until=reached))
