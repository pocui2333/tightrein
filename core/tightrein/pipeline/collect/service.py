"""CollectService(architecture/05 2.5、2.8、2.9)：运行一种采集方法，把结果写入存储。

步骤：目标解析 → 前置条件 → 创建运行 → 调用方法 → 复现检查 → 落库 → 交接。每步一个 span，前置条件写 gate 事件。
交接文档与运行摘要列出未启用的采集方法(sources/enabled.py)。

三种输出模式：正常模式写数据库与文件；--output 模式(layout.output_dir 非空)照常运行，只把信号、交接文档与
原始输出写到输出目录，不写数据库(读取位置与已读记录因此不前进)；--dry-run 只返回将要执行的内容，不写任何东西。
外部依赖(方法实例、健康检查的 HTTP 传输、部署与引用的只读查询、复现检查执行器、静态巡检审查器、启用判断)全部注入；
审查器按运行编号构造(会话记录按运行编号命名)。
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import timedelta
from pathlib import Path

from tightrein.config.project import ProjectConfig
from tightrein.domain.clock import Clock
from tightrein.domain.enums import HandoffStatus, ProbeLevel, RunStage, RunStatus
from tightrein.domain.enums import Probe as ProbeKind
from tightrein.domain.run import Run
from tightrein.observability.events import EventLog
from tightrein.observability.tracing import Tracer
from tightrein.pipeline.collect.steps import handoff, persist, preconditions, regressions, run_probe, target
from tightrein.pipeline.collect.steps.preconditions import Gate
from tightrein.pipeline.collect.steps.regressions import RegressionRunner, RegressionStep
from tightrein.pipeline.collect.steps.target import DeploymentReader, RefReader, TargetInfo
from tightrein.sources.base import Probe, ProbeOutcome, resolve_level
from tightrein.sources.common.http import Transport
from tightrein.sources.common.redact import ProbeRedactor
from tightrein.sources.static.reviewer import Reviewer
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.repos import runs
from tightrein.store.repos.deployments import Deployment
from tightrein.pipeline.common.deploys import UNCONFIGURED as DEPLOY_UNCONFIGURED

STAGE = RunStage.COLLECT
STEP = "run_script"
WRITES_SIGNALS = frozenset({RunStatus.OK, RunStatus.PARTIAL})
REBUILD_HINT = "该运行已被聚合，重新解析的结果记在新运行中；执行 tightrein aggregate --rebuild 重建问题"
FAILED_EXIT = frozenset({HandoffStatus.BLOCKED, HandoffStatus.FAILED})


@dataclass(frozen=True)
class CollectRequest:
    probe: ProbeKind
    level: ProbeLevel | None = None
    select: tuple[str, ...] = ()
    target: str | None = None
    commit: str | None = None
    reparse: str | None = None
    import_archive: Path | None = None
    regressions: bool = True
    dry_run: bool = False


@dataclass
class CollectDeps:
    layout: WorkspaceLayout
    config: ProjectConfig
    conn: sqlite3.Connection
    clock: Clock
    events: EventLog
    probe_for: Callable[[ProbeKind], Probe]
    transport: Transport
    deployments: DeploymentReader
    refs: RefReader
    redactor: ProbeRedactor
    regressions: RegressionRunner | None = None
    reviewer: Callable[[str], Reviewer] | None = None
    randomness: Callable[[int], bytes] = os.urandom
    disabled: Callable[[], Mapping[str, str]] = dict


@dataclass(frozen=True)
class DryRunPlan:
    probe: ProbeKind
    level: ProbeLevel | None
    base_url: str | None
    release: str | None
    regression_checks: int
    reparse: str | None = None


@dataclass(frozen=True)
class CollectResult:
    run: Run | None
    status: HandoffStatus | None
    handoff: Path | None = None
    plan: DryRunPlan | None = None

    @property
    def exit_code(self) -> int:
        return 1 if self.status in FAILED_EXIT else 0


@dataclass
class _Context:
    """一次运行在各步骤之间传递的状态。"""

    request: CollectRequest
    info: TargetInfo
    run: Run
    tracer: Tracer
    replace: bool
    raw_dir: Path
    notes: list[str] = field(default_factory=list)


class CollectService:
    def __init__(self, deps: CollectDeps) -> None:
        self.deps = deps

    @property
    def _output(self) -> bool:
        return self.deps.layout.output_dir is not None

    def deployments(self) -> Deployment | None:
        """collect deployments：只做部署检测，不运行探针。"""
        deps = self.deps
        return target.detect_deployment(deps.conn, deps.deployments, deps.clock, record=not self._output)

    def run(self, request: CollectRequest) -> CollectResult:
        deps = self.deps
        level = resolve_level(request.probe, request.level)
        if request.import_archive is not None and request.probe is not ProbeKind.INCIDENTAL:
            raise ValueError("--import-archive 只用于 incidental")
        run_probe.parse_selectors(request.probe, request.select)
        writes = not self._output and not request.dry_run
        previous = self._previous(request)
        if writes:
            self._fail_stale_runs()
        info = target.resolve(
            deps.config, deps.layout, deps.conn, request.probe, deployments_reader=deps.deployments, refs=deps.refs,
            clock=deps.clock, target=request.target,
            commit=previous.target_commit if previous is not None else request.commit, record=writes)
        if request.dry_run:
            return CollectResult(None, None, plan=self._plan(request, level, info))
        context = self._start(request, level, info, previous)
        gate = self._gate(context)
        if not gate.passed:
            return self._blocked(context, gate)
        outcome = self._probe(context)
        return self._finish(context, gate, outcome)

    def _previous(self, request: CollectRequest) -> Run | None:
        if request.reparse is None:
            return None
        previous = runs.get(self.deps.conn, request.reparse)
        if previous is None:
            raise LookupError(f"没有运行 {request.reparse}")
        if previous.probe is not request.probe:
            raise ValueError(f"运行 {request.reparse} 的探针是 {previous.probe.value if previous.probe else '无'}")
        return previous

    def _fail_stale_runs(self) -> None:
        deps = self.deps
        limit = timedelta(milliseconds=int(deps.config.get("stages.collect.limits.maxDurationMs")))
        now = deps.clock.now()
        for stale in runs.find(deps.conn, stage=STAGE, status=RunStatus.RUNNING):
            if now - stale.started_at > limit:
                runs.save(deps.conn, replace(stale, status=RunStatus.FAILED, ended_at=now))

    def _plan(self, request: CollectRequest, level: ProbeLevel | None, info: TargetInfo) -> DryRunPlan:
        checks = len(regressions.select(self.deps.conn, request.probe)) if request.regressions else 0
        return DryRunPlan(request.probe, level, info.base_url, info.release, checks, request.reparse)

    def _start(self, request: CollectRequest, level: ProbeLevel | None, info: TargetInfo,
               previous: Run | None) -> _Context:
        deps = self.deps
        now = deps.clock.now()
        replaces = previous is not None and previous.aggregated_at is None
        if previous is not None:
            level = previous.level
        if previous is not None and replaces:
            run_id, started_at = previous.id, previous.started_at
        else:
            run_id, started_at = runs.free_id(deps.conn, now, STAGE, request.probe), now
        raw_dir = (WorkspaceLayout(deps.layout.root).probe_raw_dir(previous.id, request.probe)
                   if previous is not None else deps.layout.probe_raw_dir(run_id, request.probe))
        tracer = Tracer(deps.events, deps.clock, run_id=run_id, stage=STAGE.value)
        run = Run(run_id, STAGE, started_at, RunStatus.RUNNING, probe=request.probe, level=level,
                  target_commit=info.release, trace_id=tracer.trace_id)
        context = _Context(request, info, run, tracer, replaces, raw_dir)
        if previous is not None and not replaces:
            context.notes.append(REBUILD_HINT)
        if info.release is None and not deps.deployments.configured():
            context.notes.append(DEPLOY_UNCONFIGURED)
        if not self._output and not replaces:
            runs.save(deps.conn, run)
        return context

    @contextmanager
    def _step(self, context: _Context, name: str) -> Iterator[None]:
        with context.tracer.span(STEP, attributes={"step": name}):
            yield

    def _gate(self, context: _Context) -> Gate:
        deps = self.deps
        with self._step(context, "preconditions"):
            gate = preconditions.check(deps.config, deps.conn, context.request.probe, context.info,
                                       transport=deps.transport, clock=deps.clock,
                                       reparse=context.request.reparse is not None)
        context.tracer.event("gate", decision="pass" if gate.passed else "block",
                             reason=gate.reason or "前置条件满足")
        if gate.note is not None:
            context.notes.append(gate.note)
        return gate

    def _blocked(self, context: _Context, gate: Gate) -> CollectResult:
        run = replace(context.run, status=RunStatus.BLOCKED, ended_at=self.deps.clock.now(),
                      environment_detail=gate.environment)
        if not self._output:
            runs.save(self.deps.conn, run)
        path = self._handoff(context, run, None, (), RegressionStep(), reason=gate.reason, hint=gate.hint)
        return CollectResult(run, HandoffStatus.BLOCKED, path)

    def _probe(self, context: _Context) -> ProbeOutcome:
        deps = self.deps
        request = context.request
        options = run_probe.options_for(
            deps.conn, request.probe, selectors=request.select, reparse=request.reparse is not None,
            import_archive=request.import_archive,
            reviewer=None if deps.reviewer is None else deps.reviewer(context.run.id))
        probe_target = context.info.probe_target(context.run.id, context.raw_dir, deps.clock)
        with self._step(context, "run_probe"):
            try:
                return run_probe.execute(deps.probe_for(request.probe), probe_target, context.run.level, options)
            except BaseException:
                if not self._output:
                    runs.save(deps.conn, replace(context.run, status=RunStatus.FAILED, ended_at=deps.clock.now()))
                raise

    def _finish(self, context: _Context, gate: Gate, outcome: ProbeOutcome) -> CollectResult:
        deps = self.deps
        request = context.request
        environment = replace(outcome.environment, health=gate.environment.health)
        run = replace(context.run, status=outcome.status, ended_at=deps.clock.now(), coverage=outcome.coverage,
                      environment_detail=environment)
        step = RegressionStep()
        if outcome.status in WRITES_SIGNALS and request.regressions and request.reparse is None:
            probe_target = context.info.probe_target(run.id, context.raw_dir, deps.clock)
            with self._step(context, "regressions"):
                step = regressions.execute(deps.conn, request.probe, probe_target, deps.regressions, deps.redactor,
                                           deps.randomness)
        signals = (*outcome.signals, *step.signals) if outcome.status in WRITES_SIGNALS else ()
        reason = None
        if not self._output:
            with self._step(context, "persist"):
                try:
                    persist.commit(deps.conn, run, outcome, signals, step.outcomes, replace=context.replace)
                except (sqlite3.Error, OSError) as error:
                    reason = f"写入数据库失败，原始输出已保留，可用 --reparse {run.id} 恢复：{error}"
                    run = replace(run, status=RunStatus.FAILED)
                    signals = ()
                    runs.save(deps.conn, run)
        if outcome.status is RunStatus.FAILED:
            reason = "；".join(outcome.notes)
        path = self._handoff(context, run, outcome, signals, step, reason=reason, hint=None)
        return CollectResult(run, handoff.HANDOFF_STATUS[run.status], path)

    def _handoff(self, context: _Context, run: Run, outcome: ProbeOutcome | None, signals: tuple,
                 step: RegressionStep, *, reason: str | None, hint: str | None) -> Path:
        deps = self.deps
        notes = [*(outcome.notes if outcome is not None else ()), *step.notes, *context.notes]
        values = handoff.outputs(run, context.info, outcome, signals, step.outcomes, notes=notes,
                                 disabled=deps.disabled())
        with self._step(context, "handoff"):
            return handoff.write(deps.layout, None if self._output else deps.conn, deps.clock, run, values, signals,
                                 reason=reason, hint=hint)
