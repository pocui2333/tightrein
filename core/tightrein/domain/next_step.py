"""对象状态到下一步模块的映射表(design 15.10)。编排层、cli 与测试共用，只在这里定义一次。"""

from __future__ import annotations

from dataclasses import dataclass, replace

from tightrein.domain.enums import Continuation, Disposition, IssuePhase, IssueStatus, ProblemStatus, Stage
from tightrein.domain.issue import Issue
from tightrein.domain.state_machine import Conditions, conditions_hold


@dataclass(frozen=True)
class NextStep:
    module: Stage | None
    command: str | None
    continuation: Continuation
    reason: str = ""

    @property
    def can_continue(self) -> bool:
        return self.continuation not in (Continuation.USER, Continuation.NONE)


@dataclass(frozen=True)
class ProblemProgress:
    """问题的当前进度：状态、最近一次分诊的去向、是否等待重新分诊，以及关联的 Issue。"""

    problem_id: str
    status: ProblemStatus
    disposition: Disposition | None = None
    retriage_requested: bool = False
    issue: Issue | None = None

    @property
    def has_issue(self) -> bool:
        return self.issue is not None


@dataclass(frozen=True)
class ProblemRow:
    statuses: frozenset[ProblemStatus]
    step: NextStep
    when: Conditions = ()


_TRIAGE = NextStep(Stage.TRIAGE, "triage", Continuation.AUTO)
_DONE = NextStep(None, None, Continuation.NONE)

PROBLEM_STEPS: tuple[ProblemRow, ...] = (
    ProblemRow(frozenset({ProblemStatus.NEW, ProblemStatus.ONGOING, ProblemStatus.REGRESSED, ProblemStatus.IGNORED}),
               _TRIAGE, when=(("retriage_requested", True),)),
    ProblemRow(frozenset({ProblemStatus.PENDING}), NextStep(Stage.AGGREGATE, "aggregate", Continuation.AUTO)),
    ProblemRow(frozenset({ProblemStatus.REGRESSED}), _TRIAGE),
    ProblemRow(frozenset({ProblemStatus.NEW}),
               NextStep(Stage.TRIAGE, "triage queue", Continuation.USER, "人工队列：等用户补充信息或改判"),
               when=(("disposition", Disposition.MANUAL_QUEUE),)),
    ProblemRow(frozenset({ProblemStatus.NEW}), _TRIAGE),
    ProblemRow(frozenset({ProblemStatus.ONGOING}), NextStep(Stage.ISSUE, "issue create", Continuation.AUTO),
               when=(("disposition", Disposition.CREATE_ISSUE), ("has_issue", False))),
    ProblemRow(frozenset({ProblemStatus.ONGOING}), NextStep(Stage.AGGREGATE, "aggregate", Continuation.WAIT_DEPLOY),
               when=(("disposition", Disposition.AWAITING_DEPLOY),)),
    ProblemRow(frozenset({ProblemStatus.RESOLVED, ProblemStatus.IGNORED, ProblemStatus.ONGOING}),
               _DONE),
)

ISSUE_STEPS: dict[tuple[IssueStatus, IssuePhase | None], NextStep] = {
    (IssueStatus.NEEDS_DECISION, None): NextStep(Stage.ISSUE, "issue approve", Continuation.USER, "等用户决定是否放行"),
    (IssueStatus.TODO, None): NextStep(Stage.FIX, "fix start", Continuation.INTERACTIVE),
    (IssueStatus.IN_PROGRESS, IssuePhase.FIX): NextStep(Stage.FIX, "fix start", Continuation.INTERACTIVE),
    (IssueStatus.IN_PROGRESS, IssuePhase.VERIFY): NextStep(Stage.VERIFY, "verify local", Continuation.AUTO),
    (IssueStatus.IN_PROGRESS, IssuePhase.SUBMIT): NextStep(Stage.RELEASE, "release", Continuation.CONFIRM_EACH),
    (IssueStatus.PENDING_MERGE, None): NextStep(None, None, Continuation.USER, "等用户在 GitHub 上审核 PR"),
    (IssueStatus.DONE, IssuePhase.DEPLOY_CHECK): NextStep(Stage.VERIFY, "verify staging", Continuation.WAIT_DEPLOY),
    (IssueStatus.DONE, None): _DONE,
    (IssueStatus.CANCELLED, None): _DONE,
}


MANUAL_APPROVE = NextStep(Stage.ISSUE, "issue approve", Continuation.USER, "用户需求：放行并申请建修复分支")


def for_issue(issue: Issue) -> NextStep:
    """待决定且带 hold 的 Issue 先向用户说明原因，确认后以 fix start --force 继续；还没有修复分支的用户需求先
    issue approve，拆分出的后续子任务另写明排在哪个 Issue 之后。"""
    step = ISSUE_STEPS[(issue.status, issue.phase)]
    if issue.is_manual and issue.status is IssueStatus.TODO and issue.branch is None:
        step = MANUAL_APPROVE
        if issue.depends_on is not None:
            step = replace(step, reason=f"拆分出的后续子任务：排在 Issue {issue.depends_on} 之后，它合并后再放行")
    if issue.hold is not None:
        return NextStep(Stage.FIX, "fix start", Continuation.USER, f"待决定：{issue.hold.reason}")
    return step


def for_problem(progress: ProblemProgress) -> NextStep:
    """持续状态且已有 Issue 的问题跟随 Issue 的下一步；其余按 PROBLEM_STEPS 自上而下取第一条匹配。"""
    if progress.issue is not None and progress.status is ProblemStatus.ONGOING and not progress.retriage_requested:
        return for_issue(progress.issue)
    for row in PROBLEM_STEPS:
        if progress.status in row.statuses and conditions_hold(row.when, progress):
            return row.step
    raise ValueError(f"问题状态 {progress.status.value} 不在映射表中")


def next_step(subject: ProblemProgress | Issue) -> NextStep:
    if isinstance(subject, Issue):
        return for_issue(subject)
    return for_problem(subject)
