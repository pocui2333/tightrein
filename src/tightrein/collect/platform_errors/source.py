"""平台错误的采集流程：读错误追踪与日志平台两个子来源，各自成败，再合成一个来源结果。

- 用哪些平台写在 setup.json 的 `collect.platform_errors.method`：一个方法名，或用 `+` 连起来的两个(`sentry+loki`)；
  方法名在 error_tracking/ 或 log_platform/ 下找；日志的解析方法取 controls."collect.platform_errors".logParse；
- 两个子来源各用一个读取位置(`collect.platform_errors:error_tracking`、`collect.platform_errors:log_platform`)，
  并行读取；一个失败时不保存它的位置、写明原因，另一个照常，状态为 partial；都失败才是 failed；
- 日志读满 logLimit 时，读取位置停在已读片段中最晚的时间，其余下次接着读(已知遗留：同一秒的条目可能重读一次，
  表现为多计一次出现)；日志解析状态存在读取位置里，下次原样交回解析方法；
- 读到数据的子来源才记进覆盖范围：平台问题的「已解决」只按真正读过的子来源判定。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from tightrein.collect.common import window
from tightrein.collect.common.signals import ReleaseAt, Signal, SignalFactory, factory_for, releases
from tightrein.collect.common.source import (
    SourceError,
    SourceMisconfigured,
    SourceResult,
    SourceStatus,
    describe,
    each,
    failed,
    skipped,
)
from tightrein.collect.platform_errors import log_signals, select, tracking_signals
from tightrein.protocol import methods
from tightrein.protocol.handoff import Metrics
from tightrein.protocol.http import Transport, UrllibTransport
from tightrein.protocol.naming import format_iso, parse_duration

if TYPE_CHECKING:
    from tightrein.protocol.runtime import Runtime

SOURCE = "collect.platform_errors"
TRACKING = tracking_signals.SOURCE_NAME
LOGS = log_signals.SOURCE_NAME
TRACKING_METHODS = "tightrein.collect.platform_errors.error_tracking"
LOG_METHODS = "tightrein.collect.platform_errors.log_platform"
PARSE_METHODS = "tightrein.collect.platform_errors.log_parse"
METHOD_SEPARATOR = "+"
NO_METHOD = "setup.json 中 collect.platform_errors 没有写 method(sentry、loki 或 sentry+loki)"


@dataclass
class Part:
    """一个子来源本次读到的东西。"""

    name: str
    signals: list[Signal]
    read: int
    state: dict[str, Any]
    notes: list[str] = field(default_factory=list)
    produced: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class Context:
    """两个子来源共用、在主线程里一次准备好的东西(数据库只在主线程读)。"""

    runtime: Runtime
    section: dict[str, Any]
    factory: SignalFactory
    release_at: ReleaseAt
    transport: Transport
    timeout_s: float
    now: datetime


def collect(runtime: Runtime, transport: Transport | None = None) -> SourceResult:
    """可能抛 SourceError(整个来源配置不对)，由调用方经 common.source.guarded 归类。"""
    module = runtime.setup.module(SOURCE)
    if not runtime.setup.enabled(SOURCE):
        return skipped(SOURCE, f"未启用：{module.reason or '接入清单中为 disabled'}")
    if not module.method:
        raise SourceMisconfigured(NO_METHOD)
    section = runtime.settings.section(SOURCE)
    context = Context(runtime, section, factory_for(runtime, SOURCE), releases(runtime.conn),
                      transport or UrllibTransport(), runtime.settings.duration("limits.timeouts.http"),
                      runtime.clock.now())
    lookback = parse_duration(section["lookback"])
    jobs: dict[str, Callable[[], Part]] = {}
    spans: list[window.Window] = []
    for name in module.method.split(METHOD_SEPARATOR):
        if methods.exists(TRACKING_METHODS, name):
            span = window.plan(runtime.conn, window.state_key(SOURCE, TRACKING), context.now, lookback)
            jobs[TRACKING] = _tracking_job(context, name, span)
        elif methods.exists(LOG_METHODS, name):
            span = window.plan(runtime.conn, window.state_key(SOURCE, LOGS), context.now, lookback)
            jobs[LOGS] = _logs_job(context, name, span)
        else:
            raise SourceMisconfigured(f"没有这个平台方法：{name}(error_tracking/ 与 log_platform/ 下都没有)")
        spans.append(span)
    found = each(jobs, workers=len(jobs))
    return combine(found, spans)


def combine(found: dict[str, Part | SourceError], spans: list[window.Window]) -> SourceResult:
    parts = [item for item in found.values() if isinstance(item, Part)]
    errors = [f"{name} 失败：{describe(item)}" for name, item in found.items() if isinstance(item, SourceError)]
    notes = [note for part in parts for note in part.notes]
    if not parts:
        return failed(SOURCE, "；".join(errors), notes=notes)
    state = {key: value for part in parts for key, value in part.state.items()}
    produced = {key: value for part in parts for key, value in part.produced.items()}
    signals = [signal for part in parts for signal in part.signals]
    produced["signals"] = len(signals)
    span = (format_iso(min(item.since for item in spans)), format_iso(max(item.until for item in spans)))
    return SourceResult(SOURCE, SourceStatus.PARTIAL if errors else SourceStatus.DONE, signals,
                        sum(part.read for part in parts), span, "；".join(errors) or None, state,
                        Metrics(produced=produced), coverage=[part.name for part in parts], notes=notes)


def _tracking_job(context: Context, name: str, span: window.Window) -> Callable[[], Part]:
    def job() -> Part:
        method = methods.load(TRACKING_METHODS, name)
        configured = methods.configure(method, settings=context.runtime.settings, source=SOURCE,
                                       secrets=context.runtime.secrets)
        found = method.module.read(configured, transport=context.transport, timeout_s=context.timeout_s,
                                   since=span.since, until=span.until, now=context.now)
        notes = [note for note in [span.gap(found.oldest_available)] if note]
        if found.truncated:
            notes.append(f"错误追踪平台在 {span.describe()} 内的分组超过条数上限，只读取了一部分")
        signals = tracking_signals.to_signals(found.issues, context.factory, context.release_at,
                                              int(context.section["projectFrames"]))
        return Part(TRACKING, signals, len(found.issues), {span.key: span.advance()}, notes,
                    {"issues": len(found.issues)})

    return job


def _logs_job(context: Context, name: str, span: window.Window) -> Callable[[], Part]:
    def job() -> Part:
        section = context.section
        query = section.get("logQuery")
        if not query:
            raise SourceMisconfigured(f'没有日志查询：在 controls."{SOURCE}".logQuery 写日志平台上取报错的查询')
        settings, secrets = context.runtime.settings, context.runtime.secrets
        method = methods.load(LOG_METHODS, name)
        configured = methods.configure(method, settings=settings, source=SOURCE, secrets=secrets)
        parser = methods.load(PARSE_METHODS, section["logParse"])
        parse_options = methods.configure(parser, settings=settings, source=SOURCE, secrets=secrets).options
        fetched = method.module.read(configured, transport=context.transport, timeout_s=context.timeout_s,
                                     query=query, since=span.since, until=span.until, limit=int(section["logLimit"]),
                                     now=context.now)
        parsed = parser.module.parse(fetched.chunks, parse_options, span.parse_state, context.now)
        notes = [note for note in [span.gap(fetched.oldest_available)] if note]
        reached = None
        if fetched.truncated:
            reached = fetched.last_time()
            notes.append(f"日志平台在 {span.describe()} 内的条目超过 logLimit，其余下次接着读")
        chosen = select.apply(parsed.entries, section["levels"], int(section["projectFrames"]))
        signals = log_signals.to_signals(chosen, context.factory, context.release_at)
        return Part(LOGS, signals, len(parsed.entries),
                    {span.key: span.advance(reached=reached, parse_state=parsed.state)}, notes,
                    {"logEntries": len(parsed.entries), "selectedEntries": len(chosen),
                     "unparsedLines": parsed.unparsed})

    return job
