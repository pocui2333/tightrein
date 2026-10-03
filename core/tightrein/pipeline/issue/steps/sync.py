"""Issue 与问题状态的同步、审阅超时提醒(architecture/06 10.5)，每次定时运行执行一次。

1. 以 Issue 文件为准重建索引；有不合格的文件时整体不更新索引，文件与错误列入结果；
2. 完成或取消的 Issue 的关联问题处于回归时重新打开 Issue，「历史」写明回归时的 commit；
3. 以已修复关闭的 Issue，关联问题最近一次分诊结论的实际结果为空时回填判对；
4. 待放行(待决定且没有 hold)超过 thresholds.issue.reviewReminderWorkdays 个工作日的列入结果。
issues.tracker 为 github 时，IssueService.sync 在这之后对齐 GitHub 镜像，结果写进 github。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tightrein.domain.clock import local_date, workdays_between
from tightrein.domain.enums import IssueEvent, IssueStatus, ProblemStatus, TriageOutcome
from tightrein.pipeline.issue.steps import transitions
from tightrein.pipeline.issue.steps.github import MirrorReport
from tightrein.pipeline.issue.steps.transitions import IssueEnv
from tightrein.store.files import issue_files
from tightrein.store.files.issue_files import IssueFileError
from tightrein.store.repos import issues, problems, triage

ACTOR = "issue-sync"


@dataclass
class SyncReport:
    reopened: list[str] = field(default_factory=list)
    outcomes: list[str] = field(default_factory=list)
    overdue: list[str] = field(default_factory=list)
    reindexed: list[str] = field(default_factory=list)
    invalid: str | None = None
    github: MirrorReport | None = None


def sync(env: IssueEnv) -> SyncReport:
    report = SyncReport()
    try:
        report.reindexed = list(issue_files.reindex(env.conn, env.layout).updated)
    except IssueFileError as error:
        report.invalid = str(error)
    today = local_date(env.clock.now(), env.zone)
    limit = env.config.whole_threshold("issue.reviewReminderWorkdays")
    for record in issues.find(env.conn):
        issue = record.issue
        if issue.is_closed:
            regressed = [problem for problem in (problems.get(env.conn, pid) for pid in issue.problems)
                         if problem is not None and problem.status is ProblemStatus.REGRESSED]
            if regressed:
                note = "；".join(f"关联问题 {item.id} 回归(commit {item.last_seen_release or '未知'})"
                                for item in regressed)
                transitions.apply_event(env, record, IssueEvent.PROBLEM_REGRESSED, actor=ACTOR, note=note)
                report.reopened.append(issue.id)
                continue
            if issue.status is IssueStatus.DONE and issue.phase is None:
                for problem_id in issue.problems:
                    latest = triage.latest(env.conn, problem_id)
                    if latest is not None and latest.result.outcome is None:
                        triage.set_outcome(env.conn, problem_id, latest.result.attempt, TriageOutcome.CORRECT,
                                           env.clock.now())
                        report.outcomes.append(problem_id)
        elif issue.status is IssueStatus.NEEDS_DECISION and issue.hold is None:
            waited = workdays_between(local_date(issue.created_at, env.zone), today, env.config.non_working_days())
            if waited > limit:
                report.overdue.append(issue.id)
    return report
