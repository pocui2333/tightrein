"""变体间比较与判定(architecture/03 2.6.5、2.6.6，design 14.4)。

每个非基线变体与基线逐用例比较：差值 = 该变体均值 - 基线均值，另列出逐项通过数有变化的评分项。

版本对比(purpose 为 version，用于改进建议)：
| 判定 | 条件 |
|---|---|
| incomplete | 因预算或环境原因没有完成全部运行，不给出其他判定 |
| reject | 任一用例的差值小于 0，即「有任何用例变差」 |
| needs-review | 没有用例变差，但候选变体有 unknown 项，或存在基线稳定通过而候选不稳定的用例 |
| pass | 以上都不是 |

工具与模型对比(purpose 为 tool-model)不做否决判定(完成时判定为空，未完成时为 incomplete)，报告按总体均值从高到低
列出各变体。均值由相同分母的分数求得，差值小于 -1e-9 才视为变差，避免浮点误差把持平判为变差。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from tightrein.domain.enums import EvalVerdict, ScoreResult
from tightrein.evaluation.stats import CaseStats, RunScore, VariantStats
from tightrein.evaluation.variants import EvaluationPlan

EPSILON = 1e-9
STABLE_PASS = 1.0


@dataclass(frozen=True)
class ItemChange:
    item_id: str
    baseline: tuple[int, int]
    candidate: tuple[int, int]

    def to_dict(self) -> dict[str, Any]:
        return {"itemId": self.item_id, "baseline": list(self.baseline), "candidate": list(self.candidate)}


@dataclass(frozen=True)
class CaseComparison:
    case_id: str
    variant: str
    baseline_mean: float
    mean: float
    delta: float
    item_changes: list[ItemChange]
    baseline_pass_rate: float
    unstable: bool

    @property
    def worse(self) -> bool:
        return self.delta < -EPSILON

    def to_dict(self) -> dict[str, Any]:
        return {"caseId": self.case_id, "variant": self.variant, "baselineMean": self.baseline_mean,
                "mean": self.mean, "delta": self.delta,
                "itemChanges": [change.to_dict() for change in self.item_changes]}


def compare(stats: Sequence[CaseStats], baseline: str) -> list[CaseComparison]:
    base = {item.case_id: item for item in stats if item.variant == baseline}
    found = []
    for item in stats:
        if item.variant == baseline or item.case_id not in base:
            continue
        reference = base[item.case_id]
        changes = [
            ItemChange(item_id, reference.item_pass_counts.get(item_id, (0, 0)),
                       item.item_pass_counts.get(item_id, (0, 0)))
            for item_id in sorted(set(reference.item_pass_counts) | set(item.item_pass_counts))
            if reference.item_pass_counts.get(item_id) != item.item_pass_counts.get(item_id)
        ]
        found.append(CaseComparison(item.case_id, item.variant, reference.mean, item.mean, item.mean - reference.mean,
                                    changes, reference.pass_rate, item.unstable))
    return found


def verdict(plan: EvaluationPlan, comparisons: Sequence[CaseComparison], runs: Sequence[RunScore],
            complete: bool) -> EvalVerdict | None:
    if not complete:
        return EvalVerdict.INCOMPLETE
    if plan.purpose == "tool-model":
        return None
    if any(item.worse for item in comparisons):
        return EvalVerdict.REJECT
    candidates = {variant.label for variant in plan.variants[1:]}
    unknown = any(item.result is ScoreResult.UNKNOWN for run in runs if run.variant in candidates
                  for item in run.items)
    newly_unstable = any(item.baseline_pass_rate == STABLE_PASS and item.unstable for item in comparisons)
    return EvalVerdict.NEEDS_REVIEW if unknown or newly_unstable else EvalVerdict.PASS


def ranking(stats: Sequence[VariantStats]) -> list[VariantStats]:
    """工具与模型对比：按总体均值从高到低，相同时按通过率。"""
    return sorted(stats, key=lambda item: (-item.mean, -item.pass_rate, item.variant))
