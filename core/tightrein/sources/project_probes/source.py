"""ProjectProbeSource(redesign/01-collect.md 第 4 节)：运行到期的项目探针(--select name:<名称> 时只运行指定的)。

每个探针的窗口为上次运行时间到现在(第一次运行回看 sources.platform-errors.initialLookbackHours 小时)。探针成功时
它的输出信号进入本次运行，新的状态与运行时间随信号在同一事务中保存(probe_states)，探针名记入 coverage.sources；
失败的探针写明原因、不保存状态，下次到期时重试。没有登记时为 skipped(未启用)，没有到期的探针时为 skipped。
trial 供 `tightrein probe test` 单独试跑一个探针：只返回校验结果与将产出的信号，不保存任何东西。
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from tightrein.config.project import ProjectConfig
from tightrein.domain.enums import ProbeLevel, RunStatus
from tightrein.domain.enums import Probe as ProbeKind
from tightrein.domain.run import Coverage
from tightrein.domain.signal import Signal
from tightrein.extensions.invoke import ProcessRunner, SubprocessRunner
from tightrein.sources.base import PROBE_LEVELS, ProbeOptions, ProbeOutcome, ProbeTarget, failed, skipped
from tightrein.sources.common.redact import ProbeRedactor
from tightrein.sources.common.signals import RandomBytes, SignalFactory
from tightrein.sources.platform_errors.logs import ReleaseAt
from tightrein.sources.project_probes import registry, runner
from tightrein.sources.project_probes.registry import Registration
from tightrein.store.repos import probe_states
from tightrein.store.repos.probe_states import ProbeState

DISABLED = "未启用：sources.project-probes 没有登记探针"
NONE_DUE = "没有到期的项目探针"
LOOKBACK = "sources.platform-errors.initialLookbackHours"


@dataclass(frozen=True)
class ProjectProbeDependencies:
    config: ProjectConfig
    conn: sqlite3.Connection
    workspace: Path
    redactor: ProbeRedactor
    release_at: ReleaseAt
    runner: ProcessRunner = field(default_factory=SubprocessRunner)
    environ: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    randomness: RandomBytes = os.urandom


@dataclass(frozen=True)
class Trial:
    run: runner.ProbeRun
    signals: tuple[Signal, ...] = ()


class ProjectProbeSource:
    name = ProbeKind.PROJECT_PROBE
    levels = PROBE_LEVELS[ProbeKind.PROJECT_PROBE]

    def __init__(self, dependencies: ProjectProbeDependencies) -> None:
        self.deps = dependencies

    def _run_one(self, item: Registration, target: ProbeTarget, now: datetime) -> runner.ProbeRun:
        previous = probe_states.get(self.deps.conn, item.name)
        since = previous.last_run_at if previous is not None else now - timedelta(
            hours=int(self.deps.config.get(LOOKBACK)))
        document = runner.build_input(item, self.deps.workspace, None if previous is None else previous.last_run_at,
                                      None if previous is None else previous.state, since, now, target.environment,
                                      target.base_url)
        return runner.execute(item, document, workspace=self.deps.workspace, runner=self.deps.runner,
                              environ=self.deps.environ, redactor=self.deps.redactor, raw_dir=target.raw_dir)

    def run(self, target: ProbeTarget, level: ProbeLevel | None, options: ProbeOptions) -> ProbeOutcome:
        items = registry.registered(self.deps.config)
        if not items:
            return skipped(DISABLED)
        now = target.clock.now().replace(microsecond=0)
        if options.names:
            try:
                chosen = [registry.find(self.deps.config, name) for name in options.names]
            except LookupError as error:
                return failed(str(error))
        else:
            chosen = registry.due(self.deps.conn, items, now)
        if not chosen:
            return skipped(NONE_DUE)
        factory = SignalFactory(target, self.name, self.deps.redactor, randomness=self.deps.randomness)
        signals: list[Signal] = []
        states: list[ProbeState] = []
        notes: list[str] = []
        for item in chosen:
            result = self._run_one(item, target, now)
            if not result.ok or result.output is None:
                notes.append(f"项目探针 {item.name} 本次作废：{result.error}")
                continue
            signals += runner.to_signals(item.name, result.output, factory, self.deps.release_at, now)
            states.append(ProbeState(item.name, now, result.output.get("state")))
            notes += [f"{item.name}：{note}" for note in result.output.get("notes", [])]
        if not states:
            return failed(*notes)
        status = RunStatus.PARTIAL if len(states) < len(chosen) else RunStatus.OK
        stats = {"probes": len(chosen), "succeeded": len(states), "signals": len(signals)}
        return ProbeOutcome(status, tuple(signals), Coverage(sources=tuple(state.name for state in states)),
                            stats=stats, notes=tuple(notes), probe_states=tuple(states))

    def trial(self, name: str, target: ProbeTarget) -> Trial:
        item = registry.find(self.deps.config, name)
        now = target.clock.now().replace(microsecond=0)
        result = self._run_one(item, target, now)
        if not result.ok or result.output is None:
            return Trial(result)
        factory = SignalFactory(target, self.name, self.deps.redactor, randomness=self.deps.randomness)
        return Trial(result, tuple(runner.to_signals(item.name, result.output, factory, self.deps.release_at, now)))
