"""出问题的来源(redesign/08-learn.md 第 1 节)：经验总结与自我改进只从这里取材料，不从正常完成的运行中取。

| 类型 | 来源 | 幂等键(经验) |
|---|---|---|
| triage-misjudged | 分诊结论的实际结果为误判为成立、误判为不成立或用户改判(改判记录的上一次结论已判为误判时由那一条负责) | lesson:triage:<问题>:<次数> |
| review-rejected | PR 上要求修改(CHANGES_REQUESTED)的评审，同一 Issue 合为一条 | lesson:pr:<Issue>:<评审> |
| review-rejected | 修复评审不通过的轮次 | lesson:review:<Issue>:<运行>-<轮次> |
| fix-failed | Issue 事件「修复转人工」 | lesson:fix-held:<Issue>:<事件编号> |
| reverted | 撤销合并的 PR 操作(原因取发布交接文档) | lesson:revert:<Issue>:<操作编号> |
| plan-rejected | 用户拒绝的修复计划(用户的说明) | lesson:plan:<Issue>:<操作编号> |
| issue-closed | 用户关闭 Issue(关闭原因与说明) | lesson:closed:<Issue>:<事件编号> |

分诊误判写成分诊经验，其余写成修复经验。since 给出时只取该时间及以后发生的。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from tightrein.domain.enums import (
    IssueEvent,
    KnowledgeType,
    OperationKind,
    OperationStatus,
    RunStage,
    TriageOutcome,
)
from tightrein.domain.triage import TriageResult
from tightrein.pipeline.common import stage_runs
from tightrein.store.files import handoff_files
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.repos import handoffs, issue_events, issues, pending_operations, problems, pulls, triage

MISJUDGED = (TriageOutcome.FALSE_CONFIRM, TriageOutcome.FALSE_REFUTE)
TRIAGE_SOURCES = (*MISJUDGED, TriageOutcome.OVERRIDDEN)
CHANGES_REQUESTED = "CHANGES_REQUESTED"
TRIAGE_MISJUDGED = "triage-misjudged"
REVIEW_REJECTED = "review-rejected"
FIX_FAILED = "fix-failed"
REVERTED = "reverted"
PLAN_REJECTED = "plan-rejected"
ISSUE_CLOSED = "issue-closed"
LABELS = {TRIAGE_MISJUDGED: "分诊误判", REVIEW_REJECTED: "评审驳回", FIX_FAILED: "修复失败", REVERTED: "被撤销",
          PLAN_REJECTED: "用户驳回计划", ISSUE_CLOSED: "用户关闭 Issue"}


@dataclass(frozen=True)
class Trouble:
    kind: str
    keys: tuple[str, ...]
    subject_type: str
    subject_id: str
    title: str
    text: str
    at: datetime | None
    run_id: str | None = None

    @property
    def knowledge_type(self) -> KnowledgeType:
        return KnowledgeType.TRIAGE_LESSON if self.kind == TRIAGE_MISJUDGED else KnowledgeType.FIX_LESSON

    @property
    def label(self) -> str:
        return LABELS[self.kind]

    def to_dict(self) -> dict[str, object]:
        return {"kind": self.kind, "subject": self.subject_id, "title": self.title}


def _conclusion(result: TriageResult) -> str:
    return f"{result.verdict.label}，去向 {result.disposition.label}，理由：{result.reason}"


def _issue_title(conn: sqlite3.Connection, issue_id: str) -> str:
    record = issues.get(conn, issue_id)
    return record.issue.title if record is not None else issue_id


def misjudged(conn: sqlite3.Connection) -> list[Trouble]:
    marks = ", ".join("?" for _ in TRIAGE_SOURCES)
    rows = conn.execute(f"SELECT problem_id, attempt, run_id FROM triage_results WHERE outcome IN ({marks}) "
                        "ORDER BY outcome_at, problem_id, attempt", [item.value for item in TRIAGE_SOURCES]).fetchall()
    found = []
    for row in rows:
        records = {item.result.attempt: item for item in triage.for_problem(conn, row["problem_id"])}
        current = records[row["attempt"]].result
        outcome = TriageOutcome(current.outcome)
        if outcome is TriageOutcome.OVERRIDDEN:
            previous = records.get(current.attempt - 1)
            if previous is None or previous.result.outcome in MISJUDGED:
                continue
            before, after, run_id = previous.result, current, previous.run_id
        else:
            later = [item.result for number, item in sorted(records.items()) if number > current.attempt]
            before, after, run_id = current, (later[0] if later else None), row["run_id"]
        problem = problems.get(conn, row["problem_id"])
        title = problem.title if problem is not None else ""
        lines = [f"问题 {row['problem_id']}：{title}", f"原结论：{_conclusion(before)}", f"实际结果：{outcome.label}"]
        if after is not None:
            lines.append(f"改判后的结论：{_conclusion(after)}")
        at = records[row["attempt"]].outcome_at
        found.append(Trouble(TRIAGE_MISJUDGED, (f"lesson:triage:{row['problem_id']}:{row['attempt']}",), "problem",
                             row["problem_id"], title, "\n".join(lines), at, run_id))
    return found


def _rejected_reviews(conn: sqlite3.Connection) -> list[Trouble]:
    found = []
    for pull in pulls.find(conn):
        keys, lines = [], []
        for index, review in enumerate(pull.reviews, start=1):
            if str(review.get("state") or "").upper() != CHANGES_REQUESTED:
                continue
            author = (review.get("author") or {}).get("login", "评审者")
            keys.append(f"lesson:pr:{pull.issue_id}:{review.get('id') or index}")
            lines.append(f"- {author} 要求修改：{str(review.get('body') or '').strip() or '(没有正文)'}")
        if keys:
            text = "\n".join([f"PR #{pull.number}：{pull.title}", *lines])
            found.append(Trouble(REVIEW_REJECTED, tuple(keys), "issue", pull.issue_id, pull.title, text,
                                 pull.closed_at or pull.created_at))
    return found


def _failed_rounds(conn: sqlite3.Connection, layout: WorkspaceLayout) -> list[Trouble]:
    """修复交接文档中评审不通过的轮次(阻断项)；同一轮在多份交接文档中出现时只取一次。"""
    found: dict[str, Trouble] = {}
    for record in handoffs.TABLE.find(conn, stage=RunStage.FIX):
        outputs = handoff_files.read(layout.root / record.path)["outputs"]
        for item in outputs.get("rounds") or []:
            failed = [review["mode"] for review in item.get("reviews", []) if review.get("passed") is False]
            if not failed:
                continue
            key = f"lesson:review:{record.subject_id}:{record.run_id}-{item['round']}"
            problems_ = [f"- {failure.get('location') or ''} {failure.get('problem') or ''}".rstrip()
                         for failure in item.get("failures", [])]
            text = "\n".join([f"Issue {record.subject_id} 修复第 {item['round']} 轮的评审({'、'.join(failed)})不通过：",
                              *problems_])
            title = _issue_title(conn, record.subject_id)
            found.setdefault(key, Trouble(REVIEW_REJECTED, (key,), "issue", record.subject_id, title, text,
                                          record.created_at, record.run_id))
    return list(found.values())


def _issue_events(conn: sqlite3.Connection, event: IssueEvent, kind: str, prefix: str) -> list[Trouble]:
    found = []
    for record in issue_events.TABLE.find(conn, event=event.value):
        title = _issue_title(conn, record.issue_id)
        reason = f"，关闭原因：{record.close_reason.label}" if record.close_reason is not None else ""
        text = f"Issue {record.issue_id}「{title}」{event.label}{reason}；说明：{record.note or '(没有说明)'}"
        found.append(Trouble(kind, (f"lesson:{prefix}:{record.issue_id}:{record.id}",), "issue", record.issue_id, title,
                             text, record.at))
    return found


def _operations(conn: sqlite3.Connection, layout: WorkspaceLayout) -> list[Trouble]:
    found = []
    for record in pending_operations.find(conn):
        title = _issue_title(conn, record.subject_id)
        if record.kind is OperationKind.REVERT_PULL_REQUEST and record.status is not OperationStatus.REJECTED:
            latest = stage_runs.latest_outputs(conn, layout, RunStage.RELEASE, record.subject_id)
            reason = ((latest[1].get("revert") or {}).get("reason") if latest is not None else None) or record.impact
            found.append(Trouble(REVERTED, (f"lesson:revert:{record.subject_id}:{record.id}",), "issue",
                                 record.subject_id, title, f"Issue {record.subject_id}「{title}」的合并被撤销：{reason}",
                                 record.created_at))
        elif record.kind is OperationKind.FIX_PLAN and record.status is OperationStatus.REJECTED:
            note = (record.result or {}).get("note") or "(没有说明)"
            found.append(Trouble(PLAN_REJECTED, (f"lesson:plan:{record.subject_id}:{record.id}",), "issue",
                                 record.subject_id, title, f"Issue {record.subject_id}「{title}」的修复计划被用户驳回：{note}",
                                 record.decided_at))
    return found


def collect(conn: sqlite3.Connection, layout: WorkspaceLayout, since: datetime | None = None) -> list[Trouble]:
    found = [*misjudged(conn), *_rejected_reviews(conn), *_failed_rounds(conn, layout),
             *_issue_events(conn, IssueEvent.FIX_HELD, FIX_FAILED, "fix-held"), *_operations(conn, layout),
             *_issue_events(conn, IssueEvent.USER_CLOSED, ISSUE_CLOSED, "closed")]
    return [item for item in found if since is None or (item.at is not None and item.at >= since)]
