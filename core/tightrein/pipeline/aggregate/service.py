"""AggregateService(architecture/05 3.3、3.4、3.9、3.10)：把待聚合的 collect 运行归并成问题。

开始前校验 normalize.yaml 与 suppressions.yaml；获取全局锁 data/aggregate.lock(排队不超过
stages.aggregate.limits.maxDurationMs，--no-wait 时立即退出)；补写上次在交接文档写完前中断的聚合运行的交接文档；
按开始时间升序逐个处理运行：各步只修改内存中的 ChangeSet，再由 apply 在一个事务中写入。某个运行的状态转换被拒绝
或写入失败时中止，已提交的前序运行保留，本次聚合运行记为 failed。

--output 模式把数据库复制到内存中的连接后走同一条路径，真实的数据库文件不变，Issue 文件与 suppressions.yaml
的副作用不执行，ChangeSet 列表写入 <输出目录>/changeset.json；--dry-run 只列出将要处理的运行与健康检查结果。
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from datetime import timedelta
from pathlib import Path
from typing import Any

from tightrein.config import normalize as normalize_config
from tightrein.config.project import ProjectConfig
from tightrein.domain.clock import Clock
from tightrein.domain.enums import HandoffStatus, RunStage, RunStatus
from tightrein.domain.normalize import Rule
from tightrein.domain.run import Run
from tightrein.domain.signal import Signal
from tightrein.domain.suppression import SuppressionRule
from tightrein.observability.events import EventLog
from tightrein.observability.tracing import Tracer
from tightrein.pipeline.aggregate import commit_facts, rebuild
from tightrein.pipeline.aggregate.changeset import ChangeSet, Inheritance, TransitionRejected
from tightrein.pipeline.aggregate.commit_facts import AncestryReader
from tightrein.pipeline.aggregate.render.summary import summary as render_summary
from tightrein.pipeline.aggregate.steps import apply, group, normalize, output, reproduce, select, status, suppress
from tightrein.pipeline.aggregate.steps.output import Summary
from tightrein.pipeline.aggregate.steps.reproduce import Replayer
from tightrein.store import locks
from tightrein.store.files import atomic, suppressions
from tightrein.store.files.issue_files import IssueFileConflict, IssueFileError
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.files.suppressions import SuppressionFileError
from tightrein.store.locks import FileLockBusy
from tightrein.store.repos import problems, runs, signals

STAGE = RunStage.AGGREGATE
STEP = "run_script"
LIVE = "live"
SKIP = "skip"
NO_NEW_SIGNALS = "没有新信号"
CHANGESET_FILE = "changeset.json"
APPLY_FAILURES = (TransitionRejected, sqlite3.Error, OSError, IssueFileError, IssueFileConflict, SuppressionFileError)


def global_lock(layout: WorkspaceLayout, config: ProjectConfig, clock: Clock, *, no_wait: bool = False,
                sleep: Callable[[float], None] = time.sleep) -> ExitStack:
    """获取 data/aggregate.lock：被占用时每隔 runtime.store.lockPollSeconds 重试，超过 stages.aggregate.limits.maxDurationMs
    仍未获取则抛出 FileLockBusy；no_wait 时立即抛出。返回持有锁的 ExitStack，由调用方以 with 释放。"""
    limit = timedelta(milliseconds=int(config.get("stages.aggregate.limits.maxDurationMs")))
    poll = float(config.get("runtime.store.lockPollSeconds"))
    deadline = clock.now() + limit
    while True:
        stack = ExitStack()
        try:
            stack.enter_context(locks.file_lock(layout.aggregate_lock(), wait=False))
            return stack
        except FileLockBusy:
            if no_wait or clock.now() >= deadline:
                raise
            sleep(poll)


@dataclass(frozen=True)
class AggregateRequest:
    select: tuple[str, ...] = ()
    input: Path | None = None
    reproduce: str | None = None
    rebuild: bool = False
    no_wait: bool = False
    dry_run: bool = False


@dataclass
class AggregateDeps:
    layout: WorkspaceLayout
    config: ProjectConfig
    conn: sqlite3.Connection
    clock: Clock
    events: EventLog
    ancestry: AncestryReader
    replayer: Replayer | None = None
    sleep: Callable[[float], None] = time.sleep


@dataclass(frozen=True)
class PlannedRun:
    run_id: str
    probe: str | None
    signals: int
    health: int | None


@dataclass(frozen=True)
class AggregatePlan:
    runs: tuple[PlannedRun, ...]
    replays: tuple[str, ...]


@dataclass(frozen=True)
class AggregateResult:
    run: Run | None
    status: HandoffStatus | None
    handoffs: tuple[Path, ...] = ()
    plan: AggregatePlan | None = None
    message: str | None = None

    @property
    def exit_code(self) -> int:
        return 1 if self.status is HandoffStatus.FAILED else 0


@dataclass
class Processed:
    changesets: list[ChangeSet] = field(default_factory=list)
    reopened: list[str] = field(default_factory=list)
    failure: str | None = None


@dataclass(frozen=True)
class Rules:
    normalize: tuple[Rule, ...]
    suppressions: tuple[SuppressionRule, ...]


class AggregateService:
    def __init__(self, deps: AggregateDeps) -> None:
        self.deps = deps

    @property
    def output_mode(self) -> bool:
        return self.deps.layout.output_dir is not None

    def run(self, request: AggregateRequest) -> AggregateResult:
        replayer = self._replayer(request)
        rules = self.rules()
        run_id, probe = select.parse_selectors(request.select)
        with self.lock(request.no_wait):
            if not self.output_mode and not request.dry_run:
                self.recover()
            if request.rebuild and request.dry_run:
                every = [(run, signals.find(self.deps.conn, run_id=run.id)) for run in runs.find(
                    self.deps.conn, stage=RunStage.COLLECT) if run.status is not RunStatus.RUNNING]
                return AggregateResult(None, None, plan=self._plan(every, []))
            if request.rebuild:
                return rebuild.rebuild(self, rules)
            inputs = self._inputs(request, run_id, probe)
            replays = select.pending_replays(self.deps.conn) if replayer is not None else []
            if not inputs and not replays:
                return AggregateResult(None, None, message=NO_NEW_SIGNALS)
            if request.dry_run:
                return AggregateResult(None, None, plan=self._plan(inputs, replays))
            work = self.work_connection()
            items: Sequence[tuple[Run | None, list[Signal]]] = inputs or [(None, [])]
            notes = [select.NO_COVERAGE] if request.input is not None else []
            return self.process(work, items, rules, replayer, notes=notes)

    def _replayer(self, request: AggregateRequest) -> Replayer | None:
        mode = request.reproduce or (SKIP if self.output_mode else LIVE)
        if mode not in (LIVE, SKIP):
            raise ValueError(f"--reproduce 只能是 {LIVE} 或 {SKIP}：{mode}")
        if mode == SKIP:
            return None
        if self.deps.replayer is None:
            raise ValueError("--reproduce live 需要重放器，没有目标环境时使用 --reproduce skip")
        return self.deps.replayer

    def rules(self) -> Rules:
        """开始前读取并校验规范化与抑制规则，不合格时抛出异常，不做任何处理。"""
        layout = self.deps.layout
        return Rules(normalize_config.load(layout.normalize_rules()), tuple(suppressions.read(layout.suppressions())))

    def lock(self, no_wait: bool) -> ExitStack:
        deps = self.deps
        return global_lock(deps.layout, deps.config, deps.clock, no_wait=no_wait, sleep=deps.sleep)

    def work_connection(self) -> sqlite3.Connection:
        """--output 模式为数据库的内存副本，其余为数据库连接本身。"""
        if not self.output_mode:
            return self.deps.conn
        copy = sqlite3.connect(":memory:", isolation_level=None)
        self.deps.conn.backup(copy)
        copy.row_factory = sqlite3.Row
        copy.execute("PRAGMA foreign_keys = ON")
        return copy

    def recover(self) -> None:
        """上次聚合在事务提交后、交接文档写完前中断：按它写下的问题事件补写交接文档，运行记为成功。"""
        deps = self.deps
        for stale in runs.find(deps.conn, stage=STAGE, status=RunStatus.RUNNING):
            documents = output.documents(deps.conn, stale.id, Summary.from_events(deps.conn, stale.id), deps.clock)
            output.write(deps.layout, deps.conn, deps.clock, documents)
            runs.save(deps.conn, replace(stale, status=RunStatus.OK, ended_at=deps.clock.now()))

    def _inputs(self, request: AggregateRequest, run_id: str | None, probe: Any) -> list[tuple[Run, list[Signal]]]:
        conn = self.deps.conn
        if request.input is not None:
            run, found = select.from_input(request.input)
            existing = runs.get(conn, run.id)
            if existing is not None and existing.aggregated_at is not None:
                return []
            return [(run, found)]
        return [(run, select.pending_signals(conn, run)) for run in select.pending_runs(conn, run_id, probe)]

    def _plan(self, inputs: Sequence[tuple[Run, list[Signal]]], replays: Sequence[Any]) -> AggregatePlan:
        planned = tuple(
            PlannedRun(run.id, run.probe.value if run.probe else None, len(found),
                       run.environment_detail.health.status if run.environment_detail.health else None)
            for run, found in inputs)
        return AggregatePlan(planned, tuple(problem.id for problem in replays))

    def changeset(self, work: sqlite3.Connection, aggregate_run: str, run: Run | None, found: Sequence[Signal],
                  rules: Rules, replayer: Replayer | None, tracer: Tracer, *,
                  inheritance: Inheritance | None = None) -> ChangeSet:
        """对一个 collect 运行执行第 1 到 5 步(第 6 步的交接文档在全部运行之后统一生成)。"""
        deps = self.deps
        config = deps.config
        now = deps.clock.now()
        changeset = ChangeSet.start(work, aggregate_run, now, config.whole_threshold("suppressionDays"))
        changeset.inheritance = inheritance

        def step(name: str) -> Any:
            return tracer.span(STEP, attributes={"step": name, "collectRunId": run.id if run else None})

        if run is not None:
            changeset.register(run)
            with step("normalize"):
                remaining = normalize.apply(changeset, list(found), rules.normalize)
            with step("suppress"):
                remaining = suppress.apply(changeset, remaining, rules.suppressions, now)
            with step("group"):
                group.apply(changeset, work, remaining, int(config.get("runtime.aggregate.titleMaxChars")))
        with step("reproduce"):
            reproduce.apply(changeset, work, replayer=replayer, attempts=config.whole_threshold("reproduceAttempts"))
        with step("status"):
            facts = commit_facts.query(deps.ancestry, config.repo, status.commit_pairs(changeset, work, run))
            status.apply(changeset, work, run, facts, now, config.whole_threshold("resolveCoveredRuns"))
        return changeset

    def begin(self, work: sqlite3.Connection) -> tuple[Run, Tracer]:
        """创建本次 aggregate 运行(running)。"""
        deps = self.deps
        started = deps.clock.now()
        aggregate_id = runs.free_id(work, started, STAGE)
        tracer = Tracer(deps.events, deps.clock, run_id=aggregate_id, stage=STAGE.value)
        record = Run(aggregate_id, STAGE, started, RunStatus.RUNNING, trace_id=tracer.trace_id)
        runs.save(work, record)
        return record, tracer

    def process_runs(self, work: sqlite3.Connection, record: Run, tracer: Tracer,
                     items: Sequence[tuple[Run | None, list[Signal]]], rules: Rules, replayer: Replayer | None, *,
                     inheritance: Inheritance | None = None,
                     before: Callable[[Run | None], None] | None = None) -> Processed:
        """逐个运行执行各步并写入；某个运行失败时停止。整体重放不执行文件副作用(原先已执行过)。"""
        deps = self.deps
        side_effects = not self.output_mode and inheritance is None
        processed = Processed()
        for run, found in items:
            try:
                if before is not None:
                    before(run)
                changeset = self.changeset(work, record.id, run, found, rules, replayer, tracer,
                                           inheritance=inheritance)
                with tracer.span(STEP, attributes={"step": "apply"}):
                    processed.reopened += apply.commit(work, changeset, deps.clock, deps.layout,
                                                       side_effects=side_effects)
                processed.changesets.append(changeset)
            except APPLY_FAILURES as error:
                problem_id = getattr(error, "problem_id", None)
                tracer.event("gate", decision="abort", reason=str(error), error_type=type(error).__name__,
                             attributes={"problemId": problem_id, "collectRunId": run.id if run else None})
                processed.failure = f"处理 {run.id if run else '待确认问题'} 时中止：{error}"
                break
        return processed

    def finish(self, work: sqlite3.Connection, record: Run, tracer: Tracer, processed: Processed, *,
               notes: Sequence[str] = (), rebuild: dict[str, Any] | None = None) -> AggregateResult:
        """第 6 步：写交接文档、运行摘要或 changeset.json，结束本次 aggregate 运行。"""
        deps = self.deps
        failure = processed.failure
        summary = Summary.of(processed.changesets, processed.reopened)
        summary.notes[:0] = notes
        documents = output.documents(work, record.id, summary, deps.clock, failure=failure, rebuild=rebuild)
        with tracer.span(STEP, attributes={"step": "output"}):
            paths = output.write(deps.layout, None if self.output_mode else work, deps.clock, documents)
        if deps.layout.output_dir is not None:
            changesets = [item.to_dict() for item in processed.changesets]
            atomic.write_text(deps.layout.output_dir / CHANGESET_FILE,
                              json.dumps(changesets, ensure_ascii=False, indent=2) + "\n")
        else:
            titles = {problem.id: problem.title for problem in problems.find(work)}
            atomic.write_text(deps.layout.run_report(record.id),
                              render_summary(record.id, documents[0]["outputs"], titles))
        final = RunStatus.FAILED if failure is not None else RunStatus.OK
        record = replace(record, status=final, ended_at=deps.clock.now())
        runs.save(work, record)
        return AggregateResult(record, HandoffStatus.FAILED if failure else HandoffStatus.OK, tuple(paths))

    def process(self, work: sqlite3.Connection, items: Sequence[tuple[Run | None, list[Signal]]], rules: Rules,
                replayer: Replayer | None, *, notes: Sequence[str] = ()) -> AggregateResult:
        record, tracer = self.begin(work)
        processed = self.process_runs(work, record, tracer, items, rules, replayer)
        return self.finish(work, record, tracer, processed, notes=notes)
