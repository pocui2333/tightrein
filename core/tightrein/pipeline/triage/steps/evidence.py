"""取证与重做(architecture/06 4.5、4.6，redesign/03-triage.md)。

取证只做一次，由 claim-verifier 同时给出判定、证据、严重度与 Issue 报告，以及价值判断、任务类型、预估规模与修复方向
(assessment)。static 不调用角色，直接检查采集时已有的取证输出(信号 context.verification)，检查不通过、没有，或缺少
分析、给人读的 report 与 assessment 时调用 claim-verifier；用户请求的重新分诊不复用。
每次输出先按代码快照补全位置(pipeline/common/locations.py)，再经证据检查与评估检查；文字中引用的位置补不全同样算不通过；
任一项不通过时把逐项原因交回同一角色重做，最多重做 retries 次；执行器返回 schema-invalid、limit-reached、failed 等非 ok
状态同样计为一次未通过的尝试。仍不通过时 passed 为假，由调用方转人工。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from tightrein.domain.enums import Complexity, Probe, RunnerStatus
from tightrein.domain.problem import Problem
from tightrein.domain.signal import Signal
from tightrein.evaluation.scorers.base import ItemResult
from tightrein.pipeline.common import locations
from tightrein.pipeline.triage.prompts import claim_verifier
from tightrein.pipeline.triage.prompts.claim_verifier import CLAIM_VERIFIER
from tightrein.pipeline.triage.prompts.common import RoleCalls
from tightrein.pipeline.triage.steps import evidence_checks
from tightrein.pipeline.triage.steps.claims import Claim

PREPARED_KEY = "verification"
PREPARED_KEYS = ("analysis", "assessment")


@dataclass
class Evidence:
    role: str
    output: Mapping[str, Any] | None = None
    outputs: dict[str, Any] | None = None
    items: list[ItemResult] = field(default_factory=list)
    statuses: list[RunnerStatus] = field(default_factory=list)
    passed: bool = False
    reason: str | None = None
    failures: list[str] = field(default_factory=list)


def reuses_prepared(problem: Problem) -> bool:
    """static 的问题在采集时已经取证，先检查已有的输出。"""
    return problem.probe is Probe.STATIC


def prepared_output(latest: Signal | None) -> Mapping[str, Any] | None:
    """static 信号中采集时已有的取证输出；缺少报告、分析或评估(旧格式)的不复用。"""
    value = None if latest is None else latest.ctx(PREPARED_KEY)
    if not isinstance(value, Mapping) or not value.get("report") or any(key not in value for key in PREPARED_KEYS):
        return None
    return value


def _checked(calls: RoleCalls, role: str, claim: Claim, output: Mapping[str, Any]) -> Evidence:
    completed = locations.complete(output, calls.context.workdir)
    output = completed.value
    outputs = evidence_checks.outputs_for(claim, output)
    items = evidence_checks.check(outputs, calls.context.workdir, calls.clock)
    failed = [*evidence_checks.failures(items), *completed.problems(),
              *evidence_checks.assessment_problems(output, calls.context.workdir)]
    return Evidence(role, output, outputs, items, passed=not failed, reason="；".join(failed) or None,
                    failures=failed)


def gather(calls: RoleCalls, problem_id: str, claim: Claim, *, role: str, complexity: Complexity, knowledge: str,
           extra: Sequence[str] = (), prepared: Mapping[str, Any] | None = None) -> Evidence:
    if prepared is not None:
        found = _checked(calls, CLAIM_VERIFIER, claim, prepared)
        if found.passed:
            return found
    feedback = list(extra)
    last = Evidence(role)
    statuses: list[RunnerStatus] = []
    for attempt in range(1, calls.retries + 2):
        result = calls.run(claim_verifier.task(calls.context, role, problem_id, claim, knowledge, complexity, attempt,
                                               feedback))
        statuses.append(result.status)
        if result.status is not RunnerStatus.OK or result.output is None:
            reason = f"执行器返回 {result.status.value}{f'({result.error_type})' if result.error_type else ''}"
            last = Evidence(role, last.output, last.outputs, last.items, reason=reason)
            feedback = [*extra, f"上一次执行没有完成：{reason}"]
            continue
        last = _checked(calls, role, claim, result.output)
        if last.passed:
            break
        feedback = [*extra, *last.failures]
    last.statuses = statuses
    return last
