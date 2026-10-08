"""取证(完整与轻量推测)与重做的循环。

每次输出先补全位置，再做证据检查(checks.py)，任一项不过就把逐项原因交回同一调用点重做，最多重做 retries 次；
执行返回非 ok(格式不符、超限、失败)也算一次没通过；重做后仍不过就判证据不足，不硬采纳。
已取证的情况先检查采集时的输出，通过就直接用，不调用模型。
能复现与轻量推测不要求反证追到入口(前者已确定存在，后者只推测触发条件)。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from tightrein.assess import checks
from tightrein.assess.cases import Case
from tightrein.assess.checks import Snapshot
from tightrein.assess.prompts import claim_verifier
from tightrein.assess.prompts.claim_verifier import Inputs
from tightrein.assess.prompts.common import Asked, ask

if TYPE_CHECKING:
    from tightrein.protocol.runtime import Runtime

COUNTER_CASES = frozenset({Case.FULL, Case.VERIFIED})


@dataclass
class Evidence:
    point: str
    output: dict[str, Any] | None = None
    passed: bool = False
    failures: list[str] = field(default_factory=list)
    asked: list[Asked] = field(default_factory=list)
    reused: bool = False

    @property
    def verdict(self) -> str | None:
        return None if self.output is None else str(self.output["verdict"])

    @property
    def reason(self) -> str | None:
        return "；".join(self.failures) or None


def examine(output: Mapping[str, Any], inputs: Inputs, snapshot: Snapshot, *, case: Case,
            vague_words: Sequence[str]) -> tuple[dict[str, Any], list[str]]:
    """补全位置并检查：返回(补全后的输出, 不通过的原因)。"""
    completed = checks.complete(output, snapshot)
    value = dict(completed.value)
    failures = checks.check(value, inputs.claim.to_json(), snapshot, vague_words=vague_words,
                            light=case not in COUNTER_CASES)
    return value, [*failures, *completed.problems()]


def gather(runtime: Runtime, point: str, subject: str, inputs: Inputs, snapshot: Snapshot, *, case: Case,
           retries: int, vague_words: Sequence[str], prepared: Mapping[str, Any] | None = None,
           extra_feedback: Sequence[str] = ()) -> Evidence:
    if prepared is not None:
        value, failures = examine(prepared, inputs, snapshot, case=case, vague_words=vague_words)
        if not failures:
            return Evidence(point, value, passed=True, reused=True)
    evidence = Evidence(point)
    feedback = list(extra_feedback)
    for attempt in range(1, retries + 2):
        asked = ask(runtime, point, claim_verifier.variables(point, inputs, feedback), subject=subject,
                    workdir=snapshot.root, round=attempt)
        evidence.asked.append(asked)
        if not asked.ok or asked.output is None:
            evidence.failures = [f"上一次{asked.failure}"]
            feedback = [*extra_feedback, *evidence.failures]
            continue
        evidence.output, evidence.failures = examine(asked.output, inputs, snapshot, case=case,
                                                     vague_words=vague_words)
        if not evidence.failures:
            evidence.passed = True
            break
        feedback = [*extra_feedback, *evidence.failures]
    return evidence
