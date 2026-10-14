"""status 与 watch 的取数：只读 store 与工作区文件，不调用模型、不写任何记录，随时可开可关，不影响运行。

读的东西只有这些：
- store：runs、issues、problems、counters(每个 Issue 的 token、依赖熔断)、state(额度 `quota`、漏跑 `schedule.missed`、
  知识库待确认 `knowledge.pending`、`knowledge.stale`)；
- 文件：各对象目录的 `*-handoff.json`(经 protocol.recovery.checkpoints)、给人看的 `90-…md`(只看是否存在与修改时间)、
  agents 写的 `<序号>-<调用点>-started.json`(endedAt 为空即调用正在进行)、运行目录的 events.jsonl、
  接入清单 setup.json、运行控制 control.json、复盘记录的文件名与状态行；
- 进行中的运行若心跳失效或(同主机)开始它的进程已不在，显示为中断并给出接管命令(启动恢复会接管)。

时间一律按 UTC 取，显示时由渲染换成本地时区。各阶段写进 handoff 必填事实、给这里读的键集中在 FACT_* 常量。
"""

from __future__ import annotations

import json
import os
import re
import socket
import sqlite3
from collections import Counter as Tally
from collections.abc import Callable, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any

from tightrein.onboard import setup as setup_file
from tightrein.onboard.setup import Setup, SetupInvalid
from tightrein.protocol import recovery, schedule
from tightrein.protocol.handoff import Handoff, Status, Tokens
from tightrein.protocol.limits import no_progress
from tightrein.protocol.naming import (
    STAGES,
    STEP_SEQUENCE,
    Clock,
    format_iso,
    parse_duration,
    parse_iso,
    run_started,
)
from tightrein.protocol.records import versions
from tightrein.protocol.recovery import Checkpoint
from tightrein.protocol.resources import WEEKLY_WINDOWS, IssueBudget, Quota
from tightrein.settings.load import MissingSetting, Settings
from tightrein.store.files.layout import ToolLayout, WorkspaceLayout
from tightrein.store.tables import counters, issues, runs, state
from tightrein.store.tables.issues import Issue
from tightrein.store.tables.runs import Run

# 各阶段写进必填事实、给 status 与 watch 读的键
FACT_SKIPPED = "skipped"            # 跳过的原因(字符串)；有值即这一步跳过
FACT_AUTO = "auto"                  # 定案：True 为自动确认，False 为人工确认
FACT_BLOCKERS = "blockers"          # 审查：[{location, kind, summary}]
FACT_DIFF_HASH = "diffHash"         # 编码：提交无关的 diff 哈希
FACT_LINES_ADDED = "linesAdded"
FACT_LINES_DELETED = "linesDeleted"
FACT_READ = "read"                  # 采集来源读到的条数
FACT_DEDUP = ("new", "merged", "muted", "regressed")  # 去重结果(列表或数目)
PRODUCED_SIGNALS = "signals"        # metrics.produced 中采集产出的信号数
# problems.extra 与 issues.extra 中给这里读的键
EXTRA_VERDICT = "verdict"           # 评估判定：confirmed、conditional、refuted、insufficient
EXTRA_SEVERITY = "severity"
EXTRA_ACCEPT_UNTIL = "acceptUntil"  # 验收观察期结束的时间(ISO UTC)
# state 表
STATE_MISSED = schedule.MISSED_KEY
STATE_KNOWLEDGE_PENDING = "knowledge.pending"
STATE_KNOWLEDGE_STALE = "knowledge.stale"

VERDICTS = ("confirmed", "conditional", "refuted", "insufficient")
SEVERITIES = ("P0", "P1", "P2", "P3")
ACTIVE_ISSUES = ("implementing", "releasing")
CLOSED_ISSUES = ("done", "cancelled")
WAITING_PROBLEMS = ("new", "regressed")
COLLECT_SOURCES = tuple(key for key in setup_file.MODULES if key.startswith("collect."))
LIVE_RUNS = ("running", "paused", "finishing")
EVENT_LINES = 40  # 取最近的条数；显示几条由终端高度定
EXTRA_GITHUB = "github"  # issues.extra 中 GitHub 镜像：{"number": 41, …}
# 问题评估的各格(assess/assess.py 的调用点)；反证与取证同一格
ASSESS_FLOW = ("assess.dedup", "assess.triage", "assess.confirm", "assess.issue")
ASSESS_SLOTS = {"assess.refute": "assess.confirm"}
CODE_POINT, REVIEW_POINT = "implement.code", "implement.review"
_RETRO_FILE = re.compile(r"^(\d{4})-(P[0-3])-.+\.md$")
_RETRO_STATUS = re.compile(r"^status:\s*(\S+)", re.MULTILINE)
_RETRO_OPEN = "open"
_ROUND_IN_PATH = re.compile(r"\.r(\d+)-")
_CALL_STATUS = re.compile(r"^[^\s/]+/[^\s：]+：(\w+)")
_STARTED = "*-started.json"
_NOISE = "子进程环境去掉了："  # agents/call.py 每次调用都记的例行动作，watch 不列
_STEP_RESULTS = {"passed": "ok", "failed": "bad", "pending": "gate"}  # 各步交接写的事件「<结果>：<摘要>」
_SOURCE_STEP = {"done": "done", "active": "active", "waiting": "waiting", "skipped": "skipped", "failed": "failed",
                "off": "skipped"}
_TOKEN_FIELDS = frozenset({"input", "output", "cache_read", "cache_write"})
_WAITING_SQL = ", ".join(f"'{name}'" for name in WAITING_PROBLEMS)
_MANUAL_SQL = "json_extract(extra, '$.assess.manual')"
_MERGED_SQL = "json_extract(extra, '$.mergedInto')"


# 类型：共用


@dataclass(frozen=True)
class Source:
    """取数要用的依赖，由命令壳组装；测试换成临时工作区与固定时钟。"""

    tool: ToolLayout
    layout: WorkspaceLayout
    conn: sqlite3.Connection
    settings: Settings
    clock: Clock
    zone: tzinfo | None = None  # 「今天」「本周」按哪个时区；None 为本机时区
    host: str = field(default_factory=socket.gethostname)
    alive: Callable[[int], bool] = field(default=lambda pid: _alive(pid))


@dataclass(frozen=True)
class RunInfo:
    id: str
    trigger: str | None
    stage: str
    status: str  # running、paused、finishing、interrupted、done、failed、skipped
    started_at: datetime
    ended_at: datetime | None
    heartbeat_at: datetime | None
    gone_reason: str | None = None  # 判为中断的原因：进程已不在、心跳失效


@dataclass(frozen=True)
class ControlInfo:
    mode: str  # normal、paused、stopped
    since: datetime | None = None
    note: str | None = None


