"""Issue 实体、Issue 状态机、待决定的原因 hold 与问题状态的同步规则(redesign/04-issue.md，design 4.7)。

六种状态：待决定、待修、进行中、待合并、完成、取消。进行中的细分(修复、合并前验证、提交)与完成后等待部署后确认记在
phase，只供程序推进；转换规则按 IssueContext.phase(当前 phase)匹配，经副作用 set-phase 写入新的 phase。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from tightrein.domain.clock import format_iso, parse_iso
from tightrein.domain.enums import (
    CloseReason,
    IssueEffect,
    IssueEvent,
    IssueOrigin,
    IssuePhase,
    IssueStatus,
    Severity,
    SizeTier,
    Stage,
    TaskType,
    Treatment,
    TriageOutcome,
)
from tightrein.domain.problem import Problem, ProblemContext, ignore_condition
from tightrein.domain.state_machine import Rule, SideEffect, apply
from tightrein.domain.triage import IntroducedBy


@dataclass(frozen=True)
class Hold:
    """转人工标记：不是状态，带有它的 Issue 不会被自动推进。"""

    reason: str
    stage: Stage
    since: datetime
    details: str = ""

    def __post_init__(self) -> None:
        if self.since.tzinfo is None:
            raise ValueError("since 必须带时区")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Hold":
        return cls(data["reason"], Stage(data["stage"]), parse_iso(data["since"]), data.get("details", ""))

    def to_dict(self) -> dict[str, Any]:
        return {"reason": self.reason, "stage": self.stage.value, "since": format_iso(self.since),
                "details": self.details}


@dataclass(frozen=True)
class GithubLink:
    """本地 Issue 在 GitHub 上的镜像(issues.tracker 为 github 时)：编号与链接。"""

    number: int
    url: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GithubLink":
        return cls(int(data["number"]), data["url"])

    def to_dict(self) -> dict[str, Any]:
        return {"number": self.number, "url": self.url}


@dataclass(frozen=True)
class Issue:
    id: str
    slug: str
    title: str
    status: IssueStatus
    severity: Severity
    created_at: datetime
    updated_at: datetime
    treatment: Treatment | None = None
    task_type: TaskType | None = None
    size_tier: SizeTier | None = None
    problems: tuple[str, ...] = ()
    root_cause: tuple[str, ...] = ()
    introduced_by: IntroducedBy | None = None
    triage_commit: str | None = None
    findings: str | None = None
    branch: str | None = None
    pr: str | None = None
    close_reason: CloseReason | None = None
    hold: Hold | None = None
    origin: IssueOrigin = IssueOrigin.TRIAGE
    github: GithubLink | None = None
    depends_on: str | None = None  # 拆分出的后续子任务：前一个子任务的 Issue，它合并后才可开始
    parent: str | None = None  # 拆分出的后续子任务：父 Issue
    phase: IssuePhase | None = None
    source: str | None = None  # 来源：探针与档位、用户需求或拆分自哪个 Issue

    def __post_init__(self) -> None:
        if self.created_at.tzinfo is None or self.updated_at.tzinfo is None:
            raise ValueError("时间必须带时区")
        if (self.status in CLOSED) != (self.close_reason is not None):
            raise ValueError("只有完成与取消的 Issue 带关闭原因，且这两种状态必须带关闭原因")
        if (self.status is IssueStatus.DONE) != (self.close_reason is CloseReason.FIXED):
            raise ValueError("完成的关闭原因只能是已修复，已修复只能是完成")
        if self.hold is not None and self.status is not IssueStatus.NEEDS_DECISION:
            raise ValueError("hold 只出现在待决定的 Issue 上")
        if self.phase is not None and self.phase not in PHASES[self.status]:
            raise ValueError(f"{self.status.label}的 Issue 不能处于「{self.phase.label}」")
        if self.status is IssueStatus.IN_PROGRESS and self.phase is None:
            raise ValueError("进行中的 Issue 须给出 phase")

    @property
    def is_manual(self) -> bool:
        """用户直接提出的需求：没有关联问题与信号，没有复现检查。"""
        return self.origin is IssueOrigin.MANUAL

    @property
    def is_closed(self) -> bool:
        return self.status in CLOSED


CLOSED = frozenset({IssueStatus.DONE, IssueStatus.CANCELLED})
OPEN = frozenset(IssueStatus) - CLOSED
PHASES: dict[IssueStatus, frozenset[IssuePhase]] = {
    **{status: frozenset() for status in IssueStatus},
    IssueStatus.IN_PROGRESS: frozenset({IssuePhase.FIX, IssuePhase.VERIFY, IssuePhase.SUBMIT}),
    IssueStatus.DONE: frozenset({IssuePhase.DEPLOY_CHECK}),
}
# 迁移前的八种状态(store/migrations/009_issue_status.sql 用同一映射)：旧状态 → (状态, phase)
LEGACY_STATUS: dict[str, tuple[IssueStatus, IssuePhase | None]] = {
    "in-review": (IssueStatus.NEEDS_DECISION, None),
    "todo": (IssueStatus.TODO, None),
    "fixing": (IssueStatus.IN_PROGRESS, IssuePhase.FIX),
    "to-verify": (IssueStatus.IN_PROGRESS, IssuePhase.VERIFY),
    "to-submit": (IssueStatus.IN_PROGRESS, IssuePhase.SUBMIT),
    "pr-review": (IssueStatus.PENDING_MERGE, None),
    "merged": (IssueStatus.DONE, IssuePhase.DEPLOY_CHECK),
}


def legacy_status(status: str, close_reason: CloseReason | None,
                  held: bool) -> tuple[IssueStatus, IssuePhase | None, CloseReason | None]:
    """旧 Issue 文件中的状态换算为 (状态, phase, 关闭原因)：带 hold 的未关闭 Issue 为待决定；已合并为完成并等待部署后确认；
    已关闭按关闭原因分为完成与取消。新状态原样返回。"""
    if status == "closed":
        done = close_reason is CloseReason.FIXED
        return (IssueStatus.DONE if done else IssueStatus.CANCELLED), None, close_reason
    if status not in LEGACY_STATUS:
        return IssueStatus(status), None, close_reason
    found, phase = LEGACY_STATUS[status]
    if status == "merged":
        return found, phase, CloseReason.FIXED
    if held:
        return IssueStatus.NEEDS_DECISION, None, close_reason
    return found, phase, close_reason


def dependency_met(dependency: Issue) -> bool:
    """排在前面的子任务已合并(合并即为完成)。"""
    return dependency.status is IssueStatus.DONE


@dataclass(frozen=True)
class IssueContext:
    close_reason: CloseReason | None = None
    duplicate_of: str | None = None
    hold: Hold | None = None
    restart_to: IssueStatus | None = None
    phase: IssuePhase | None = None  # 当前的 phase，由 apply_event 按 Issue 填入
    held: bool = False  # 当前是否带 hold，由 apply_event 按 Issue 填入

    @property
    def has_hold(self) -> bool:
        return self.hold is not None


S = IssueStatus
P = IssuePhase
_RESTARTABLE = frozenset({S.TODO, S.IN_PROGRESS, S.PENDING_MERGE})
_CLOSE = (IssueEffect.CLOSE, IssueEffect.SYNC_PROBLEMS)
_PHASE = (IssueEffect.SET_PHASE,)


def _phase(phase: IssuePhase | None) -> tuple[tuple[str, object], ...]:
    return (("phase", phase),)


def _user_close_rules() -> tuple[Rule, ...]:
    event, cancelled = IssueEvent.USER_CLOSED, S.CANCELLED
    return (
        Rule(event, OPEN, cancelled, when=(("close_reason", CloseReason.WONT_FIX),), effects=_CLOSE,
             payload=(("close_reason", CloseReason.WONT_FIX),)),
        Rule(event, OPEN, cancelled, when=(("close_reason", CloseReason.DUPLICATE),), effects=_CLOSE,
             payload=(("close_reason", CloseReason.DUPLICATE),), carry=("duplicate_of",)),
        Rule(event, OPEN, cancelled, when=(("close_reason", CloseReason.NOT_A_BUG),),
             effects=(*_CLOSE, IssueEffect.FILL_TRIAGE_OUTCOME),
             payload=(("close_reason", CloseReason.NOT_A_BUG), ("outcome", TriageOutcome.FALSE_CONFIRM))),
    )


def _restart_rules() -> tuple[Rule, ...]:
    rules = []
    for sources, when in ((_RESTARTABLE, ()), (frozenset({S.NEEDS_DECISION}), (("held", True),))):
        rules += [
            Rule(IssueEvent.RESTART, sources, S.TODO, when=(*when, ("restart_to", S.TODO)),
                 effects=(IssueEffect.CLEAR_HOLD, *_PHASE), payload=_phase(None)),
            Rule(IssueEvent.RESTART, sources, S.IN_PROGRESS, when=(*when, ("restart_to", S.IN_PROGRESS)),
                 effects=(IssueEffect.CLEAR_HOLD, *_PHASE), payload=_phase(P.VERIFY)),
        ]
    return tuple(rules)


TRANSITIONS: tuple[Rule, ...] = (
    Rule(IssueEvent.APPROVE, frozenset({S.NEEDS_DECISION}), S.TODO, effects=(IssueEffect.CLEAR_HOLD,)),
    Rule(IssueEvent.FIX_STARTED, frozenset({S.TODO, S.NEEDS_DECISION, S.IN_PROGRESS, S.PENDING_MERGE}), S.IN_PROGRESS,
         effects=(IssueEffect.CLEAR_HOLD, *_PHASE), payload=_phase(P.FIX)),
    Rule(IssueEvent.NOT_REPRODUCED, frozenset({S.IN_PROGRESS}), S.NEEDS_DECISION,
         effects=(IssueEffect.FILL_TRIAGE_OUTCOME, IssueEffect.REQUEST_RETRIAGE),
         payload=(("outcome", TriageOutcome.FALSE_CONFIRM),)),
    Rule(IssueEvent.FIX_HELD, frozenset({S.TODO, S.IN_PROGRESS}), S.NEEDS_DECISION, effects=(IssueEffect.SET_HOLD,),
         carry=("hold",)),
    Rule(IssueEvent.FIX_DONE, frozenset({S.IN_PROGRESS}), S.IN_PROGRESS, when=(("phase", P.FIX),), effects=_PHASE,
         payload=_phase(P.VERIFY)),
    Rule(IssueEvent.VERIFY_PASSED, frozenset({S.IN_PROGRESS}), S.IN_PROGRESS, when=(("phase", P.VERIFY),),
         effects=_PHASE, payload=_phase(P.SUBMIT)),
    Rule(IssueEvent.VERIFY_FAILED, frozenset({S.IN_PROGRESS}), S.NEEDS_DECISION,
         when=(("phase", P.VERIFY), ("has_hold", True)), effects=(IssueEffect.SET_HOLD,), carry=("hold",)),
    Rule(IssueEvent.VERIFY_FAILED, frozenset({S.IN_PROGRESS}), S.TODO, when=(("phase", P.VERIFY),)),
    Rule(IssueEvent.MAIN_MERGED, frozenset({S.IN_PROGRESS}), S.IN_PROGRESS, when=(("phase", P.SUBMIT),),
         effects=_PHASE, payload=_phase(P.VERIFY)),
    Rule(IssueEvent.MAIN_MERGED, frozenset({S.PENDING_MERGE}), S.IN_PROGRESS, effects=_PHASE,
         payload=_phase(P.VERIFY)),
    Rule(IssueEvent.PR_CREATED, frozenset({S.IN_PROGRESS}), S.PENDING_MERGE, when=(("phase", P.SUBMIT),)),
    Rule(IssueEvent.PR_MERGED, frozenset({S.PENDING_MERGE}), S.DONE, effects=(IssueEffect.CLOSE, *_PHASE),
         payload=(("close_reason", CloseReason.FIXED), ("phase", P.DEPLOY_CHECK))),
    Rule(IssueEvent.PR_CLOSED, frozenset({S.PENDING_MERGE}), S.CANCELLED, effects=_CLOSE,
         payload=(("close_reason", CloseReason.FIX_REJECTED),)),
    Rule(IssueEvent.STAGING_VERIFIED, frozenset({S.DONE}), S.DONE, when=(("phase", P.DEPLOY_CHECK),),
         effects=(IssueEffect.SYNC_PROBLEMS, IssueEffect.FILL_TRIAGE_OUTCOME, *_PHASE),
         payload=(("close_reason", CloseReason.FIXED), ("outcome", TriageOutcome.CORRECT), ("phase", None))),
    Rule(IssueEvent.STAGING_FAILED, frozenset({S.DONE}), S.TODO, when=(("phase", P.DEPLOY_CHECK),),
         effects=(IssueEffect.REOPEN,)),
    Rule(IssueEvent.PROBLEM_REGRESSED, CLOSED, S.TODO, effects=(IssueEffect.REOPEN,)),
    *_user_close_rules(),
    Rule(IssueEvent.USER_REOPENED, CLOSED, S.TODO, effects=(IssueEffect.REOPEN,)),
    *_restart_rules(),
)


def transition(
    state: IssueStatus, event: IssueEvent, context: IssueContext = IssueContext()
) -> tuple[IssueStatus, tuple[SideEffect, ...]]:
    """Issue 状态机；不在表中的组合抛出 InvalidTransition。

    用户关闭只接受不修、重复、不是缺陷；已修复与修复未采纳只由 pr-merged 与 pr-closed 写入。
    """
    return apply(TRANSITIONS, state, event, context)


CLOSE_IGNORE_REASONS = frozenset({CloseReason.FIX_REJECTED, CloseReason.WONT_FIX})


def problem_context_for_close(
    problem: Problem, close_reason: CloseReason, duplicate_of: str | None = None
) -> ProblemContext:
    """Issue 关闭时关联问题所需的上下文(design 4.7)：不修与修复未采纳在严重度升级或出现在新版本中时恢复。"""
    if close_reason in CLOSE_IGNORE_REASONS:
        condition = ignore_condition(problem, new_release=True, severity_escalated=True)
        return ProblemContext(close_reason=close_reason, ignore_until=condition)
    return ProblemContext(close_reason=close_reason, duplicate_of=duplicate_of)


def event_for_regression(issue: Issue) -> IssueEvent | None:
    """关联问题回归(问题副作用 issue-regressed)时 Issue 应收到的事件。

    完成且还在等待部署后确认的判为部署后确认失败；其余完成与取消的重新打开；未关闭的修复仍在进行，不需要处理。
    """
    if issue.status is IssueStatus.DONE and issue.phase is IssuePhase.DEPLOY_CHECK:
        return IssueEvent.STAGING_FAILED
    if issue.is_closed:
        return IssueEvent.PROBLEM_REGRESSED
    return None


def in_phase(issue: Issue, phase: IssuePhase) -> bool:
    """进行中且处于这一阶段；deploy-check 时为完成且等待部署后确认。"""
    status = IssueStatus.DONE if phase is IssuePhase.DEPLOY_CHECK else IssueStatus.IN_PROGRESS
    return issue.status is status and issue.phase is phase
