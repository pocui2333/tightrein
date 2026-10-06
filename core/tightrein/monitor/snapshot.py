"""watch 的状态快照：只读数据库与工作区文件，不调用模型、不写任何记录。

- 运行：最近一次 loop 运行；各步骤取自它在事件日志中的 gate 事件(执行或跳过)，后一步开始即前一步结束。
- 正在进行：进行中的 Issue 与它的修复进度(data/fixes/<编号>/progress.json)；正在执行的模型调用是运行目录
  raw/runner/<角色>-<对象>/ 中 started.json 比 result.json 新的(同一角色可多轮调用)。
- 交接文件：修复目录、Issue 目录与每日汇总中最近修改的交接文档，结论取「结论」小节的第一行。
- 事件：模型调用(invoke_agent)与各阶段的判定(gate，loop 自身的除外)；用量为事件中记录的输入与输出 token 之和。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any

from tightrein.domain.clock import parse_iso
from tightrein.domain.enums import IssueStatus, RunStage, RunStatus
from tightrein.domain.run import Run
from tightrein.observability import events as event_log
from tightrein.orchestrator.rules import STEPS
from tightrein.pipeline.fix.steps import progress as fix_progress
from tightrein.store.files import documents
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store import locks
from tightrein.domain.handoff import types
from tightrein.store.files import handoff_files, markdown
from tightrein.store.repos import handoffs, issues, runs

LOOP_STEPS = ("recovery", *(step.name for step in STEPS))
EXECUTE = "执行"
DOCUMENT_LIMIT = 5
DAILY_REPORTS = "daily-*.md"  # 各运行的 run-*.md 是纯文本摘要，不是交接文档
EVENT_LIMIT = 8
STARTED_FILE, RESULT_FILE = "started.json", "result.json"


@dataclass(frozen=True)
class StepState:
    name: str
    state: str  # done、running、skipped、waiting、failed、interrupted
    started_at: datetime | None = None
    duration: timedelta | None = None
    note: str = ""
    tokens: int = 0  # 这一步模型调用的输入与输出 token 之和
    model: str | None = None  # 这一步最后一次模型调用的工具与模型；没有调用时为空(本地程序)


@dataclass(frozen=True)
class AgentCall:
    role: str
    subject: str
    tool: str
    model: str | None
    effort: str | None
    started_at: datetime


@dataclass(frozen=True)
class FixStep:
    label: str
    state: str
    note: str = ""


@dataclass(frozen=True)
class FixCall:
    """本次运行中这个 Issue 已结束的一次模型调用。"""
    role: str
    agent: str
    result: str
    duration_ms: int | None
    tokens: int | None


@dataclass(frozen=True)
class ActiveIssue:
    id: str
    title: str
    severity: str
    treatment: str | None
    lane: str | None
    steps: tuple[FixStep, ...]
    calls: tuple[FixCall, ...] = ()


@dataclass(frozen=True)
class DocumentLine:
    modified_at: datetime
    kind: str
    path: str
    conclusion: str


@dataclass(frozen=True)
class EventLine:
    at: datetime
    role: str
    agent: str
    result: str
    duration_ms: int | None
    tokens: int | None
    note: str


@dataclass(frozen=True)
class Snapshot:
    workspace: str
    now: datetime
    loop: Run | None
    steps: tuple[StepState, ...]
    run_tokens: int
    day_tokens: int
    issues: tuple[ActiveIssue, ...]
    agents: tuple[AgentCall, ...]
    documents: tuple[DocumentLine, ...]
    events: tuple[EventLine, ...]
    problems: list[str] = field(default_factory=list)  # 读取失败的文件，显示在界面底部
    pid: int | None = None  # 运行中的 loop 的持有进程(锁表)
    paused: str | None = None  # 暂停的原因

    @property
    def running(self) -> bool:
        return self.loop is not None and self.loop.status is RunStatus.RUNNING


def take(conn: sqlite3.Connection, layout: WorkspaceLayout, workspace: str, now: datetime,
         zone: tzinfo | None, gone: Collection[str] = (), paused: str | None = None) -> Snapshot:
    """gone 为状态仍是 running、但持有进程已不在的运行(orchestrator/recovery.interrupted)，按已中断显示；
    paused 为工作区或全局暂停的原因(orchestrator/pause.reason)。"""
    problems: list[str] = []
    loops = runs.find(conn, stage=RunStage.LOOP)
    loop = loops[-1] if loops else None
    if loop is not None and loop.id in gone:
        loop = replace(loop, status=RunStatus.INTERRUPTED)
    since = loop.started_at if loop is not None else now
    day_start = _local_midnight(now, zone)
    logged = _events(layout, min(since, day_start), now, problems)
    run_events = [item for item in logged if item.timestamp >= since]
    return Snapshot(
        workspace, now, loop, _steps(conn, layout, loop, run_events, now),
        _tokens(run_events), _tokens(item for item in logged if item.timestamp >= day_start),
        _issues(conn, layout, problems, run_events), _agents(conn, layout, since, gone), _documents(layout, problems),
        _event_lines(logged), problems, _pid(conn, loop), paused)


def _pid(conn: sqlite3.Connection, loop: Run | None) -> int | None:
    if loop is None or loop.status is not RunStatus.RUNNING:
        return None
    held = locks.TABLE.find(conn, run_id=loop.id)
    return held[0].holder_pid if held else None


def _local_midnight(now: datetime, zone: tzinfo | None) -> datetime:
    local = now.astimezone(zone)
    return datetime.combine(local.date(), datetime.min.time(), local.tzinfo).astimezone(timezone.utc)


def _events(layout: WorkspaceLayout, start: datetime, end: datetime,
            problems: list[str]) -> list[event_log.Event]:
    found: list[event_log.Event] = []
    day: date = start.astimezone(timezone.utc).date()
    while day <= end.astimezone(timezone.utc).date():
        path = layout.events_log(day)
        if path.is_file():
            try:
                found += [item for item in event_log.read(path) if item.timestamp >= start]
            except ValueError as error:
                problems.append(f"事件日志 {layout.relative(path)} 无法读取：{error}")
        day += timedelta(days=1)
    return sorted(found, key=lambda item: item.timestamp)


def _tokens(found: Iterable[event_log.Event]) -> int:
    return sum(_used(item) or 0 for item in found if item.operation == "invoke_agent")


def _used(call: event_log.Event) -> int | None:
    """一次模型调用的输入与输出 token 之和；两项都没有记录时为空。"""
    if call.input_tokens is None and call.output_tokens is None:
        return None
    return (call.input_tokens or 0) + (call.output_tokens or 0)


def _steps(conn: sqlite3.Connection, layout: WorkspaceLayout, loop: Run | None, found: Sequence[event_log.Event],
           now: datetime) -> tuple[StepState, ...]:
    """时间窗口取自 gate 事件(后一步开始即前一步结束)，花费与模型取窗口内的模型调用；结束的运行另以运行摘要中各步骤
    的实际结果与异常为准。没有 gate、但后面的步骤已开始的，是本次没有选中的步骤，按跳过显示。"""
    if loop is None:
        return ()
    gates = [item for item in found if item.run_id == loop.id and item.operation == "gate"
             and "step" in item.attributes]
    seen = {item.attributes["step"]: index for index, item in enumerate(gates)}
    calls = [item for item in found if item.operation == "invoke_agent"]
    finished = loop.status is not RunStatus.RUNNING
    summary = _summary(conn, layout, loop) if finished else None
    reached = max((LOOP_STEPS.index(name) for name in seen if name in LOOP_STEPS), default=-1)
    states = []
    for position, name in enumerate(LOOP_STEPS):
        index = seen.get(name)
        if index is None:
            states.append(StepState(name, "skipped" if position < reached else "waiting"))
            continue
        gate = gates[index]
        if gate.decision != EXECUTE:
            states.append(StepState(name, "skipped", gate.timestamp, note=gate.reason or ""))
            continue
        following = gates[index + 1].timestamp if index + 1 < len(gates) else None
        if following is not None:
            state = "done"
        elif not finished:
            state = "running"
        else:
            state = "interrupted" if loop.status is RunStatus.INTERRUPTED else "failed"
        end = following or loop.ended_at or (_last_activity(found, gate.timestamp, now) if finished else now)
        window = [item for item in calls if gate.timestamp <= item.timestamp < end]
        note = gate.reason or ""
        if summary is not None and name in summary.states:
            state = summary.states[name]
            note = summary.anomalies.get(name, note)
        states.append(StepState(name, state, gate.timestamp, end - gate.timestamp, note,
                                _tokens(window), _model(window[-1]) if window else None))
    return tuple(states)


def _last_activity(found: Sequence[event_log.Event], start: datetime, now: datetime) -> datetime:
    """被强行结束的运行没有结束时间：取这一步开始后最后一个事件的结束时刻作为它停下的时刻。"""
    ends = [item.timestamp + timedelta(milliseconds=item.duration_ms or 0) for item in found if item.timestamp >= start]
    return min(max(ends, default=start), now)


@dataclass(frozen=True)
class _Summary:
    states: dict[str, str]
    anomalies: dict[str, str]


def _summary(conn: sqlite3.Connection, layout: WorkspaceLayout, loop: Run) -> _Summary | None:
    """结束的运行的摘要(loop 交接文档的 outputs)：各步骤的结果与异常说明；没有摘要时为空。"""
    records = handoffs.for_run(conn, loop.id)
    if not records:
        return None
    try:
        outputs = handoff_files.read(layout.root / records[-1].path)["outputs"]
        states = {step["name"]: ("skipped" if not step["executed"] else
                                 "failed" if step.get("status") == RunStatus.FAILED.value else "done")
                  for step in outputs["steps"]}
    except (OSError, ValueError, KeyError):
        return None
    anomalies = {str(item.get("source")): str(item.get("reason", "")) for item in outputs.get("anomalies", [])}
    return _Summary(states, anomalies)


def _model(call: event_log.Event) -> str:
    effort = call.attributes.get("effort")
    text = " ".join(part for part in (call.agent, call.model) if part)
    return f"{text} ({effort})" if effort else text


def _issues(conn: sqlite3.Connection, layout: WorkspaceLayout, problems: list[str],
            found: Sequence[event_log.Event] = ()) -> tuple[ActiveIssue, ...]:
    """进行中的 Issue、修复进度与本次运行中它已结束的模型调用(found 为本次运行的事件)。"""
    active = []
    for record in issues.find(conn, status=IssueStatus.IN_PROGRESS):
        issue = record.issue
        path = layout.fixes_dir(issue.id) / fix_progress.FILE
        data: dict[str, Any] = {}
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                problems.append(f"修复进度 {layout.relative(path)} 无法读取：{error}")
        steps = tuple(FixStep(fix_progress.STEPS.get(int(key), key), value["state"], value.get("note", ""))
                      for key, value in data.get("steps", {}).items())
        calls = tuple(FixCall(str(item.attributes.get("role", "")), _model(item), item.status or "", item.duration_ms,
                              _used(item))
                      for item in found if item.operation == "invoke_agent"
                      and str(item.attributes.get("subjectId", "")) == issue.id)
        active.append(ActiveIssue(issue.id, issue.title, issue.severity.value,
                                  issue.treatment.label if issue.treatment else None, data.get("lane"), steps, calls))
    return tuple(active)


def _agents(conn: sqlite3.Connection, layout: WorkspaceLayout, since: datetime,
            gone: Collection[str]) -> tuple[AgentCall, ...]:
    found = []
    for run in runs.find(conn, status=RunStatus.RUNNING):
        if run.started_at < since or run.id in gone:
            continue
        directory = layout.run_dir(run.id) / "raw" / "runner"
        for marker in directory.glob(f"*/{STARTED_FILE}") if directory.is_dir() else ():
            result = marker.parent / RESULT_FILE
            if result.exists() and result.stat().st_mtime >= marker.stat().st_mtime:
                continue
            try:
                data = json.loads(marker.read_text(encoding="utf-8"))
                started = parse_iso(data["startedAt"])
            except (OSError, ValueError, KeyError):
                continue  # 执行器正在写入，下一次刷新再读
            found.append(AgentCall(data.get("role", marker.parent.name), str(data.get("subject", "")),
                                   data.get("tool", ""), data.get("model"), data.get("effort"), started))
    return tuple(sorted(found, key=lambda item: item.started_at))


def _documents(layout: WorkspaceLayout, problems: list[str]) -> tuple[DocumentLine, ...]:
    candidates = [*layout.data_dir().glob("fixes/*/*.md"), *layout.issues_dir().glob("*.md"),
                  *layout.reports_dir().glob(DAILY_REPORTS)]
    handoff = [path for path in candidates if path.is_file() and _is_handoff(path)]
    newest = sorted(handoff, key=lambda path: path.stat().st_mtime, reverse=True)[:DOCUMENT_LIMIT]
    found = []
    for path in newest:
        modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        try:
            parsed = documents.read(path)
        except (OSError, documents.DocumentError) as error:
            problems.append(f"交接文件 {layout.relative(path)} 无法读取：{error}")
            continue
        conclusion = next((line.strip() for line in parsed.conclusion.splitlines() if line.strip()), "")
        found.append(DocumentLine(modified, str(parsed.header.get("kind", "")), layout.relative(path), conclusion))
    return tuple(found)


def _is_handoff(path: Path) -> bool:
    """头信息的 kind 是登记过的交接文档类型；修复报告(type: fix-report)、PR 正文等其他文件不列出。"""
    try:
        header = markdown.parse(path.read_text(encoding="utf-8"), str(path)).frontmatter
    except (OSError, UnicodeDecodeError, markdown.FrontmatterError):
        return False
    return str(header.get("kind")) in types.TYPES


def _event_lines(found: Sequence[event_log.Event]) -> tuple[EventLine, ...]:
    lines = []
    for item in found:
        if item.operation == "invoke_agent":
            agent = " ".join(part for part in (item.agent, item.model) if part)
            lines.append(EventLine(item.timestamp, str(item.attributes.get("role", "")), agent,
                                   item.status or "", item.duration_ms, _used(item),
                                   str(item.attributes.get("subjectId", ""))))
        elif item.operation == "gate" and item.stage not in (None, RunStage.LOOP.value) and item.decision:
            lines.append(EventLine(item.timestamp, item.stage or "", "", item.decision, None, None,
                                   (item.reason or "").split("；", 1)[0]))
    return tuple(lines[-EVENT_LIMIT:][::-1])