@dataclass(frozen=True)
class QuotaInfo:
    tool: str
    five_hour: float | None
    five_hour_resets_at: datetime | None
    weekly: float | None
    weekly_resets_at: datetime | None
    models: tuple[tuple[str, float], ...]  # 读得到时另列的 opus、sonnet
    reserve_five_hour: float
    reserve_weekly: float
    rejected: bool


@dataclass(frozen=True)
class BreakerInfo:
    dependency: str
    failures: int
    reopens_at: datetime


# 类型：status


@dataclass(frozen=True)
class WaitingItem:
    id: str
    title: str
    severity: str | None
    kind: str  # review(待审核)、failed(出问题)
    point: str | None
    round: int | None
    since: datetime
    summary: str
    document: str  # 相对工作区的路径
    command: str
    github: int | None = None  # 镜像到 GitHub 的编号


@dataclass(frozen=True)
class ActiveItem:
    id: str
    title: str
    severity: str | None
    point: str | None
    round: int | None
    step_started: datetime | None
    step_limit_s: float | None
    tokens_used: float
    tokens_limit: float
    files: int | None
    lines: int | None
    added: int | None
    deleted: int | None


@dataclass(frozen=True)
class Stock:
    sources_enabled: int
    sources_total: int
    last_collect: datetime | None
    problems: dict[str, int]  # 按状态
    assess_pending: int
    verdicts: dict[str, int]
    severities: dict[str, int]
    queued: tuple[str, ...]  # 排队的 Issue，按严重度与编号
    held: tuple[str, ...]  # 手动接管
    prs_open: int
    prs_ci: int
    prs_to_merge: int
    to_merge_since: datetime | None
    awaiting_deploy: int
    accept_until: datetime | None
    retro_open: dict[str, int]  # 按评级
    retro_new_week: int
    knowledge_pending: int
    knowledge_stale: int


@dataclass(frozen=True)
class Totals:
    delivered: int = 0
    failed: int = 0
    signals: int = 0
    problems: int = 0
    issues: int = 0
    approvals_auto: int = 0
    approvals_manual: int = 0
    tokens: Tokens = field(default_factory=Tokens)
    tokens_by_model: dict[str, int] = field(default_factory=dict)  # 「工具/模型」→ 输入加输出
    cost_usd: float = 0.0
    avg_duration_s: float | None = None
    avg_tokens: float | None = None
    first_pass: float | None = None

    @property
    def total_tokens(self) -> int:
        return self.tokens.input + self.tokens.output

    @property
    def cache_hit(self) -> float | None:
        return self.tokens.cache_read / self.tokens.input if self.tokens.input else None


@dataclass(frozen=True)
class Health:
    blind_spots: tuple[str, ...]
    stray_worktrees: int
    missed: int
    stale_runs: tuple[str, ...]
    breakers: tuple[BreakerInfo, ...]
    disk_bytes: int
    purge_runs: int


@dataclass(frozen=True)
class StatusSnapshot:
    project: str
    commit: str | None
    now: datetime
    setup_missing: tuple[str, ...]  # 未就绪时缺哪项；空为就绪
    control: ControlInfo
    in_window: bool
    next_run: datetime | None
    config_issues: tuple[str, ...]
    config_pending: bool  # 运行开始后改过配置：下次运行才生效
    current: RunInfo | None
    last: RunInfo | None
    quotas: tuple[QuotaInfo, ...]
    reserve_reasons: tuple[str, ...]
    halted_until: datetime | None
    waiting: tuple[WaitingItem, ...]
    active: tuple[ActiveItem, ...]
    stock: Stock
    today: Totals
    week: Totals
    health: Health


# 类型：watch


@dataclass(frozen=True)
class CallInfo:
    point: str
    round: int | None
    tool: str
    model: str
    effort: str | None
    started_at: datetime
    turns: int | None  # agents 在标记中写了进度时才有
    tokens: Tokens | None


@dataclass(frozen=True)
class StepMark:
    point: str
    state: str  # done、active、skipped、waiting、failed、gate
    duration_s: float | None
    note: str | None  # 跳过的原因；定案为 auto 或 manual
    round: int | None
    rounds_limit: int | None


@dataclass(frozen=True)
class ActiveRow:
    """watch「进行中」的一行：并行实施的每个 Issue、正在评估的问题、运行级的采集与复盘各一行。"""

    id: str  # 0025、P-0038；运行级的为阶段控制键(collect、retro)
    kind: str  # issue、problem、collect、run
    github: int | None  # 镜像到 GitHub 的编号
    title: str
    severity: str | None
    steps: tuple[StepMark, ...]  # 进度条的各格；采集为启用的各来源与去重
    point: str | None  # 正在做的调用点(可带子步骤，如 implement.review.deep)
    step: str | None  # point 所在的那一格
    round: int | None
    action: str  # model、tests、ci、program、idle
    call: CallInfo | None
    last_failure: str | None  # 这一步上一次调用没成功的结果(timeout 等)，模型重试时不像卡死
    stalled: bool  # 与上一轮比没有进展(protocol.limits.no_progress)
    step_started: datetime | None
    step_limit_s: float | None
    tokens_used: float | None  # 只有 Issue 有 token 预算
    tokens_limit: float | None


@dataclass(frozen=True)
class SourceMark:
    key: str
    state: str  # done、active、skipped、off、waiting、failed
    read: int | None
    produced: int | None
    duration_s: float | None
    reason: str | None


@dataclass(frozen=True)
class CollectCard:
    started_at: datetime | None
    sources: tuple[SourceMark, ...]
    dedup: tuple[int, int, int, int] | None  # 新问题、累计到已有、抑制、回归
    breaker: BreakerInfo | None


@dataclass(frozen=True)
class EventLine:
    at: datetime
    subject: str | None
    point: str
    round: int | None
    mark: str  # ok、bad、warn、info
    summary: str


@dataclass(frozen=True)
class QueueInfo:
    todo: tuple[str, ...]  # 待修的 Issue，按严重度与编号
    assess_pending: int  # 待评估的问题(转人工的不算)
    ci: int  # 发布中：提 PR、等 CI
    to_merge: int  # 发布中：等合并
    deploying: int  # 发布中：等部署
    accepting: int  # 验收观察中
    retro_after: bool  # 本次运行结束前还要复盘


@dataclass(frozen=True)
class WatchSnapshot:
    project: str
    now: datetime
    run: RunInfo | None
    stage: str | None  # 运行正在做的阶段(最近一条事件所在的阶段)；运行已结束为 None
    control: ControlInfo
    quotas: tuple[QuotaInfo, ...]
    active: tuple[ActiveRow, ...]
    waiting: tuple[WaitingItem, ...]  # 等你处理的 Issue(待审核、出问题)
    manual: tuple[str, ...]  # 评估转人工、等你看的问题
    collect: CollectCard
    events: tuple[EventLine, ...]  # 新的在前
    queue: QueueInfo


