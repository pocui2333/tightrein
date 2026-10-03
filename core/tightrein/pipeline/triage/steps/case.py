"""一个问题在分诊各步骤之间传递的状态。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from tightrein.domain.enums import Complexity, RunnerStatus, Verdict
from tightrein.domain.problem import Problem
from tightrein.domain.signal import Signal
from tightrein.pipeline.triage.steps.attribution import Attribution
from tightrein.pipeline.triage.steps.claims import Claim
from tightrein.pipeline.triage.steps.evidence import Evidence
from tightrein.pipeline.triage.steps.rating import Rating

CONFIRMING = frozenset({Verdict.CONFIRMED, Verdict.CONDITIONAL})


@dataclass
class TriageCase:
    problem: Problem
    latest: Signal | None
    signals: list[Signal]
    p0: bool
    claim: Claim
    complexity: Complexity
    knowledge: str
    attempt: int
    handled: list[int] = field(default_factory=list)
    evidence: Evidence | None = None
    refuter: Evidence | None = None
    verdict: Verdict | None = None
    refuter_verdict: Verdict | None = None
    needs_manual: bool = False
    attribution: Attribution = field(default_factory=Attribution)
    tradeoff_hit: str | None = None
    rating: Rating | None = None
    merged_into: str | None = None
    notes: list[str] = field(default_factory=list)
    attempts: dict[str, list[RunnerStatus]] = field(default_factory=dict)

    @property
    def outputs(self) -> dict[str, Any]:
        """证据检查所用的 outputs(判定、主张、证据、根因、缺少的信息、报告与评估)；没有取证时为空。"""
        return dict(self.evidence.outputs or {}) if self.evidence is not None else {}

    @property
    def confirmed(self) -> bool:
        return self.merged_into is None and self.verdict in CONFIRMING and not self.needs_manual

    def record(self, role: str, statuses: list[RunnerStatus]) -> None:
        if statuses:
            self.attempts.setdefault(role, []).extend(statuses)
