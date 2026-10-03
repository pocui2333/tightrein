"""评级(architecture/06 4.10，redesign/03-triage.md)：严重度、规模档与复杂度，全部由 domain 的纯函数计算。

- 严重度取取证输出 report.severity(取证角色按分诊规则 references/severity.md 与项目的 triage.severityGuide 给出)；
  没有报告时按 impact.kind 映射；没有影响面(不成立、证据不足)时，预估为 P0 的仍记 P0，其余为空；
- 预估改动取取证评估 assessment.estimate(不含测试文件)，没有时取根因文件、行数记 0；规模档按 thresholds.tiers；
- 复杂度在取证前按问题本身估计(决定上限)，取证后按预估文件数重新计算一次写入结论。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from tightrein.config.project import ProjectConfig
from tightrein.domain import sizing
from tightrein.domain.enums import Complexity, ImpactKind, Probe, Severity, SizeTier, TaskType
from tightrein.domain.problem import Problem
from tightrein.domain.sizing import ComplexityHint
from tightrein.domain.signal import Signal
from tightrein.domain.triage import severity
from tightrein.orchestrator.policy.lanes import tier_of


@dataclass(frozen=True)
class Rating:
    severity: Severity | None
    complexity: Complexity
    tier: SizeTier | None
    task_type: TaskType | None
    estimated_files: tuple[str, ...]
    estimate: Mapping[str, Any] | None = None


def hint(problem: Problem, latest: Signal | None, p0: bool, estimated_files: int = 1) -> ComplexityHint:
    frames = latest.ctx("projectFrames") if latest is not None else None
    return ComplexityHint(touches_authorization=p0, has_stack=bool(frames),
                          location_is_code=problem.probe in (Probe.STATIC, Probe.INCIDENTAL),
                          estimated_files=estimated_files)


def complexity(config: ProjectConfig, value: ComplexityHint) -> Complexity:
    return sizing.complexity(value, config.whole_threshold("triage.complexityFiles"))


def impact_kind(outputs: Mapping[str, Any]) -> ImpactKind | None:
    impact = (outputs.get("evidence") or {}).get("impact")
    return ImpactKind(impact["kind"]) if impact else None


def rate(config: ProjectConfig, problem: Problem, latest: Signal | None, outputs: Mapping[str, Any], *,
         p0: bool) -> Rating:
    kind = impact_kind(outputs)
    assessed = (outputs.get("report") or {}).get("severity")
    if assessed:
        level: Severity | None = Severity(assessed)
    else:
        level = severity(kind) if kind is not None else (Severity.P0 if p0 else None)
    assessment = outputs.get("assessment")
    if assessment is not None:
        estimate = assessment["estimate"]
        files = tuple(dict.fromkeys(item["path"] for item in estimate["files"]))
        lines = estimate["lines"]
    else:
        estimate = None
        files = tuple(dict.fromkeys(cause["file"] for cause in outputs.get("rootCauses") or []))
        lines = 0
    tier = tier_of(config, len(files), lines) if assessment is not None or files else None
    rated = complexity(config, hint(problem, latest, p0, max(len(files), 1)))
    task_type = TaskType(assessment["taskType"]) if assessment is not None else None
    return Rating(level, rated, tier, task_type, files, estimate)
