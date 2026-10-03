"""TriageResult 实体；严重度、处理标签与去向的判定规则(design 3.5，redesign/03-triage.md)。"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass

from tightrein.domain.enums import (
    Complexity,
    Disposition,
    ImpactKind,
    IssueLabel,
    Severity,
    SizeTier,
    TaskType,
    Treatment,
    TriageOutcome,
    Verdict,
    WorthRecommendation,
)
from tightrein.domain.problem import IgnoreCondition, Problem, ignore_condition


SEVERITY_BY_IMPACT: dict[ImpactKind, Severity] = {
    ImpactKind.AUTHORIZATION: Severity.P0,
    ImpactKind.DATA_OWNERSHIP: Severity.P0,
    ImpactKind.DATA_CORRECTNESS: Severity.P0,
    ImpactKind.CREDENTIAL_LEAK: Severity.P0,
    ImpactKind.CORE_FLOW_BROKEN: Severity.P1,
    ImpactKind.NON_CORE_ERROR: Severity.P2,
    ImpactKind.CONTRACT_MISMATCH: Severity.P2,
    ImpactKind.EXPERIENCE: Severity.P3,
    ImpactKind.SLOW_RESPONSE: Severity.P3,
    ImpactKind.DEPENDENCY_VULNERABILITY: Severity.P3,
}

# 处理标签到去向：观察留在问题列表(再出现或严重度升级时重新分诊)，不修直接忽略(redesign/04-issue.md 第 2 节)
DISPOSITION_BY_TREATMENT: dict[Treatment, Disposition] = {
    Treatment.IMMEDIATE: Disposition.CREATE_ISSUE,
    Treatment.SCHEDULED: Disposition.CREATE_ISSUE,
    Treatment.OBSERVE: Disposition.DEFERRED,
    Treatment.WONT_FIX: Disposition.ACCEPTED_TRADEOFF,
}


TREATMENT_ORDER = (Treatment.IMMEDIATE, Treatment.SCHEDULED, Treatment.OBSERVE, Treatment.WONT_FIX)
SEVERITY_ORDER = (Severity.P0, Severity.P1, Severity.P2, Severity.P3)


def urgency_key(treatment: Treatment | None, severity: Severity | None) -> tuple[int, int]:
    """排序键：先按处理标签(立即修在前)，再按严重度；没有的排在最后。Issue 列表与运行摘要共用。"""
    rank = TREATMENT_ORDER.index(treatment) if treatment is not None else len(TREATMENT_ORDER)
    level = SEVERITY_ORDER.index(severity) if severity is not None else len(SEVERITY_ORDER)
    return rank, level


@dataclass(frozen=True)
class RootCause:
    file: str
    line: int
    symbol: str | None = None


@dataclass(frozen=True)
class IntroducedBy:
    commit: str
    author: str | None = None
    pr: int | None = None


@dataclass(frozen=True)
class TriageFlags:
    """须由用户定夺的三类问题：根因在设计本身、要动数据结构或存量数据、会改变公共实现或接口契约。"""

    design: bool = False
    data_structure: bool = False
    public_contract: bool = False


@dataclass(frozen=True)
class TriageResult:
    problem_id: str
    attempt: int
    verdict: Verdict
    disposition: Disposition
    reason: str
    triage_commit: str
    severity: Severity | None = None
    complexity: Complexity | None = None
    root_causes: tuple[RootCause, ...] = ()
    introduced_by: IntroducedBy | None = None
    refuter_verdict: Verdict | None = None
    flags: TriageFlags = TriageFlags()
    labels: tuple[IssueLabel, ...] = ()
    outcome: TriageOutcome | None = None
    treatment: Treatment | None = None
    task_type: TaskType | None = None
    size_tier: SizeTier | None = None

    def __post_init__(self) -> None:
        if self.attempt < 1:
            raise ValueError(f"分诊次数从 1 开始：{self.attempt}")
        if self.verdict is Verdict.REFUTED and self.treatment is not None:
            raise ValueError("不成立的问题没有处理标签")


def severity(impact_kind: ImpactKind) -> Severity:
    return SEVERITY_BY_IMPACT[impact_kind]


@dataclass(frozen=True)
class TreatmentFacts:
    verdict: Verdict
    severity: Severity | None
    tier: SizeTier | None
    worth: WorthRecommendation | None


@dataclass(frozen=True)
class TreatmentRule:
    """决策树的一条规则：各条件为空表示不限，全部满足时给出 treatment。"""

    treatment: Treatment
    verdicts: frozenset[Verdict] = frozenset()
    severities: frozenset[Severity] = frozenset()
    tiers: frozenset[SizeTier] = frozenset()
    worth: frozenset[WorthRecommendation] = frozenset()

    @staticmethod
    def _hit(allowed: Collection[object], value: object | None) -> bool:
        return not allowed or value in allowed

    def matches(self, facts: TreatmentFacts) -> bool:
        return (self._hit(self.verdicts, facts.verdict) and self._hit(self.severities, facts.severity)
                and self._hit(self.tiers, facts.tier) and self._hit(self.worth, facts.worth))

    @classmethod
    def from_config(cls, data: Mapping[str, object]) -> TreatmentRule:
        def values(key: str, kind: type) -> frozenset:
            return frozenset(kind(item) for item in data.get(key) or [])
        return cls(Treatment(data["treatment"]), values("verdicts", Verdict), values("severities", Severity),
                   values("tiers", SizeTier), values("worth", WorthRecommendation))


def treatment(facts: TreatmentFacts, rules: Sequence[TreatmentRule]) -> Treatment:
    """按顺序取第一条命中的规则(triage.treatment.rules)；没有命中时说明决策树缺少兜底规则。"""
    for rule in rules:
        if rule.matches(facts):
            return rule.treatment
    raise ValueError("triage.treatment.rules 没有命中任何规则，最后一条应不带条件作为兜底")


@dataclass(frozen=True)
class DispositionFacts:
    """定去向所需的分诊结论。needs_manual 表示证据检查或证伪复核已要求转人工。"""

    verdict: Verdict
    severity: Severity | None = None
    treatment: Treatment | None = None
    fixed_on_main: bool = False
    tradeoff_hit: bool = False
    needs_manual: bool = False
    refuter_verdict: Verdict | None = None


def disposition(facts: DispositionFacts) -> Disposition:
    """按顺序判定：转人工与证据不足、不成立、main 上已修复、P0、已接受的取舍、处理标签。"""
    if facts.needs_manual or facts.verdict is Verdict.INSUFFICIENT:
        return Disposition.MANUAL_QUEUE
    if facts.verdict is Verdict.REFUTED:
        if facts.severity is Severity.P0 and facts.refuter_verdict is not Verdict.REFUTED:
            raise ValueError("P0 问题判为不成立须经证伪复核，且复核同样判为不成立")
        return Disposition.FALSE_POSITIVE
    if facts.fixed_on_main:
        return Disposition.AWAITING_DEPLOY
    if facts.severity is Severity.P0:
        return Disposition.CREATE_ISSUE
    if facts.tradeoff_hit:
        return Disposition.ACCEPTED_TRADEOFF
    if facts.treatment is None:
        raise ValueError("成立的问题须先由决策树给出处理标签")
    return DISPOSITION_BY_TREATMENT[facts.treatment]


def labels(touches_protected: bool) -> tuple[IssueLabel, ...]:
    """Issue 标签：预估改动触及受保护文件时需要先与代码作者讨论。"""
    return (IssueLabel.DISCUSS_WITH_AUTHOR,) if touches_protected else ()


def deferred_condition(problem: Problem, reopen_occurrences: int) -> IgnoreCondition:
    """暂不修的恢复条件：再出现 reopen_occurrences(thresholds.triage.deferredReopenOccurrences)次或严重度升级。"""
    return ignore_condition(problem, occurrences=reopen_occurrences, severity_escalated=True)
