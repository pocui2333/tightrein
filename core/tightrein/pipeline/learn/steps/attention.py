"""周报「需要处理的事项」(architecture/08 5.2)：每一项给出对象编号与处理命令。

本周自动判为误报的问题全部列出，供用户抽查(design 3.6)；回归两次及以上的 Issue 单独列出(design 8.2)。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import timedelta

from tightrein.domain.clock import local_date
from tightrein.domain.enums import Disposition, IssueEvent, IssueStatus, SuggestionStatus
from tightrein.pipeline.learn.steps.metrics import MetricContext, manual_queue_items
from tightrein.pipeline.learn.steps.weeks import workdays
from tightrein.store.files import suppressions
from tightrein.store.repos import issue_events, issues, pulls, suggestions, triage

OPEN = "OPEN"
MERGEABLE = "MERGEABLE"
REGRESSIONS_TO_LIST = 2


@dataclass(frozen=True)
class Attention:
    kind: str
    subject_id: str
    summary: str
    command: str

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "subjectId": self.subject_id, "summary": self.summary, "command": self.command}


def _review_waits(ctx: MetricContext) -> list[Attention]:
    limit = ctx.config.whole_threshold("issue.reviewReminderWorkdays")
    found = []
    for record in issues.find(ctx.conn, status=IssueStatus.NEEDS_DECISION):
        if record.issue.hold is not None:
            continue
        entered = [event.at for event in issue_events.for_issue(ctx.conn, record.issue.id)
                   if event.to_status is IssueStatus.NEEDS_DECISION]
        waited = workdays(max(entered, default=record.issue.created_at), ctx.now, ctx.config, ctx.zone)
        if waited > limit:
            found.append(Attention("issue-review", record.issue.id,
                                   f"「{record.issue.title}」等待放行 {waited} 个工作日",
                                   f"tightrein approve {record.issue.id}"))
    return found


def _pull_items(ctx: MetricContext) -> list[Attention]:
    limit = ctx.config.whole_threshold("release.prReminderWorkdays")
    found = []
    for pull in pulls.find(ctx.conn, state=OPEN):
        waited = workdays(pull.created_at, ctx.now, ctx.config, ctx.zone)
        if waited > limit:
            found.append(Attention("pr-review", pull.issue_id, f"PR #{pull.number} 等待审核 {waited} 个工作日",
                                   pull.url))
        if pull.mergeable == MERGEABLE:
            found.append(Attention("pr-mergeable", pull.issue_id, f"PR #{pull.number} 可以合并", pull.url))
    return found


def _manual_queue(ctx: MetricContext) -> list[Attention]:
    return [Attention("manual-queue", row["problem_id"], f"人工队列中，{row['created_at']} 起等待处理",
                      f"tightrein problem retriage {row['problem_id']} --note <补充信息>")
            for row in manual_queue_items(ctx.conn)]


def _expiring_suppressions(ctx: MetricContext) -> list[Attention]:
    today = local_date(ctx.now, ctx.zone)
    until = today + timedelta(days=ctx.config.whole_threshold("learn.suppressionNoticeDays"))
    found = []
    for rule in suppressions.read(ctx.layout.suppressions()):
        if today <= rule.expires_on <= until:
            subject = rule.fingerprint or f"{rule.probe.value if rule.probe else ''}:{rule.message_pattern}"
            found.append(Attention("suppression-expiring", subject,
                                   f"抑制规则将于 {rule.expires_on.isoformat()} 到期：{rule.reason}",
                                   f"编辑 {ctx.layout.relative(ctx.layout.suppressions())}"))
    return found


def _false_positives(ctx: MetricContext) -> list[Attention]:
    found = []
    for record in triage.find(ctx.conn, disposition=Disposition.FALSE_POSITIVE):
        if ctx.window.contains(record.created_at):
            result = record.result
            found.append(Attention("false-positive", result.problem_id, f"自动判为误报：{result.reason}",
                                   f"tightrein problem retriage {result.problem_id} --verdict confirmed --reason <原因>"))
    return found


def regressed_issues(conn: sqlite3.Connection) -> dict[str, int]:
    """因关联问题回归而重新打开两次及以上的 Issue 与次数。"""
    rows = conn.execute("SELECT issue_id, COUNT(*) AS times FROM issue_events WHERE event = ? GROUP BY issue_id "
                        "HAVING COUNT(*) >= ? ORDER BY issue_id",
                        (IssueEvent.PROBLEM_REGRESSED.value, REGRESSIONS_TO_LIST))
    return {row["issue_id"]: row["times"] for row in rows}


def _regressions(ctx: MetricContext) -> list[Attention]:
    return [Attention("regressed-issue", issue_id, f"已回归 {times} 次，局部修复没有根治，需要考虑设计层面的问题",
                      f"tightrein issue show {issue_id}") for issue_id, times in regressed_issues(ctx.conn).items()]


def _pending_suggestions(ctx: MetricContext) -> list[Attention]:
    return [Attention("suggestion", record.id, f"{record.kind.label}：{record.subject}",
                      f"tightrein learn accept {record.id}")
            for record in suggestions.find(ctx.conn, status=SuggestionStatus.PENDING)]


def collect(ctx: MetricContext) -> list[Attention]:
    return [*_review_waits(ctx), *_pull_items(ctx), *_manual_queue(ctx), *_expiring_suppressions(ctx),
            *_false_positives(ctx), *_regressions(ctx), *_pending_suggestions(ctx)]
