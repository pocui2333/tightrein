"""访问日志的采集流程：取数据 → 解析成请求 → 按接口统计 → 与基线比较、每个退化的接口一条信号 → 更新基线。

取数据两种取法，交出同样的东西(访问日志的行，或已解析的请求)，后面是同一套程序：
- 接入清单为 enabled：用 method 指定的日志平台方法(如 loki)按 controls."collect.access_log".query 取原文；
- 接入清单为 custom：运行项目按 project_sources/ 的方法文档写的脚本(script)，输出按
  project_sources/output.schema.json 校验。

基线存在读取位置(state 表 `collect.access_log`)里，按指数平均更新，与信号同一事务保存；第一个窗口只建基线、不报退化。
读取失败时整个来源失败，位置与基线都不前进，下次从原位置重读。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tightrein.collect.access_log import parse, stats
from tightrein.collect.access_log.log_platform.fetch import fetch
from tightrein.collect.common import window
from tightrein.collect.common.signals import factory_for, releases
from tightrein.collect.common.source import SourceInvalid, SourceMisconfigured, SourceResult, SourceStatus, skipped
from tightrein.onboard.setup import ModuleStatus
from tightrein.protocol import scripts
from tightrein.protocol.handoff import Metrics, load_schema, schema_errors
from tightrein.protocol.http import Transport, UrllibTransport
from tightrein.protocol.naming import format_iso, parse_duration
from tightrein.protocol.raw import RawDir, raw_dir

if TYPE_CHECKING:
    from tightrein.protocol.runtime import Runtime

SOURCE = "collect.access_log"
SOURCE_NAME = "access_log"
BASELINE = "baseline"
OUTPUT_SCHEMA = Path(__file__).parent / "project_sources" / "output.schema.json"


@dataclass(frozen=True)
class Fetched:
    requests: list[parse.Request]
    unparsed: int
    truncated: bool
    reached: datetime | None  # 读满上限时实际读到的最后时间
    notes: list[str]


def collect(runtime: Runtime, transport: Transport | None = None) -> SourceResult:
    """可能抛 SourceError(取不到数据、配置不对)，由调用方经 common.source.guarded 归类，位置不前进。"""
    module = runtime.setup.module(SOURCE)
    if not runtime.setup.enabled(SOURCE):
        return skipped(SOURCE, f"未启用：{module.reason or '接入清单中为 disabled'}")
    section = runtime.settings.section(SOURCE)
    now = runtime.clock.now()
    span = window.plan(runtime.conn, window.state_key(SOURCE), now, parse_duration(section["lookback"]))
    if module.status is ModuleStatus.CUSTOM:
        fetched = _from_script(runtime, section, module.script or "", list(module.secrets), span)
    elif module.method:
        fetched = _from_platform(runtime, section, module.method, transport or UrllibTransport(), span)
    else:
        raise SourceMisconfigured("setup.json 中 collect.access_log 没有写 method(日志平台方法，如 loki)")
    return _compare(runtime, section, span, fetched)


def _compare(runtime: Runtime, section: dict[str, Any], span: window.Window, fetched: Fetched) -> SourceResult:
    current = stats.summarize(fetched.requests)
    previous = (span.previous or {}).get(BASELINE) or {}
    baseline = {endpoint: stats.EndpointStats.from_json(item) for endpoint, item in previous.items()}
    found = stats.compare(current, baseline, min_requests=int(section["minRequests"]),
                          latency_ratio=float(section["latencyRatio"]),
                          error_rate_delta=float(section["errorRateDelta"]))
    factory = factory_for(runtime, SOURCE)
    commit = releases(runtime.conn)(span.until)
    signals = [factory.create(
        check_type=item.check_type, location=item.endpoint, message=f"{item.endpoint} 退化：{item.describe()}",
        occurred_at=span.until, commit=commit,
        evidence={"sourceName": SOURCE_NAME,
                  "window": {"since": format_iso(span.since), "until": format_iso(span.until)},
                  "current": item.current.to_json(), "baseline": item.baseline.to_json()})
        for item in found]
    notes = list(fetched.notes)
    if fetched.truncated:
        notes.append("访问日志超过条数上限，本次只按读到的部分统计，其余下次接着读")
    if fetched.unparsed:
        notes.append(f"{fetched.unparsed} 行访问日志无法解析，检查 controls.\"{SOURCE}\" 的 fields 或 pattern")
    if not baseline:
        notes.append("还没有基线，本次统计作为基线")
    merged = stats.update(baseline, current, float(section["baselineWeight"]))
    state = {span.key: span.advance(
        reached=fetched.reached, extra={BASELINE: {endpoint: item.to_json() for endpoint, item in merged.items()}})}
    produced = {"requests": len(fetched.requests), "endpoints": len(current), "unparsedLines": fetched.unparsed,
                "signals": len(signals)}
    return SourceResult(SOURCE, SourceStatus.DONE, signals, len(fetched.requests),
                        (format_iso(span.since), format_iso(span.until)), None, state, Metrics(produced=produced),
                        coverage=[SOURCE_NAME] if fetched.requests else [], notes=notes)


def _from_platform(runtime: Runtime, section: dict[str, Any], method: str, transport: Transport,
                   span: window.Window) -> Fetched:
    read = fetch(runtime, source=SOURCE, method=method, transport=transport, since=span.since, until=span.until)
    requests, unparsed = parse.parse(read.lines(), section["fields"], section.get("pattern"))
    notes = [note for note in [span.gap(read.oldest_available)] if note]
    return Fetched(requests, unparsed, read.truncated, read.last_time() if read.truncated else None, notes)


def _from_script(runtime: Runtime, section: dict[str, Any], script: str, secret_names: list[str],
                 span: window.Window) -> Fetched:
    document = {"window": {"since": format_iso(span.since), "until": format_iso(span.until)},
                "limit": int(section["limit"]), "workspace": str(runtime.workspace.root.absolute())}
    stdout = scripts.run(name=SOURCE_NAME, command=scripts.command_for(script), document=document,
                         workspace=runtime.workspace.root, runner=runtime.runner, environ=runtime.environ,
                         secrets=runtime.secrets, secret_names=secret_names, redactor=runtime.redactor,
                         raw=RawDir(raw_dir(runtime.workspace, runtime.run, SOURCE)),
                         timeout_s=parse_duration(section["scriptTimeout"]))
    try:
        output = json.loads(stdout)
    except ValueError as error:
        raise SourceInvalid("取数脚本的标准输出不是 JSON") from error
    errors = schema_errors(output, load_schema(OUTPUT_SCHEMA))
    if errors:
        raise SourceInvalid("取数脚本的输出不符合 project_sources/output.schema.json：" + "；".join(errors[:5]))
    if "requests" in output:
        found = [parse.request_of(item) for item in output["requests"]]
        requests = [item for item in found if item is not None]
        unparsed = len(found) - len(requests)
    else:
        requests, unparsed = parse.parse(output["lines"], section["fields"], section.get("pattern"))
    return Fetched(requests, unparsed, bool(output["truncated"]), None, list(output.get("notes") or []))
