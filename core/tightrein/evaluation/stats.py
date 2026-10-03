"""每次运行的得分与统计(architecture/03 2.4、2.6.5)。

对每个「用例 × 变体」(n 为运行次数)：均值为 n 次 score 的算术平均；方差为样本方差(n - 1 为分母，n 为 1 时为 0)；
通过率为 passed 为真的次数 ÷ n；逐项通过数为每个评分项在适用的运行中的 (通过次数, 适用次数)；n 次运行中既有通过
也有不通过即为不稳定。对每个变体另算总体：各用例均值的平均、全部运行的通过率、总费用与平均耗时。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tightrein.domain.enums import RunnerStatus, ScoreResult
from tightrein.evaluation.scorers.base import ItemResult
from tightrein.runner.result import Usage


@dataclass(frozen=True)
class RunScore:
    case_id: str
    variant: str
    attempt: int
    status: RunnerStatus
    items: list[ItemResult]
    score: float
    passed: bool
    usage: Usage
    duration_ms: int
    output_dir: Path
    failure: str | None = None
    transcripts: tuple[str, ...] = ()

    @property
    def key(self) -> tuple[str, str, int]:
        return self.variant, self.case_id, self.attempt

    def to_dict(self) -> dict[str, Any]:
        return {"caseId": self.case_id, "variant": self.variant, "attempt": self.attempt, "status": self.status.value,
                "items": [item.to_dict() for item in self.items], "score": self.score, "passed": self.passed,
                "usage": self.usage.to_dict(), "durationMs": self.duration_ms, "outputDir": str(self.output_dir),
                "failure": self.failure, "transcripts": list(self.transcripts)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RunScore:
        return cls(data["caseId"], data["variant"], data["attempt"], RunnerStatus(data["status"]),
                   [ItemResult.from_dict(item) for item in data["items"]], data["score"], data["passed"],
                   Usage.from_dict(data["usage"]), data["durationMs"], Path(data["outputDir"]), data["failure"],
                   tuple(data["transcripts"]))


@dataclass(frozen=True)
class CaseStats:
    case_id: str
    variant: str
    scores: list[float]
    mean: float
    variance: float
    pass_rate: float
    item_pass_counts: dict[str, tuple[int, int]]
    unstable: bool

    def to_dict(self) -> dict[str, Any]:
        return {"caseId": self.case_id, "variant": self.variant, "scores": self.scores, "mean": self.mean,
                "variance": self.variance, "passRate": self.pass_rate,
                "itemPassCounts": {key: list(value) for key, value in self.item_pass_counts.items()},
                "unstable": self.unstable}


@dataclass(frozen=True)
class VariantStats:
    variant: str
    mean: float
    pass_rate: float
    cost_usd: float | None
    duration_ms: float | None

    def to_dict(self) -> dict[str, Any]:
        return {"variant": self.variant, "mean": self.mean, "passRate": self.pass_rate, "costUsd": self.cost_usd,
                "durationMs": self.duration_ms}


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def sample_variance(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    center = mean(values)
    return sum((value - center) ** 2 for value in values) / (len(values) - 1)


def item_pass_counts(runs: Sequence[RunScore]) -> dict[str, tuple[int, int]]:
    counts: dict[str, list[int]] = {}
    for run in runs:
        for item in run.items:
            if item.result is ScoreResult.NOT_APPLICABLE:
                continue
            passes, applicable = counts.setdefault(item.item_id, [0, 0])
            counts[item.item_id] = [passes + (item.result is ScoreResult.PASS), applicable + 1]
    return {key: (value[0], value[1]) for key, value in counts.items()}


def case_stats(runs: Sequence[RunScore], variants: Sequence[str], case_ids: Sequence[str]) -> list[CaseStats]:
    """按用例、再按变体的顺序；没有运行记录的组合不出现。"""
    found = []
    for case_id in case_ids:
        for variant in variants:
            group = sorted((run for run in runs if run.case_id == case_id and run.variant == variant),
                           key=lambda run: run.attempt)
            if not group:
                continue
            scores = [run.score for run in group]
            passed = [run.passed for run in group]
            found.append(CaseStats(case_id, variant, scores, mean(scores), sample_variance(scores),
                                   sum(passed) / len(group), item_pass_counts(group), any(passed) and not all(passed)))
    return found


def variant_stats(runs: Sequence[RunScore], stats: Sequence[CaseStats], variants: Sequence[str]) -> list[VariantStats]:
    found = []
    for variant in variants:
        group = [run for run in runs if run.variant == variant]
        means = [item.mean for item in stats if item.variant == variant]
        costs = [run.usage.cost_usd for run in group if run.usage.cost_usd is not None]
        found.append(VariantStats(
            variant, mean(means), sum(run.passed for run in group) / len(group) if group else 0.0,
            sum(costs) if costs else None, mean([run.duration_ms for run in group]) if group else None,
        ))
    return found