# 公共函数


def status_snapshot(source: Source) -> StatusSnapshot:
    now = source.clock.now()
    today, week = _day_start(now, source.zone), _week_start(now, source.zone)
    setup, setup_missing = _setup(source.layout)
    current, last, stale = _runs(source)
    all_issues = issues.find(source.conn)
    checkpoints = _recent_checkpoints(source.layout, week)
    breakers = _breakers(source)
    return StatusSnapshot(
        project=source.layout.project,
        commit=_commit(source),
        now=now,
        setup_missing=setup_missing,
        control=_control(source.layout),
        in_window=schedule.in_window(source.settings, now, source.zone),
        next_run=schedule.next_run(source.settings, now, source.zone),
        config_issues=tuple(source.settings.issues()),
        config_pending=_config_pending(source, current),
        current=current,
        last=last,
        quotas=_quotas(source),
        reserve_reasons=tuple(Quota(source.conn, source.clock, source.settings).reserve_reasons()),
        halted_until=Quota(source.conn, source.clock, source.settings).halted_until(),
        waiting=tuple(_waiting(source, all_issues)),
        active=tuple(_active(source, all_issues)),
        stock=_stock(source, setup, all_issues, week),
        today=_totals(source, [item for item in checkpoints if _created(item) >= today], today),
        week=_totals(source, checkpoints, week),
        health=Health(
            blind_spots=tuple(item.key for item in setup.blind_spots()) if setup else (),
            stray_worktrees=_stray_worktrees(source.layout, all_issues),
            missed=int(state.get(source.conn, STATE_MISSED) or 0),
            stale_runs=stale,
            breakers=breakers,
            disk_bytes=_disk(source.layout.data_dir),
            purge_runs=_purge_runs(source, now),
        ),
    )


def watch_snapshot(source: Source) -> WatchSnapshot:
    """每秒左右取一次：只读 store、进行中对象目录里的交接与 started 标记、本次运行的 events.jsonl。"""
    now = source.clock.now()
    current, last, _ = _runs(source)
    run = current or last
    events = _events(source.layout, run.id) if run is not None else []
    stage = _current_stage(run, events)
    all_issues = issues.find(source.conn)
    waiting = _waiting(source, all_issues)
    collect = _collect_card(source, run, events, stage)
    run_row = _run_row(source, run, stage, collect) if run is not None and stage is not None else None
    active = ([run_row] if run_row else []) + _issue_rows(source, all_issues, {item.id for item in waiting})
    active += _problem_rows(source, events) if stage is not None else []
    return WatchSnapshot(
        project=source.layout.project,
        now=now,
        run=run,
        stage=stage,
        control=_control(source.layout),
        quotas=_quotas(source),
        active=tuple(active),
        waiting=tuple(waiting),
        manual=_manual_problems(source.conn),
        collect=collect,
        events=tuple(_event_lines(events)),
        queue=_queue(source, all_issues, run, stage),
    )


def point_of(stage: str | None, step: str | None) -> str | None:
    """issues 表的 stage、step 合成调用点：step 已是控制键时原样用。"""
    if not step:
        return stage
    return step if "." in step else f"{stage}.{step}" if stage else step


def stage_steps(stage: str) -> tuple[str, ...]:
    """一个阶段的各小步骤(按文件序号排)；采集的来源与去重也在其中。"""
    return tuple(point for point in STEP_SEQUENCE if point.startswith(stage + "."))


# 内部：运行与控制


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _runs(source: Source) -> tuple[RunInfo | None, RunInfo | None, tuple[str, ...]]:
    """(当前运行、上一次结束的运行、判为中断的运行)。进程已不在或心跳失效的进行中运行按中断显示。"""
    stale_s = source.settings.duration("limits.lock.stale")
    now = source.clock.now()
    current: RunInfo | None = None
    gone: list[RunInfo] = []
    for run in runs.running(source.conn):
        reason = _gone(run, source, now, stale_s)
        info = _run_info(run, "interrupted" if reason else _running_state(source.layout), reason)
        if reason:
            gone.append(info)
        elif current is None or info.started_at > current.started_at:
            current = info
    row = source.conn.execute(
        "SELECT id FROM runs WHERE status != 'running' ORDER BY started_at DESC, id DESC LIMIT 1").fetchone()
    finished = runs.get(source.conn, row[0]) if row else None
    last = _run_info(finished, finished.status, None) if finished else None
    if current is None and gone:
        current = max(gone, key=lambda item: item.started_at)
    return current, last, tuple(item.id for item in gone)


def _gone(run: Run, source: Source, now: datetime, stale_s: float) -> str | None:
    if run.holder_host == source.host and run.holder_pid is not None and not source.alive(run.holder_pid):
        return "process"
    beat = run.heartbeat_at or run.started_at
    return "heartbeat" if (now - beat).total_seconds() > stale_s else None


def _running_state(layout: WorkspaceLayout) -> str:
    control = recovery.control(layout)
    return "paused" if control is not None and control.mode is recovery.Mode.PAUSED else "running"


def _run_info(run: Run, status: str, gone: str | None) -> RunInfo:
    if status == "running" and run.stage == "retro":
        status = "finishing"
    return RunInfo(run.id, run.trigger, run.stage, status, run.started_at, run.ended_at, run.heartbeat_at, gone)


def _control(layout: WorkspaceLayout) -> ControlInfo:
    control = recovery.control(layout)
    if control is None:
        return ControlInfo("normal")
    return ControlInfo(control.mode.value, parse_iso(control.since), control.note)


def _commit(source: Source) -> str | None:
    commit = versions(source.tool.root, source.settings.hash, prompt_hash=None, tool=None, tool_version=None,
                      model=None).tightrein
    return commit[:7] if commit else None


def _setup(layout: WorkspaceLayout) -> tuple[Setup | None, tuple[str, ...]]:
    try:
        return setup_file.load(layout), ()
    except SetupInvalid as error:
        return None, tuple(error.issues)


def _config_pending(source: Source, current: RunInfo | None) -> bool:
    if current is None:
        return False
    paths = [source.layout.settings, source.tool.controls]
    started = current.started_at.timestamp()
    return any(path.is_file() and path.stat().st_mtime > started for path in paths)


def _day_start(now: datetime, zone: tzinfo | None) -> datetime:
    local = now.astimezone(zone)
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def _week_start(now: datetime, zone: tzinfo | None) -> datetime:
    day = _day_start(now, zone)
    return day - timedelta(days=day.weekday())


# 内部：额度与熔断


def _quotas(source: Source) -> tuple[QuotaInfo, ...]:
    found = Quota(source.conn, source.clock, source.settings).current()
    tools = sorted({item.tool for item in found} | {"claude", "agy"}, key=lambda name: (name != "claude", name))
    return tuple(_quota(source, tool, [item for item in found if item.tool == tool]) for tool in tools)


