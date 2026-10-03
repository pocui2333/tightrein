"""落库与交接文档的 outputs(architecture/06 4.12、5)。

一个问题的落库在一个事务内完成：triage_results(新的 attempt)、scores、处理过的 retriage-requested 事件的
handled_at，以及经 ChangeSet 与 apply.commit 写入的问题状态、problem_events(detail.context 为转换所用的上下文，
整体重放按它重新施加)、合并与抑制规则(suppressions.yaml 由带文件锁的写入函数追加)。事务失败时全部回滚，问题视为
未处理。合并(查重)不写 triage_results，交接文档的判定与去向为空。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from typing import Any

from tightrein.config.project import ProjectConfig
from tightrein.domain.clock import Clock
from tightrein.domain.enums import ProblemEvent, Stage
from tightrein.domain.problem import Problem, ProblemContext
from tightrein.domain.triage import IntroducedBy, RootCause, TriageFlags, TriageResult
from tightrein.evaluation.scorers.base import ItemResult
from tightrein.pipeline.aggregate.changeset import ChangeSet
from tightrein.pipeline.aggregate.steps import apply
from tightrein.pipeline.triage.steps.case import TriageCase
from tightrein.pipeline.triage.steps.disposition import Decision
from tightrein.store.db import transaction
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.repos import problem_events, problems, scores, triage
from tightrein.store.repos.problem_events import OPERATION_AUTO
from tightrein.store.repos.scores import ScoreRecord
from tightrein.store.repos.triage import introduced_by_dict

FLAG_KEYS = (("design", "design"), ("dataStructure", "data_structure"), ("publicContract", "public_contract"))


def root_causes(case: TriageCase) -> tuple[RootCause, ...]:
    return tuple(RootCause(item["file"], item["line"], item.get("symbol")) for item in case.outputs.get("rootCauses")
                 or [])


def assessment(case: TriageCase) -> dict[str, Any] | None:
    """证据检查通过的取证评估；没有时为空。"""
    if case.evidence is None or not case.evidence.passed:
        return None
    return case.outputs.get("assessment")


def flags(case: TriageCase) -> dict[str, Any] | None:
    found = assessment(case)
    return None if found is None else dict(found["flags"])


def worth(case: TriageCase) -> dict[str, Any] | None:
    found = assessment(case)
    if found is None:
        return None
    return {"recommendation": found["worth"], "reason": found["worthReason"], "direction": found["direction"],
            "reevaluateWhen": found["reevaluateWhen"]}


def scope(case: TriageCase) -> dict[str, Any] | None:
    """取证给出的范围：明确不做的部分与必须保持不变的文件、接口与行为(Issue「范围」「注意事项」)。"""
    found = assessment(case)
    if found is None:
        return None
    return {"outOfScope": list(found.get("outOfScope") or []), "mustKeep": list(found.get("mustKeep") or [])}


def triage_flags(case: TriageCase) -> TriageFlags:
    found = flags(case) or {}
    return TriageFlags(**{name: bool(found.get(key, {}).get("flagged")) for key, name in FLAG_KEYS})


def score_items(items: Sequence[ItemResult]) -> list[dict[str, Any]]:
    return [{"itemId": item.item_id, "result": item.result.value, "method": item.method.value,
             "reason": item.reason} for item in items]


def score_records(case: TriageCase, run_id: str, clock: Clock) -> list[ScoreRecord]:
    items = case.evidence.items if case.evidence is not None else []
    return [ScoreRecord(Stage.TRIAGE, run_id, case.problem.id, case.attempt, item.item_id, item.result, item.method,
                        clock.now(), item.reason) for item in items]


def handoff_outputs(case: TriageCase, decision: Decision | None, reason: str, triage_commit: str) -> dict[str, Any]:
    outputs = case.outputs
    evidence_output = case.evidence.output if case.evidence is not None else None
    rating = case.rating
    treatment = None if decision is None else decision.treatment
    return {
        "problemId": case.problem.id,
        "claim": case.claim.to_dict(),
        "verdict": None if decision is None or case.verdict is None else case.verdict.value,
        "severity": None if rating is None or rating.severity is None else rating.severity.value,
        "complexity": (rating.complexity if rating is not None else case.complexity).value,
        "rootCauses": list(outputs.get("rootCauses") or []),
        "introducedBy": [introduced_by_dict(item) for item in case.attribution.introduced_by],
        "disposition": None if decision is None else decision.disposition.value,
        "reason": reason,
        "triageCommit": triage_commit,
        "refuterVerdict": None if case.refuter_verdict is None else case.refuter_verdict.value,
        "treatment": None if treatment is None else treatment.value,
        "taskType": None if rating is None or rating.task_type is None else rating.task_type.value,
        "sizeTier": None if rating is None or rating.tier is None else rating.tier.value,
        "estimate": None if rating is None or rating.estimate is None else dict(rating.estimate),
        "flags": flags(case),
        "labels": [] if decision is None else [label.value for label in decision.labels],
        "evidence": outputs.get("evidence"),
        "worth": worth(case),
        "scope": scope(case),
        "fixedOnMain": None if evidence_output is None else evidence_output.get("fixedOnMain"),
        "report": outputs.get("report"),
        "tradeoffHit": case.tradeoff_hit,
        "mergedInto": case.merged_into,
        "missingInfo": list(outputs.get("missingInfo") or []),
        "incidentalFindings": [] if evidence_output is None else list(evidence_output.get("incidental") or []),
        "scores": score_items(case.evidence.items if case.evidence is not None else []),
        "attempts": [{"role": role, "statuses": [status.value for status in statuses]}
                     for role, statuses in case.attempts.items()],
    }


def triage_record(case: TriageCase, decision: Decision, reason: str, triage_commit: str, run_id: str,
                  clock: Clock) -> triage.TriageRecord:
    rating = case.rating
    introduced: IntroducedBy | None = case.attribution.introduced_by[0] if case.attribution.introduced_by else None
    if case.verdict is None:
        raise ValueError(f"问题 {case.problem.id} 没有判定，不能写分诊结论")
    result = TriageResult(
        problem_id=case.problem.id, attempt=case.attempt, verdict=case.verdict, disposition=decision.disposition,
        reason=reason, triage_commit=triage_commit, severity=None if rating is None else rating.severity,
        complexity=rating.complexity if rating is not None else case.complexity, root_causes=root_causes(case),
        introduced_by=introduced, refuter_verdict=case.refuter_verdict, flags=triage_flags(case),
        labels=decision.labels, treatment=decision.treatment, task_type=None if rating is None else rating.task_type,
        size_tier=None if rating is None else rating.tier)
    return triage.TriageRecord(result, run_id, clock.now())


def commit(conn: sqlite3.Connection, layout: WorkspaceLayout, clock: Clock, config: ProjectConfig, *, run_id: str,
           problem_id: str, event: ProblemEvent, context: ProblemContext, reason: str,
           record: triage.TriageRecord | None = None, score_rows: Sequence[ScoreRecord] = (),
           handled: Sequence[int] = (), operation: str = OPERATION_AUTO) -> Problem:
    current = problems.get(conn, problem_id)
    if current is None:
        raise LookupError(f"没有问题 {problem_id}")
    changeset = ChangeSet.start(conn, run_id, clock.now(), config.whole_threshold("suppressionDays"))
    changeset.transition(current, event, context, operation=operation, reason=reason)
    with transaction(conn):
        if record is not None:
            triage.save(conn, record)
        for row in score_rows:
            scores.append(conn, row)
        for event_id in handled:
            problem_events.mark_handled(conn, event_id, clock.now())
        apply.commit(conn, changeset, clock, layout, side_effects=True)
    return problems.get(conn, problem_id) or current


def merge(conn: sqlite3.Connection, layout: WorkspaceLayout, clock: Clock, config: ProjectConfig, *, run_id: str,
          problem_id: str, target: str, reason: str, handled: Sequence[int] = ()) -> Problem:
    return commit(conn, layout, clock, config, run_id=run_id, problem_id=problem_id, event=ProblemEvent.MERGED,
                  context=ProblemContext(merge_target=target), reason=reason, handled=handled)
