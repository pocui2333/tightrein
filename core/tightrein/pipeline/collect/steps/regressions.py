"""第 5 步 复现检查(architecture/05 2.5，architecture/04 7.2、7.3)。

选出以「已修复」关闭的 Issue 中与本方法对应类型的检查(api-fuzz 为 api，static 为 static；页面类只在验证环节运行)，交给注入的
执行器；失败的检查生成回归信号，并入本次信号。回归信号的 targetFingerprints 取该 Issue 关联问题的指纹。
检查清单的读取与三类检查的执行由执行器完成(RegressionRunner，实现为 pipeline.checks.regressions.runner.RegressionExecutor)，
没有执行器时本步不执行检查并在 notes 中写明。
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from tightrein.domain.enums import IssueStatus, RegressionKind, RegressionResult, Source
from tightrein.domain.enums import Probe as ProbeKind
from tightrein.domain.fingerprint import REGRESSION_CHECK
from tightrein.domain.signal import Signal
from tightrein.sources.base import ProbeTarget
from tightrein.sources.common.redact import ProbeRedactor
from tightrein.sources.common.signals import SignalFactory
from tightrein.pipeline.checks.regressions.runner import RegressionOutcome
from tightrein.store.repos import issues, problems, regressions
from tightrein.store.repos.regressions import RegressionCheck

KINDS: dict[ProbeKind, RegressionKind] = {
    ProbeKind.API_FUZZ: RegressionKind.API,
    ProbeKind.STATIC: RegressionKind.STATIC,
}
NO_RUNNER = "没有提供复现检查执行器，本次未执行复现检查"


class RegressionRunner(Protocol):
    def run_checks(self, checks: Sequence[RegressionCheck], target: ProbeTarget) -> list[RegressionOutcome]: ...


@dataclass(frozen=True)
class RegressionStep:
    outcomes: tuple[RegressionOutcome, ...] = ()
    signals: tuple[Signal, ...] = ()
    notes: tuple[str, ...] = ()


def select(conn: sqlite3.Connection, probe: ProbeKind) -> list[RegressionCheck]:
    kind = KINDS.get(probe)
    if kind is None:
        return []
    fixed = [record.issue.id for record in issues.find(conn, status=IssueStatus.DONE)]
    return [check for issue_id in fixed for check in regressions.find(conn, issue_id=issue_id) if check.kind is kind]


def _signal(outcome: RegressionOutcome, fingerprints: list[str], factory: SignalFactory,
            target: ProbeTarget) -> Signal:
    check = outcome.check
    return factory.create(
        source=Source.SYNTHETIC, check=REGRESSION_CHECK, location=outcome.location,
        message=f"复现检查 {check.issue_id}/{check.check_id} 失败：{outcome.detail}",
        occurred_at=target.clock.now(), release=target.release,
        context={"issue": check.issue_id, "checkId": check.check_id, "targetFingerprints": fingerprints,
                 "detail": outcome.detail},
    )


def execute(conn: sqlite3.Connection, probe: ProbeKind, target: ProbeTarget, runner: RegressionRunner | None,
            redactor: ProbeRedactor, randomness: Callable[[int], bytes] = os.urandom) -> RegressionStep:
    checks = select(conn, probe)
    if not checks:
        return RegressionStep()
    if runner is None:
        return RegressionStep(notes=(NO_RUNNER,))
    outcomes = runner.run_checks(checks, target)
    factory = SignalFactory(target, probe, redactor, randomness=randomness)
    signals: list[Signal] = []
    notes: list[str] = []
    for outcome in outcomes:
        check = outcome.check
        if outcome.result is RegressionResult.INVALID:
            notes.append(f"复现检查 {check.issue_id}/{check.check_id} 无法执行：{outcome.detail}")
        if outcome.result is not RegressionResult.FAILED:
            continue
        fingerprints = sorted(problem.fingerprint for problem in problems.find(conn, issue_id=check.issue_id))
        if not fingerprints:
            notes.append(f"复现检查 {check.issue_id}/{check.check_id} 失败，但 Issue 没有关联问题，未生成回归信号")
            continue
        signals.append(_signal(outcome, fingerprints, factory, target))
    return RegressionStep(tuple(outcomes), tuple(signals), tuple(notes))