def _quota_for(source: Source, tool: str) -> QuotaInfo:
    found = Quota(source.conn, source.clock, source.settings).current()
    return _quota(source, tool, [item for item in found if item.tool == tool])


def _quota(source: Source, tool: str, limits: Sequence[Any]) -> QuotaInfo:
    windows = {item.window: item for item in limits}
    five, weekly = windows.get("five_hour"), windows.get("weekly")
    models = tuple((name, windows[name].used_ratio) for name in sorted(WEEKLY_WINDOWS - {"weekly"})
                   if name in windows and windows[name].used_ratio is not None)
    return QuotaInfo(
        tool=tool,
        five_hour=five.used_ratio if five else None,
        five_hour_resets_at=parse_iso(five.resets_at) if five and five.resets_at else None,
        weekly=weekly.used_ratio if weekly else None,
        weekly_resets_at=parse_iso(weekly.resets_at) if weekly and weekly.resets_at else None,
        models=models,
        reserve_five_hour=float(source.settings.get("resources.quota.reserveFiveHour")),
        reserve_weekly=float(source.settings.get("resources.quota.reserveWeekly")),
        rejected=any(item.status == "rejected" for item in limits),
    )


def _breakers(source: Source) -> tuple[BreakerInfo, ...]:
    """打开着的依赖熔断。键名照 protocol/limits.py 的 Breaker：breaker.dependency.<依赖>.failures/.opened。"""
    threshold = int(source.settings.get("limits.breaker.dependencyFailures"))
    pause = source.settings.duration("limits.breaker.pause")
    values = counters.find(source.conn, "breaker.dependency.")
    now = source.clock.now().timestamp()
    found = []
    for key, failures in values.items():
        if not key.endswith(".failures") or failures < threshold:
            continue
        dependency = key.removeprefix("breaker.dependency.").removesuffix(".failures")
        opened = values.get(f"breaker.dependency.{dependency}.opened", 0.0)
        if now - opened < pause:
            found.append(BreakerInfo(dependency, int(failures),
                                     datetime.fromtimestamp(opened + pause, UTC)))
    return tuple(sorted(found, key=lambda item: item.reopens_at))


# 内部：对象


def _checkpoints(layout: WorkspaceLayout, subject: str) -> list[Checkpoint]:
    return recovery.checkpoints(layout, subject)


def _created(item: Checkpoint) -> datetime:
    return parse_iso(item.handoff.created_at) if item.handoff.created_at else _mtime(item.path)


def _mtime(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, UTC)


def _relative(layout: WorkspaceLayout, path: Path) -> str:
    return str(path.relative_to(layout.root))


def _severity_order(issue: Issue) -> tuple[int, str]:
    rank = SEVERITIES.index(issue.severity) if issue.severity in SEVERITIES else len(SEVERITIES)
    return rank, issue.id


def _waiting(source: Source, all_issues: Sequence[Issue]) -> list[WaitingItem]:
    """待审核(停在关卡)与出问题(出问题文档比最后一份交接新)的 Issue；手动接管的不列。"""
    found = []
    for issue in all_issues:
        if issue.status in CLOSED_ISSUES or issue.held_by:
            continue
        item = _waiting_item(source.layout, issue)
        if item is not None:
            found.append(item)
    return sorted(found, key=lambda item: (item.kind != "failed", item.since))


def _waiting_item(layout: WorkspaceLayout, issue: Issue) -> WaitingItem | None:
    pending, failure = layout.human_document(issue.id, "pending"), layout.human_document(issue.id, "failure")
    review = issue.status == "needs_decision" or bool(issue.gate)
    if not review and not failure.is_file():
        return None
    checkpoints = _checkpoints(layout, issue.id)
    last = checkpoints[-1] if checkpoints else None
    if review:
        document, kind, command = pending, "review", f"tightrein approve {issue.id}"
    else:
        if last is not None and last.path.stat().st_mtime > failure.stat().st_mtime:
            return None  # 出问题之后又往前做了：文档留着备查，不再算等你处理
        document, kind, command = failure, "failed", f"tightrein show {issue.id}"
    since = _mtime(document) if document.is_file() else (_created(last) if last else _mtime(layout.issue_dir(issue.id)))
    return WaitingItem(
        id=issue.id,
        title=issue.title,
        severity=issue.severity,
        kind=kind,
        point=point_of(issue.stage, issue.step) or (last.handoff.point if last else None),
        round=issue.round,
        since=since,
        summary=last.handoff.summary if last else "",
        document=_relative(layout, document),
        command=command,
        github=_github(issue),
    )


def _active(source: Source, all_issues: Sequence[Issue]) -> list[ActiveItem]:
    budget = IssueBudget(source.conn, source.clock, source.settings)
    found = []
    for issue in all_issues:
        if issue.status not in ACTIVE_ISSUES or issue.held_by or issue.gate:
            continue
        if _waiting_item(source.layout, issue) is not None:
            continue
        checkpoints = _checkpoints(source.layout, issue.id)
        point = point_of(issue.stage, issue.step)
        call = _running(_markers(source.layout, issue.id))
        started = call.started_at if call else (_created(checkpoints[-1]) if checkpoints else None)
        changed = _changed(checkpoints)
        found.append(ActiveItem(
            id=issue.id,
            title=issue.title,
            severity=issue.severity,
            point=point,
            round=issue.round,
            step_started=started,
            step_limit_s=_limit_s(source.settings, point),
            tokens_used=budget.used(issue.id),
            tokens_limit=budget.limit,
            files=changed[0],
            lines=changed[1],
            added=changed[2],
            deleted=changed[3],
        ))
    return sorted(found, key=lambda item: item.step_started.timestamp() if item.step_started else 0.0, reverse=True)


def _changed(checkpoints: Sequence[Checkpoint]) -> tuple[int | None, int | None, int | None, int | None]:
    """最近一份记了改动量的交接：(文件数、行数、增、删)。"""
    for item in reversed(checkpoints):
        metrics = item.handoff.metrics
        if metrics.files_changed is not None:
            facts = item.handoff.facts
            return (metrics.files_changed, metrics.lines_changed, _int(facts.get(FACT_LINES_ADDED)),
                    _int(facts.get(FACT_LINES_DELETED)))
    return None, None, None, None


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, list):
        return len(value)
    return None


def _limit_s(settings: Settings, point: str | None) -> float | None:
    if point is None:
        return None
    try:
        return parse_duration(settings.control(point, "timeout"))
    except (MissingSetting, ValueError):
        return None


def _queued(all_issues: Sequence[Issue], exclude: str | None = None) -> tuple[str, ...]:
    waiting = [issue for issue in all_issues if issue.status == "todo" and not issue.held_by and issue.id != exclude]
    return tuple(issue.id for issue in sorted(waiting, key=_severity_order))


