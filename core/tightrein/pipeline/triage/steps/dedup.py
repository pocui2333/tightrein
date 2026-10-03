"""查重(architecture/06 4.4)：候选、执行器判断、代码检查与合并目标。

候选直接取自数据库：未关闭的 Issue，以及最近 days 天内分诊过、没有被合并的其他问题；只保留与本问题位置相关的
(问题的路由或页面相同，或根因文件出现在本问题的候选文件中)，按时间从新到旧取 limit 个。没有候选时不调用执行器。
判为同一根因时，target 必须是候选之一、依据位置必须真实存在；不通过的交回重做，仍不通过视为不同根因继续分诊。
target 为 Issue 时取该 Issue 的第一个问题作为合并目标。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from tightrein.domain.enums import RunnerStatus, ScoreResult
from tightrein.domain.problem import Problem
from tightrein.evaluation.scorers.base import ScoringContext
from tightrein.evaluation.scorers.code import REGISTRY
from tightrein.pipeline.triage.prompts import dedup as prompt
from tightrein.pipeline.triage.prompts.common import RoleCalls
from tightrein.pipeline.triage.prompts.dedup import Candidate
from tightrein.pipeline.triage.steps.claims import Claim
from tightrein.store.repos import issues, problems, triage

ISSUE = "issue"
PROBLEM = "problem"


@dataclass
class DedupOutcome:
    target: str | None = None
    statuses: list[RunnerStatus] = field(default_factory=list)
    note: str | None = None


def _file(location: str) -> str:
    return location.partition(":")[0]


def candidates(conn: sqlite3.Connection, problem: Problem, files: Sequence[str], now: datetime, days: int,
               limit: int) -> list[Candidate]:
    wanted = set(files)
    found: list[tuple[datetime, Candidate]] = []
    for record in issues.find(conn):
        issue = record.issue
        if issue.is_closed or problem.id in issue.problems:
            continue
        linked = [problems.get(conn, problem_id) for problem_id in issue.problems]
        same_place = any(item is not None and item.scope.location == problem.scope.location for item in linked)
        if same_place or wanted & {_file(cause) for cause in issue.root_cause}:
            found.append((issue.updated_at, Candidate(issue.id, ISSUE, issue.title, issue.root_cause,
                                                      issue.status.label)))
    latest: dict[str, triage.TriageRecord] = {}
    for record in triage.find(conn):
        if record.created_at >= now - timedelta(days=days):
            latest[record.result.problem_id] = record
    for problem_id, record in latest.items():
        other = problems.get(conn, problem_id)
        if other is None or other.id == problem.id or other.merged_into is not None or other.issue_id is not None:
            continue
        causes = tuple(f"{cause.file}:{cause.line}" for cause in record.result.root_causes)
        files_hit = wanted & {cause.file for cause in record.result.root_causes}
        if other.scope.location == problem.scope.location or files_hit:
            found.append((record.created_at, Candidate(other.id, PROBLEM, other.title, causes,
                                                       record.result.disposition.label)))
    found.sort(key=lambda item: (item[0], item[1].id), reverse=True)
    return [candidate for _, candidate in found[:limit]]


def check(output: Mapping[str, Any], found: Sequence[Candidate], worktree: Path) -> list[str]:
    if not output["sameRootCause"]:
        return []
    reasons = []
    if output["target"] not in {candidate.id for candidate in found}:
        reasons.append(f"target {output['target']} 不在候选中，只能是 {'、'.join(item.id for item in found)}")
    located = REGISTRY["evidence-locations"]({"evidence": list(output["evidence"])}, {"paths": ["evidence[*]"]},
                                             ScoringContext(project_snapshot=worktree))
    if located.result is ScoreResult.FAIL:
        reasons.append(f"依据位置不合格：{located.reason}")
    return reasons


def merge_target(conn: sqlite3.Connection, target: str) -> str | None:
    """候选编号对应的合并目标问题；Issue 取它的第一个问题。"""
    if target.startswith("P-"):
        return target
    record = issues.get(conn, target)
    return record.issue.problems[0] if record is not None and record.issue.problems else None


def decide(calls: RoleCalls, conn: sqlite3.Connection, problem: Problem, claim: Claim,
           found: Sequence[Candidate]) -> DedupOutcome:
    outcome = DedupOutcome()
    if not found:
        return outcome
    feedback: list[str] = []
    for attempt in range(1, calls.retries + 2):
        result = calls.run(prompt.task(calls.context, problem.id, claim, found, attempt, feedback))
        outcome.statuses.append(result.status)
        if result.status is not RunnerStatus.OK or result.output is None:
            feedback = [f"上一次执行没有完成：{result.status.value}"]
            continue
        feedback = check(result.output, found, calls.context.workdir)
        if not feedback:
            if result.output["sameRootCause"]:
                outcome.target = merge_target(conn, result.output["target"])
            return outcome
    outcome.note = "查重没有得到合格的判断，按不同根因继续分诊" + (f"：{'；'.join(feedback)}" if feedback else "")
    return outcome
