"""由分诊结论生成 Issue 的元数据，以及对写好的 Issue 文件做「提 Issue 与报告」一项的代码检查(architecture/06 10.1)。

frontmatter 的 schema 校验在 store.files.issue_files 写入时进行；这里的检查按评分表 evaluation/rubrics/issue.json：
必需章节齐全、「证据」一节每条带真实存在的位置、日期为绝对日期。没有代码快照时位置一项为 unknown。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from tightrein.domain.clock import Clock
from tightrein.domain.enums import IssueStatus, Severity, SizeTier, Stage, TaskType, Treatment
from tightrein.domain.issue import Issue
from tightrein.domain.problem import Problem
from tightrein.evaluation.scorers.base import ItemResult, ScoringContext
from tightrein.evaluation.scoring import score_output
from tightrein.store.repos.triage import introduced_by_from


def issue_for(issue_id: str, slug: str, title: str, problem: Problem, outputs: Mapping[str, Any], now: datetime,
              findings: str, source: str | None = None) -> Issue:
    if outputs.get("severity") is None:
        raise ValueError(f"{problem.id} 的分诊结论没有严重度，不能提 Issue")
    introduced = outputs.get("introducedBy") or []
    return Issue(
        id=issue_id, slug=slug, title=title, status=IssueStatus.NEEDS_DECISION,
        severity=Severity(outputs["severity"]), created_at=now, updated_at=now,
        treatment=Treatment(outputs["treatment"]) if outputs.get("treatment") else None,
        task_type=TaskType(outputs["taskType"]) if outputs.get("taskType") else None,
        size_tier=SizeTier(outputs["sizeTier"]) if outputs.get("sizeTier") else None, problems=(problem.id,),
        root_cause=tuple(f"{cause['file']}:{cause['line']}" for cause in outputs.get("rootCauses") or []),
        introduced_by=introduced_by_from(introduced[0]) if introduced else None,
        triage_commit=outputs.get("triageCommit"), findings=findings, source=source,
    )


def check(root: Path, relative: str, snapshot: Path | None, clock: Clock) -> list[ItemResult]:
    context = ScoringContext(project_snapshot=snapshot, output_dir=root)
    return score_output(Stage.ISSUE, {"outputs": {"path": relative}}, context, None, clock)