# 内部：各阶段存量


def _stock(source: Source, setup: Setup | None, all_issues: Sequence[Issue], week: datetime) -> Stock:
    conn = source.conn
    problems = {row[0]: int(row[1]) for row in conn.execute(
        "SELECT status, COUNT(*) FROM problems GROUP BY status").fetchall()}
    verdicts = _by_extra(conn, EXTRA_VERDICT)
    severities = _by_extra(conn, EXTRA_SEVERITY)
    releasing = [issue for issue in all_issues if issue.status == "releasing" and not issue.held_by]
    to_merge = [issue for issue in releasing if point_of(issue.stage, issue.step) == "release.merge"]
    accepting = [issue for issue in all_issues if issue.status == "accepting"]
    accept_until = [parse_iso(str(issue.extra[EXTRA_ACCEPT_UNTIL])) for issue in accepting
                    if issue.deploy and issue.extra.get(EXTRA_ACCEPT_UNTIL)]
    retro_open, retro_new = _retro(source.layout, week)
    last_collect = runs.latest(conn, "collect")
    return Stock(
        sources_enabled=sum(1 for key in COLLECT_SOURCES if setup and key in setup.modules and setup.enabled(key)),
        sources_total=len(COLLECT_SOURCES),
        last_collect=last_collect.started_at if last_collect else None,
        problems=problems,
        assess_pending=sum(problems.get(name, 0) for name in WAITING_PROBLEMS),
        verdicts=verdicts,
        severities={name: severities.get(name, 0) for name in SEVERITIES},
        queued=_queued(all_issues),
        held=tuple(issue.id for issue in all_issues if issue.held_by and issue.status not in CLOSED_ISSUES),
        prs_open=sum(1 for issue in releasing if issue.pr is not None),
        prs_ci=sum(1 for issue in releasing if point_of(issue.stage, issue.step) == "release.ci"),
        prs_to_merge=len(to_merge),
        to_merge_since=min((_last_time(source.layout, issue.id) for issue in to_merge), default=None),
        awaiting_deploy=sum(1 for issue in accepting if not issue.deploy),
        accept_until=min(accept_until, default=None),
        retro_open=retro_open,
        retro_new_week=retro_new,
        knowledge_pending=len(state.get(conn, STATE_KNOWLEDGE_PENDING) or []),
        knowledge_stale=len(state.get(conn, STATE_KNOWLEDGE_STALE) or []),
    )


def _by_extra(conn: sqlite3.Connection, name: str) -> dict[str, int]:
    """问题按 extra 中某个键的取值计数(评估写入的判定与严重度)，一条语句在库里算完。"""
    rows = conn.execute(
        "SELECT json_extract(extra, ?), COUNT(*) FROM problems WHERE json_extract(extra, ?) IS NOT NULL GROUP BY 1",
        (f"$.{name}", f"$.{name}")).fetchall()
    return {str(row[0]): int(row[1]) for row in rows}


def _last_time(layout: WorkspaceLayout, subject: str) -> datetime:
    checkpoints = _checkpoints(layout, subject)
    return _created(checkpoints[-1]) if checkpoints else _mtime(layout.subject_dir(subject))


def _retro(layout: WorkspaceLayout, week: datetime) -> tuple[dict[str, int], int]:
    """复盘记录：文件名带评级；状态行 `status: open` 为待看(没写的也算待看)。只读每个文件开头的 1 KB。"""
    opened: Tally[str] = Tally()
    new = 0
    if not layout.retro_dir.is_dir():
        return {}, 0
    for path in layout.retro_dir.iterdir():
        match = _RETRO_FILE.match(path.name)
        if match is None:
            continue
        with path.open(encoding="utf-8", errors="replace") as handle:
            head = handle.read(1024)
        status = _RETRO_STATUS.search(head)
        if status is None or status.group(1) == _RETRO_OPEN:
            opened[match.group(2)] += 1
        if _mtime(path) >= week:
            new += 1
    return {name: opened[name] for name in SEVERITIES if opened[name]}, new


# 内部：汇总


def _recent_checkpoints(layout: WorkspaceLayout, since: datetime) -> list[Checkpoint]:
    """本周以来的交接：只进修改时间在本周以来的对象目录(目录里加文件会更新目录的修改时间)。"""
    found: list[Checkpoint] = []
    cutoff = since.timestamp()
    for parent in (layout.issues_dir, layout.problems_dir, layout.runs_dir):
        if not parent.is_dir():
            continue
        for directory in parent.iterdir():
            if directory.is_dir() and directory.stat().st_mtime >= cutoff:
                found += [item for item in _checkpoints(layout, directory.name) if _created(item) >= since]
    return found


def _totals(source: Source, checkpoints: Sequence[Checkpoint], since: datetime) -> Totals:
    tokens = Tokens()
    by_model: Tally[str] = Tally()
    cost = 0.0
    per_issue: dict[str, list[Handoff]] = {}
    delivered: set[str] = set()
    failed = signals = auto = manual = 0
    for item in checkpoints:
        handoff = item.handoff
        metrics = handoff.metrics
        if metrics.tokens is not None:
            tokens.add(metrics.tokens)
            name = "/".join(part for part in (handoff.versions.tool, handoff.versions.model) if part) or "-"
            by_model[name] += metrics.tokens.input + metrics.tokens.output
        cost += metrics.cost_usd or 0.0
        failed += handoff.status is Status.FAILED
        signals += (metrics.produced or {}).get(PRODUCED_SIGNALS, 0) if handoff.point.startswith("collect.") else 0
        if handoff.point == "implement.approve" and isinstance(handoff.facts.get(FACT_AUTO), bool):
            auto += handoff.facts[FACT_AUTO]
            manual += not handoff.facts[FACT_AUTO]
        if not handoff.subject.startswith(("R-", "P-")):
            per_issue.setdefault(handoff.subject, []).append(handoff)
        if handoff.point == "implement.deliver" and handoff.status is Status.PASSED:
            delivered.add(handoff.subject)
    stamp = format_iso(since)
    weight = float(source.settings.get("resources.cacheReadWeight"))
    return Totals(
        delivered=len(delivered),
        failed=failed,
        signals=signals,
        problems=_count(source.conn, "SELECT COUNT(*) FROM problems WHERE first_seen >= ?", stamp),
        issues=_count(source.conn, "SELECT COUNT(*) FROM issues WHERE created_at >= ?", stamp),
        approvals_auto=auto,
        approvals_manual=manual,
        tokens=tokens,
        tokens_by_model=dict(by_model),
        cost_usd=cost,
        avg_duration_s=_average([sum(h.metrics.duration_ms or 0 for h in items) / 1000
                                 for items in per_issue.values()]),
        avg_tokens=_average([sum(h.metrics.tokens.weighted(weight) for h in items if h.metrics.tokens)
                             for items in per_issue.values()]),
        first_pass=_first_pass(per_issue, delivered),
    )


