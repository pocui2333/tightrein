"""处理标签、去向、Issue 标签与问题状态变化的上下文(architecture/06 4.12，redesign/03-triage.md)。

规则都在 domain 的纯函数中，这里只按配置取决策树(triage.treatment.rules)与阈值；之前判为观察的问题再次进入分诊
仍判为观察时升为排期修。暂不修的恢复条件取
thresholds.triage.deferredReopenOccurrences，「需要先与代码作者讨论」看预估改动文件是否命中 protectedPaths。
用户改判不调用角色：没有给出去向时，不成立为误报、证据不足为人工队列、成立与条件成立为提 Issue。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from tightrein.config.project import ProjectConfig
from tightrein.domain import triage as rules
from tightrein.domain.enums import Disposition, IssueLabel, Severity, SizeTier, Treatment, Verdict, WorthRecommendation
from tightrein.domain.problem import Problem, ProblemContext
from tightrein.domain.triage import TreatmentFacts, TreatmentRule
from tightrein.guards.policy import GuardSettings
from tightrein.guards.protected import matching_pattern

OVERRIDE_DISPOSITIONS = {
    Verdict.REFUTED: Disposition.FALSE_POSITIVE,
    Verdict.INSUFFICIENT: Disposition.MANUAL_QUEUE,
    Verdict.CONFIRMED: Disposition.CREATE_ISSUE,
    Verdict.CONDITIONAL: Disposition.CREATE_ISSUE,
}
CONFIRMING = frozenset({Verdict.CONFIRMED, Verdict.CONDITIONAL})


@dataclass(frozen=True)
class Decision:
    disposition: Disposition
    context: ProblemContext
    treatment: Treatment | None = None
    labels: tuple[IssueLabel, ...] = ()


def tree(config: ProjectConfig) -> list[TreatmentRule]:
    return [TreatmentRule.from_config(item) for item in config.get("triage.treatment.rules")]


def context_for(config: ProjectConfig, problem: Problem, disposition: Disposition) -> ProblemContext:
    if disposition is Disposition.DEFERRED:
        occurrences = config.whole_threshold("triage.deferredReopenOccurrences")
        return ProblemContext(disposition=disposition, ignore_until=rules.deferred_condition(problem, occurrences))
    return ProblemContext(disposition=disposition)


def touches_protected(config: ProjectConfig, files: Sequence[str]) -> bool:
    patterns = GuardSettings.from_config(config).protected_paths
    return any(matching_pattern(file, patterns) is not None for file in files)


def decide(config: ProjectConfig, problem: Problem, *, verdict: Verdict, severity: Severity | None,
           tier: SizeTier | None, worth: WorthRecommendation | None, fixed_on_main: bool, tradeoff_hit: bool,
           needs_manual: bool, refuter_verdict: Verdict | None, estimated_files: Sequence[str],
           observed_before: bool = False) -> Decision:
    """observed_before：这个问题上一次分诊的处理标签为观察——它因再次出现或严重度升级恢复后又进入分诊，
    决策树仍给出观察时升为排期修，建 Issue(redesign/04-issue.md 第 2 节)。"""
    treatment = None
    if verdict in CONFIRMING and not needs_manual:
        treatment = rules.treatment(TreatmentFacts(verdict, severity, tier, worth), tree(config))
        if treatment is Treatment.OBSERVE and observed_before:
            treatment = Treatment.SCHEDULED
    disposition = rules.disposition(rules.DispositionFacts(
        verdict, severity, treatment, fixed_on_main, tradeoff_hit, needs_manual, refuter_verdict))
    labels: tuple[IssueLabel, ...] = ()
    if disposition is Disposition.CREATE_ISSUE:
        labels = rules.labels(touches_protected(config, estimated_files))
    return Decision(disposition, context_for(config, problem, disposition), treatment, labels)


def override(config: ProjectConfig, problem: Problem, verdict: Verdict,
             disposition: Disposition | None = None) -> Decision:
    chosen = disposition or OVERRIDE_DISPOSITIONS[verdict]
    return Decision(chosen, context_for(config, problem, chosen))