def _count(conn: sqlite3.Connection, sql: str, *args: Any) -> int:
    return int(conn.execute(sql, args).fetchone()[0])


def _average(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _first_pass(per_issue: dict[str, list[Handoff]], delivered: set[str]) -> float | None:
    """交付的 Issue 中，编码只做了一轮(审查没有交回)的比例。"""
    if not delivered:
        return None
    once = sum(1 for subject in delivered
               if all((handoff.round or 1) <= 1 for handoff in per_issue.get(subject, [])
                      if handoff.point == "implement.code"))
    return once / len(delivered)


# 内部：健康


def _stray_worktrees(layout: WorkspaceLayout, all_issues: Sequence[Issue]) -> int:
    """worktree 目录名里找不到任何未结束 Issue 编号的，算残留。"""
    if not layout.worktrees_dir.is_dir():
        return 0
    live = [issue.id for issue in all_issues if issue.status not in CLOSED_ISSUES]
    return sum(1 for path in layout.worktrees_dir.iterdir()
               if path.is_dir() and not any(identifier in path.name for identifier in live))


def _disk(root: Path) -> int:
    total = 0
    for directory, _, files in os.walk(root):
        for name in files:
            try:
                total += (Path(directory) / name).stat().st_size
            except OSError:
                continue  # 正在被清理或轮转的文件
    return total


def _purge_runs(source: Source, now: datetime) -> int:
    """超过保留期、下次清理会删掉的运行目录数(年龄取自运行编号，与 store/retention.py 一致)。"""
    if not source.layout.runs_dir.is_dir():
        return 0
    keep = timedelta(seconds=source.settings.duration("records.retention.runs"))
    count = 0
    for path in source.layout.runs_dir.iterdir():
        try:
            count += now - run_started(path.name) > keep
        except ValueError:
            continue
    return count


# 内部：watch


def _events(layout: WorkspaceLayout, run: str) -> list[dict[str, Any]]:
    path = layout.events(run)
    if not path.is_file():
        return []
    found = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue  # 正在写入的最后一行，下一次刷新再读
        if isinstance(event, dict) and event.get("at") and not str(event.get("summary") or "").startswith(_NOISE):
            found.append(event)
    return found


def _current_stage(run: RunInfo | None, events: Sequence[dict[str, Any]]) -> str | None:
    """运行正在做的阶段：一轮之内各阶段先后进行(protocol/schedule 的 RUN_ORDER)，取最近一条事件所在的阶段。"""
    if run is None or run.status not in LIVE_RUNS:
        return None
    for event in reversed(events):
        stage = str(event.get("point") or "").split(".")[0]
        if stage in STAGES:
            return stage
    return run.stage if run.stage in STAGES else None


def _github(issue: Issue) -> int | None:
    mirror = issue.extra.get(EXTRA_GITHUB)
    number = mirror.get("number") if isinstance(mirror, dict) else None
    return number if isinstance(number, int) and not isinstance(number, bool) else None


def _markers(layout: WorkspaceLayout, subject: str) -> list[dict[str, Any]]:
    directory = layout.subject_dir(subject)
    if not directory.is_dir():
        return []
    found = []
    for path in directory.glob(_STARTED):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue  # 正在写入，下一次刷新再读
        if not isinstance(data, dict) or not data.get("point") or not data.get("startedAt"):
            continue
        round_match = _ROUND_IN_PATH.search(path.name)
        data["_round"] = int(round_match.group(1)) if round_match else None
        found.append(data)
    return sorted(found, key=lambda data: str(data.get("startedAt")))


def _running(markers: Sequence[dict[str, Any]]) -> CallInfo | None:
    """进行中的模型调用：started 标记的 endedAt 为空；有几个时取最新开始的。"""
    running = [data for data in markers if data.get("endedAt") is None]
    if not running:
        return None
    data = running[-1]
    tokens = data.get("tokens")
    turns = data.get("turns")
    return CallInfo(
        point=str(data["point"]),
        round=data["_round"],
        tool=str(data.get("tool") or ""),
        model=str(data.get("model") or ""),
        effort=data.get("effort"),
        started_at=parse_iso(str(data["startedAt"])),
        turns=turns if isinstance(turns, int) and not isinstance(turns, bool) else None,
        tokens=(Tokens(**{_snake(key): int(value) for key, value in tokens.items()
                          if _snake(key) in _TOKEN_FIELDS and isinstance(value, int)})
                if isinstance(tokens, dict) else None),
    )


def _snake(name: str) -> str:
    return "".join(f"_{char.lower()}" if char.isupper() else char for char in name)


def _last_failure(markers: Sequence[dict[str, Any]], point: str | None, call: CallInfo | None) -> str | None:
    """这一步最近一次结束的调用没有成功时的结果；正在调用时它就是重试前的那一次。"""
    if point is None:
        return None
    ended = [data for data in markers if data.get("point") == point and data.get("endedAt")]
    if not ended or call is not None and parse_iso(str(ended[-1]["startedAt"])) >= call.started_at:
        return None
    status = ended[-1].get("status")
    return str(status) if status and status != "ok" else None


def _base_step(steps: Sequence[str], point: str | None) -> str | None:
    """调用点所在的那一格：implement.review.deep → implement.review。"""
    if point is None:
        return None
    return next((step for step in steps if point == step or point.startswith(step + ".")), None)


def _issue_rows(source: Source, all_issues: Sequence[Issue], waiting: AbstractSet[str]) -> list[ActiveRow]:
    """实施中的 Issue 各一行(等你处理的除外，列在等你处理)；发布中的只在有进行中的模型调用时列出。"""
    budget = IssueBudget(source.conn, source.clock, source.settings)
    rows = []
    for issue in all_issues:
        if issue.status not in ACTIVE_ISSUES or issue.held_by or issue.id in waiting:
            continue
        markers = _markers(source.layout, issue.id)
        call = _running(markers)
        if issue.status != "implementing" and call is None:
            continue
        rows.append(_issue_row(source, issue, markers, call, budget))
    return rows


def _issue_row(source: Source, issue: Issue, markers: Sequence[dict[str, Any]], call: CallInfo | None,
               budget: IssueBudget) -> ActiveRow:
    checkpoints = _checkpoints(source.layout, issue.id)
    point = call.point if call else point_of(issue.stage, issue.step)
    round_ = call.round if call and call.round else issue.round
    stage = (point or issue.stage or "implement").split(".")[0]
    return ActiveRow(
        id=issue.id,
        kind="issue",
        github=_github(issue),
        title=issue.title,
        severity=issue.severity,
        steps=_step_marks(source.settings, stage, checkpoints, point, round_),
        point=point,
        step=_base_step(stage_steps(stage), point),
        round=round_,
        action=_action(point, call),
        call=call,
        last_failure=_last_failure(markers, point, call),
        stalled=_stalled(checkpoints),
        step_started=call.started_at if call else (_created(checkpoints[-1]) if checkpoints else None),
        step_limit_s=_limit_s(source.settings, point),
        tokens_used=budget.used(issue.id),
        tokens_limit=budget.limit,
    )


def _problem_rows(source: Source, events: Sequence[dict[str, Any]]) -> list[ActiveRow]:
    """本次运行里正在评估(有进行中的模型调用)的问题各一行；评估按组并行，可能同时有几个。"""
    subjects = dict.fromkeys(str(event["subject"]) for event in events
                             if str(event.get("subject") or "").startswith("P-"))
    rows = []
    for subject in sorted(subjects):
        markers = _markers(source.layout, subject)
        call = _running(markers)
        if call is None:
            continue
        found = source.conn.execute("SELECT title, extra FROM problems WHERE id = ?", (subject,)).fetchone()
        if found is None:
            continue
        extra = json.loads(found[1]) if found[1] else {}
        severity = extra.get(EXTRA_SEVERITY) if isinstance(extra, dict) else None
        rows.append(ActiveRow(
            id=subject, kind="problem", github=None, title=str(found[0]),
            severity=severity if isinstance(severity, str) else None,
            steps=_assess_marks(markers, _checkpoints(source.layout, subject), call.point),
            point=call.point, step=_assess_slot(call.point), round=call.round, action="model", call=call,
            last_failure=_last_failure(markers, call.point, call), stalled=False, step_started=call.started_at,
            step_limit_s=_limit_s(source.settings, call.point), tokens_used=None, tokens_limit=None,
        ))
    return rows


def _assess_slot(point: str | None) -> str | None:
    if point is None:
        return None
    base = _base_step(ASSESS_FLOW + tuple(ASSESS_SLOTS), point)
    return ASSESS_SLOTS.get(base or "", base)


def _assess_marks(markers: Sequence[dict[str, Any]], checkpoints: Sequence[Checkpoint],
                  current: str | None) -> tuple[StepMark, ...]:
    """问题评估的各格(查重 → 判定 → 取证 → 写 Issue)：有交接或调用成功结束的为做完；取证含反证。"""
    done = {_assess_slot(item.handoff.point) for item in checkpoints if item.handoff.status is Status.PASSED}
    done |= {_assess_slot(str(data["point"])) for data in markers if data.get("endedAt") and data.get("status") == "ok"}
    active = _assess_slot(current)
    return tuple(StepMark(slot, "active" if slot == active else "done" if slot in done else "waiting",
                          None, None, None, None) for slot in ASSESS_FLOW)


def _run_row(source: Source, run: RunInfo, stage: str, collect: CollectCard) -> ActiveRow | None:
    """运行级的一行：采集(各来源)；复盘等记在运行目录、正在调用模型的。"""
    markers = _markers(source.layout, run.id)
    call = _running(markers)
    if call is not None and call.point.split(".")[0] in STAGES:
        stage = call.point.split(".")[0]
    if stage == "collect":
        steps = tuple(StepMark(mark.key, _SOURCE_STEP[mark.state], mark.duration_s, mark.reason, None, None)
                      for mark in collect.sources if mark.state != "off")
        steps += (StepMark("collect.dedup", "done" if collect.dedup is not None else "waiting", None, None, None, None),)
        active = next((mark.point for mark in steps if mark.state == "active"), None)
        point = call.point if call else active
        started = call.started_at if call else collect.started_at
    elif call is not None or stage == "retro":
        steps = _step_marks(source.settings, stage, _checkpoints(source.layout, run.id),
                            call.point if call else None, None)
        point = call.point if call else stage
        started = call.started_at if call else None
    else:
        return None
    return ActiveRow(
        id=stage, kind="collect" if stage == "collect" else "run", github=None, title="", severity=None,
        steps=steps, point=point, step=_base_step([mark.point for mark in steps], point),
        round=call.round if call else None, action=_action(point, call), call=call,
        last_failure=_last_failure(markers, point, call), stalled=False, step_started=started,
        step_limit_s=_limit_s(source.settings, point), tokens_used=None, tokens_limit=None,
    )


def _step_marks(settings: Settings, stage: str, checkpoints: Sequence[Checkpoint], current: str | None,
                round_: int | None) -> tuple[StepMark, ...]:
    latest: dict[str, Checkpoint] = {checkpoint.handoff.point: checkpoint for checkpoint in checkpoints}
    marks = []
    passed_current = False
    for point in stage_steps(stage):
        rounds = _control_int(settings, point, "rounds")
        if current is not None and (current == point or current.startswith(point + ".")):
            marks.append(StepMark(point, "active", None, None, round_, rounds))
            passed_current = True
            continue
        item = latest.get(point)
        # 当前步之后、还停在上一轮的步骤(编码第 2 轮时的自检、审查第 1 轮)这一轮还没做
        earlier_round = passed_current and item is not None and (item.round or 0) < (round_ or 0)
        if item is None or earlier_round:
            marks.append(StepMark(point, "waiting", None, None, None, rounds))
            continue
        handoff = item.handoff
        duration = handoff.metrics.duration_ms / 1000 if handoff.metrics.duration_ms is not None else None
        skipped = handoff.facts.get(FACT_SKIPPED)
        if skipped:
            marks.append(StepMark(point, "skipped", None, str(skipped), item.round, rounds))
        elif handoff.status is Status.FAILED:
            marks.append(StepMark(point, "failed", duration, None, item.round, rounds))
        elif handoff.status is Status.PENDING:
            marks.append(StepMark(point, "gate", duration, None, item.round, rounds))
        else:
            auto = handoff.facts.get(FACT_AUTO)
            note = None if not isinstance(auto, bool) else "auto" if auto else "manual"
            marks.append(StepMark(point, "done", duration, note, item.round, rounds))
    return tuple(marks)


def _control_int(settings: Settings, point: str | None, name: str) -> int | None:
    if point is None:
        return None
    try:
        return int(settings.control(point, name))
    except (MissingSetting, ValueError):
        return None


def _action(point: str | None, call: CallInfo | None) -> str:
    if call is not None:
        return "model"
    if point is None:
        return "idle"
    if point.endswith(".check"):
        return "tests"
    if point == "release.ci":
        return "ci"
    return "program"


def _stalled(checkpoints: Sequence[Checkpoint]) -> bool:
    """审查至少交回过两轮，且与上一轮比没有进展：编码的 diff 哈希没变，或阻断项(位置, 类型)是同一批。
    判断用 protocol.limits.no_progress，与调度停下的规则同一处。"""
    reviews = [[(str(entry.get("location")), str(entry.get("kind"))) for entry in item.handoff.facts[FACT_BLOCKERS]
                if isinstance(entry, dict)]
               for item in checkpoints
               if item.handoff.point == REVIEW_POINT and isinstance(item.handoff.facts.get(FACT_BLOCKERS), list)]
    if len(reviews) < 2:
        return False
    diffs = [str(item.handoff.facts[FACT_DIFF_HASH]) for item in checkpoints
             if item.handoff.point == CODE_POINT and item.handoff.facts.get(FACT_DIFF_HASH) is not None]
    previous_diff, current_diff = (diffs[-2], diffs[-1]) if len(diffs) >= 2 else (None, None)
    return no_progress(reviews[-2], reviews[-1], previous_diff, current_diff)


def _manual_problems(conn: sqlite3.Connection) -> tuple[str, ...]:
    """评估转人工的问题(状态不变、带 manual 标记，不再自动评估)：等你看了再改判或请求重新评估。"""
    rows = conn.execute(
        f"SELECT id FROM problems WHERE status IN ({_WAITING_SQL}) AND {_MANUAL_SQL} = 1 AND {_MERGED_SQL} IS NULL "
        "ORDER BY id").fetchall()
    return tuple(str(row[0]) for row in rows)


def _queue(source: Source, all_issues: Sequence[Issue], run: RunInfo | None, stage: str | None) -> QueueInfo:
    releasing = [point_of(issue.stage, issue.step) for issue in all_issues
                 if issue.status == "releasing" and not issue.held_by]
    pending = source.conn.execute(
        f"SELECT COUNT(*) FROM problems WHERE status IN ({_WAITING_SQL}) AND COALESCE({_MANUAL_SQL}, 0) = 0 "
        f"AND {_MERGED_SQL} IS NULL").fetchone()[0]
    return QueueInfo(
        todo=_queued(all_issues),
        assess_pending=int(pending),
        ci=sum(1 for point in releasing if point in ("release.pr", "release.ci")),
        to_merge=sum(1 for point in releasing if point == "release.merge"),
        deploying=sum(1 for point in releasing if point == "release.deploy"),
        accepting=sum(1 for issue in all_issues if issue.status == "accepting"),
        retro_after=run is not None and run.stage == "run" and stage is not None and stage != "retro",
    )


def _collect_card(source: Source, run: RunInfo | None, events: Sequence[dict[str, Any]],
                  stage: str | None) -> CollectCard:
    """采集看板：运行目录中每个来源的交接；正在采集时，还没有交接、但已有事件的来源为进行中。"""
    try:
        setup: Setup | None = setup_file.load(source.layout)
    except SetupInvalid:
        setup = None
    collecting = run is not None and stage == "collect"
    done = {item.handoff.point: item for item in _checkpoints(source.layout, run.id)} if run else {}
    first_event: dict[str, datetime] = {}
    for event in events:
        point = str(event.get("point") or "")
        key = _base_step(COLLECT_SOURCES, point)
        if key is not None:
            first_event.setdefault(key, parse_iso(str(event["at"])))
    now = source.clock.now()
    marks = []
    for key in COLLECT_SOURCES:
        if setup is not None and key in setup.modules and not setup.enabled(key):
            marks.append(SourceMark(key, "off", None, None, None, None))
            continue
        item = done.get(key)
        if item is not None:
            handoff = item.handoff
            skipped = handoff.facts.get(FACT_SKIPPED)
            state_ = "skipped" if skipped else "failed" if handoff.status is Status.FAILED else "done"
            marks.append(SourceMark(
                key, state_, _int(handoff.facts.get(FACT_READ)), (handoff.metrics.produced or {}).get(PRODUCED_SIGNALS),
                handoff.metrics.duration_ms / 1000 if handoff.metrics.duration_ms is not None else None,
                str(skipped) if skipped else None))
        elif collecting and key in first_event:
            marks.append(SourceMark(key, "active", None, None, (now - first_event[key]).total_seconds(), None))
        else:
            marks.append(SourceMark(key, "waiting", None, None, None, None))
    dedup_item = done.get("collect.dedup")
    breakers = _breakers(source)
    started = min(first_event.values(), default=None) if first_event else None
    return CollectCard(
        started_at=started or (run.started_at if collecting and run else None),
        sources=tuple(marks),
        dedup=_dedup(dedup_item.handoff) if dedup_item else None,
        breaker=breakers[0] if breakers else None,
    )


def _dedup(handoff: Handoff) -> tuple[int, int, int, int]:
    new, merged, muted, regressed = (_int(handoff.facts.get(name)) or 0 for name in FACT_DEDUP)
    return new, merged, muted, regressed


def _event_lines(events: Sequence[dict[str, Any]]) -> list[EventLine]:
    lines = []
    for event in events[-EVENT_LINES:][::-1]:
        refs = event.get("refs") or {}
        round_match = next((match for match in (_ROUND_IN_PATH.search(str(value)) for value in refs.values()) if match),
                           None)
        mark, summary = _mark(event)
        subject = event.get("subject")
        lines.append(EventLine(
            at=parse_iso(str(event["at"])),
            subject=None if not subject or str(subject).startswith("R-") else str(subject),
            point=str(event.get("point") or ""),
            round=int(round_match.group(1)) if round_match else None,
            mark=mark,
            summary=summary,
        ))
    return lines


def _mark(event: dict[str, Any]) -> tuple[str, str]:
    """(标记, 摘要)：一步的结果(passed、failed、pending)按结果标出并去掉前缀；模型调用成功是例行的(routine)，
    失败为 bad；其余异常(越界、熔断、超时、重试)为 warn；决定为 info；程序做完的动作为 ok。"""
    summary = str(event.get("summary") or "")
    kind = event.get("kind")
    head, separator, rest = summary.partition("：")
    if kind == "effect" and separator and head in _STEP_RESULTS:
        return _STEP_RESULTS[head], rest
    if kind == "effect" and separator and head.endswith("失败"):
        return "bad", summary
    status = (event.get("refs") or {}).get("status")
    match = _CALL_STATUS.match(summary)
    status = status or (match.group(1) if match else None)
    if status is not None:
        return ("routine" if status == "ok" else "ok" if status in ("passed", "done") else "bad"), summary
    if kind == "effect":
        return "warn", summary
    return ("ok" if kind == "action" else "info"), summary


__all__ = [
    "ActiveItem",
    "ActiveRow",
    "BreakerInfo",
    "CallInfo",
    "CollectCard",
    "ControlInfo",
    "EventLine",
    "Health",
    "QueueInfo",
    "QuotaInfo",
    "RunInfo",
    "Source",
    "SourceMark",
    "StatusSnapshot",
    "StepMark",
    "Stock",
    "Totals",
    "WaitingItem",
    "WatchSnapshot",
    "point_of",
    "stage_steps",
    "status_snapshot",
    "watch_snapshot",
]
