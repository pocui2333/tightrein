"""Problem 实体与问题状态机；标题、出现的累计、覆盖、解决、回归与忽略到期的判定(design 2.7 到 2.9)。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from tightrein.domain.clock import format_iso, parse_iso
from tightrein.domain.commit_facts import CommitFacts
from tightrein.domain.enums import (
    CloseReason,
    Disposition,
    Probe,
    ProblemEffect,
    ProblemEvent,
    ProblemStatus,
)
from tightrein.domain.fingerprint import AUTHZ_CHECK
from tightrein.domain.normalize import location as normalize_location
from tightrein.domain.run import Run
from tightrein.domain.signal import Signal
from tightrein.domain.state_machine import Rule, SideEffect, apply




@dataclass(frozen=True)
class IgnoreCondition:
    """忽略的恢复条件，任一条件满足即恢复；全部为空表示永久忽略。

    occurrences 与 new_release 以忽略时的出现次数与最近出现 commit 为基准。
    """

    until: datetime | None = None
    occurrences: int | None = None
    new_release: bool = False
    severity_escalated: bool = False
    baseline_occurrences: int = 0
    baseline_release: str | None = None

    def __post_init__(self) -> None:
        if self.until is not None and self.until.tzinfo is None:
            raise ValueError("until 必须带时区")
        if self.occurrences is not None and self.occurrences < 1:
            raise ValueError(f"再出现次数必须大于 0：{self.occurrences}")

    @property
    def permanent(self) -> bool:
        return self.until is None and self.occurrences is None and not self.new_release and not self.severity_escalated

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "IgnoreCondition":
        until = data.get("until")
        return cls(
            until=parse_iso(until) if until else None,
            occurrences=data.get("occurrences"),
            new_release=bool(data.get("newRelease", False)),
            severity_escalated=bool(data.get("severityEscalated", False)),
            baseline_occurrences=int(data.get("baselineOccurrences", 0)),
            baseline_release=data.get("baselineRelease"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "until": format_iso(self.until) if self.until else None,
            "occurrences": self.occurrences,
            "newRelease": self.new_release,
            "severityEscalated": self.severity_escalated,
            "baselineOccurrences": self.baseline_occurrences,
            "baselineRelease": self.baseline_release,
        }


@dataclass(frozen=True)
class ProblemScope:
    """问题所在的位置，覆盖运行的判定依据：规范化后的位置、涉及的角色，以及平台来源与项目探针的来源名
    (signal.context.sourceName：error-tracking、log-platform、alert-source 或项目探针名)。"""

    location: str
    roles: frozenset[str] = frozenset()
    source: str | None = None

    def with_role(self, role: str | None) -> "ProblemScope":
        if role is None or role in self.roles:
            return self
        return replace(self, roles=self.roles | {role})

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProblemScope":
        return cls(data["location"], frozenset(data.get("roles", [])), data.get("source"))

    def to_dict(self) -> dict[str, Any]:
        return {"location": self.location, "roles": sorted(self.roles), "source": self.source}


@dataclass(frozen=True)
class Problem:
    id: str
    fingerprint: str
    fingerprint_version: int
    probe: Probe
    title: str
    status: ProblemStatus
    first_seen_at: datetime
    last_seen_at: datetime
    scope: ProblemScope
    first_seen_release: str | None = None
    last_seen_release: str | None = None
    resolved_release: str | None = None
    occurrences: int = 1
    issue_id: str | None = None
    ignore_until: IgnoreCondition | None = None
    intermittent: bool = False
    clean_covered_runs: int = 0
    merged_into: str | None = None

    def __post_init__(self) -> None:
        if self.first_seen_at.tzinfo is None or self.last_seen_at.tzinfo is None:
            raise ValueError("出现时间必须带时区")
        if self.occurrences < 1:
            raise ValueError(f"出现次数必须大于 0：{self.occurrences}")

    def to_dict(self) -> dict[str, Any]:
        """data/problem.schema.json 的文档形式。"""
        return {
            "id": self.id,
            "fingerprint": self.fingerprint,
            "fingerprintVersion": self.fingerprint_version,
            "probe": self.probe.value,
            "title": self.title,
            "status": self.status.value,
            "firstSeenAt": format_iso(self.first_seen_at),
            "lastSeenAt": format_iso(self.last_seen_at),
            "scope": self.scope.to_dict(),
            "firstSeenRelease": self.first_seen_release,
            "lastSeenRelease": self.last_seen_release,
            "resolvedRelease": self.resolved_release,
            "occurrences": self.occurrences,
            "issueId": self.issue_id,
            "ignoreUntil": self.ignore_until.to_dict() if self.ignore_until is not None else None,
            "intermittent": self.intermittent,
            "cleanCoveredRuns": self.clean_covered_runs,
            "mergedInto": self.merged_into,
        }


def ignore_condition(
    problem: Problem,
    *,
    until: datetime | None = None,
    occurrences: int | None = None,
    new_release: bool = False,
    severity_escalated: bool = False,
) -> IgnoreCondition:
    """以问题当前的出现次数与最近出现 commit 为基准生成恢复条件。"""
    return IgnoreCondition(
        until=until,
        occurrences=occurrences,
        new_release=new_release,
        severity_escalated=severity_escalated,
        baseline_occurrences=problem.occurrences,
        baseline_release=problem.last_seen_release,
    )


def role_of(signal: Signal) -> str | None:
    role = signal.actor.get("role") or signal.ctx("role")
    return str(role) if role else None


def _message(signal: Signal) -> str:
    return signal.normalized_message or signal.message


def title_for(signal: Signal, max_length: int) -> str:
    """问题标题：写现象，不写原因；只取规范化后的稳定字段。"""
    location = normalize_location(signal.location)
    probe, check = signal.probe, signal.check
    if probe is Probe.API_FUZZ:
        status = signal.ctx("response", "status")
        title = f"{location} {check}" + (f" {status}" if status is not None else "")
        if check == AUTHZ_CHECK:
            title += f"({role_of(signal)})"
    elif probe is Probe.PLATFORM_ERRORS:
        # 平台问题的消息即平台标题(异常类型开头)；日志条目没有异常时补上类别
        exception = signal.ctx("exceptionType") or signal.ctx("platformGroup")
        title = _message(signal) if exception else f"{signal.ctx('category')}：{_message(signal)}"
    elif probe in (Probe.ALERTS, Probe.ACCESS_LOG, Probe.PROJECT_PROBE):
        title = f"{location}：{_message(signal)}"
    elif probe is Probe.STATIC:
        title = f"{check}：{location}"
    else:
        title = f"{location}：{_message(signal)}"
    return title[:max_length]


def scope_of(signal: Signal) -> ProblemScope:
    source = signal.ctx("sourceName")
    return ProblemScope(location=normalize_location(signal.location),
                        source=str(source) if source else None).with_role(role_of(signal))


def new_problem(problem_id: str, signal: Signal, fingerprint: str, fingerprint_version: int,
                title_length: int) -> Problem:
    """由第一条信号新建问题，状态为待确认；标题截断到 title_length 个字符(runtime.aggregate.titleMaxChars)。"""
    return Problem(
        id=problem_id,
        fingerprint=fingerprint,
        fingerprint_version=fingerprint_version,
        probe=signal.probe,
        title=title_for(signal, title_length),
        status=ProblemStatus.PENDING,
        first_seen_at=signal.occurred_at,
        last_seen_at=signal.occurred_at,
        scope=scope_of(signal),
        first_seen_release=signal.release,
        last_seen_release=signal.release,
    )


def apply_occurrence(problem: Problem, signal: Signal) -> Problem:
    """累计一次出现：次数加一，更新首次与最近出现，清零覆盖运行计数，补充涉及的角色。

    信号可能不按时间顺序到达(重放、配套日志)，首次与最近出现按 occurred_at 比较；信号没有 commit 时保留原值。
    """
    earlier = signal.occurred_at < problem.first_seen_at
    later = signal.occurred_at >= problem.last_seen_at
    release = signal.release
    return replace(
        problem,
        occurrences=problem.occurrences + 1,
        first_seen_at=signal.occurred_at if earlier else problem.first_seen_at,
        first_seen_release=release if earlier and release is not None else problem.first_seen_release,
        last_seen_at=signal.occurred_at if later else problem.last_seen_at,
        last_seen_release=release if later and release is not None else problem.last_seen_release,
        clean_covered_runs=0,
        scope=problem.scope.with_role(role_of(signal)),
    )


RESOLVABLE = frozenset({ProblemStatus.NEW, ProblemStatus.ONGOING})
# 按来源名判断覆盖的采集方法
SOURCE_COVERED = frozenset({Probe.PLATFORM_ERRORS, Probe.ACCESS_LOG, Probe.ALERTS, Probe.PROJECT_PROBE})


def _file_of(location: str) -> str:
    return location.partition(":")[0]


def is_covered(problem: Problem, run: Run) -> bool:
    """本次运行是否真正测到了问题所在的位置(design 2.8「覆盖运行」)。

    平台来源与项目探针看本次是否读到了问题所属来源的数据；incidental 的覆盖范围为空，不会被覆盖。
    """
    if run.probe is not problem.probe:
        return False
    scope, coverage = problem.scope, run.coverage
    if problem.probe is Probe.API_FUZZ:
        method, _, route = scope.location.partition(" ")
        roles: frozenset[str | None] = frozenset(scope.roles) or frozenset({None})
        return all(coverage.covers_endpoint(method, route, role) for role in roles)
    if problem.probe in SOURCE_COVERED:
        return scope.source is not None and scope.source in coverage.sources
    if problem.probe is Probe.STATIC:
        return _file_of(scope.location) in coverage.files
    return False


def count_clean_run(problem: Problem, run: Run, facts: CommitFacts) -> Problem:
    """覆盖运行中没有出现：只有运行的 commit 晚于问题最后出现的 commit 时才累计。"""
    if facts.is_newer(run.target_commit, problem.last_seen_release) is not True:
        return problem
    return replace(problem, clean_covered_runs=problem.clean_covered_runs + 1)


def resolution_ready(problem: Problem, run: Run, facts: CommitFacts, covered_runs: int = 3) -> bool | None:
    """新发现或持续的问题能否判为已解决；commit 关系未知时返回 None，本次不做判定。

    static 在新 commit 上覆盖一次而未命中即可，其余探针需要 covered_runs 次覆盖运行。
    """
    if problem.status not in RESOLVABLE:
        return False
    newer = facts.is_newer(run.target_commit, problem.last_seen_release)
    if newer is None:
        return None
    required = 1 if problem.probe is Probe.STATIC else covered_runs
    return newer and problem.clean_covered_runs >= required


def is_regression(problem: Problem, release: str | None, facts: CommitFacts) -> bool | None:
    """已解决的问题再次出现在晚于解决时 commit 的版本上才算回归；关系未知时返回 None。"""
    if problem.status is not ProblemStatus.RESOLVED:
        return False
    return facts.is_newer(release, problem.resolved_release)


def ignore_expired(problem: Problem, now: datetime, facts: CommitFacts, escalated: bool = False) -> bool | None:
    """忽略的恢复条件是否满足：任一条件成立即为 True；其余不成立而有条件未知时返回 None。

    escalated 为调用方给出的「严重度已升级」事实；聚合看不到严重度，只有分诊能给出。
    """
    condition = problem.ignore_until
    if problem.status is not ProblemStatus.IGNORED or condition is None:
        return False
    results: list[bool | None] = []
    if condition.until is not None:
        results.append(now >= condition.until)
    if condition.occurrences is not None:
        results.append(problem.occurrences - condition.baseline_occurrences >= condition.occurrences)
    if condition.new_release:
        results.append(facts.is_newer(problem.last_seen_release, condition.baseline_release))
    if condition.severity_escalated:
        results.append(escalated)
    if any(result is True for result in results):
        return True
    if any(result is None for result in results):
        return None
    return False


@dataclass(frozen=True)
class ProblemContext:
    """转换所需的事实，由调用方用上面的判定函数算好后传入。"""

    disposition: Disposition | None = None
    close_reason: CloseReason | None = None
    ready_to_resolve: bool = False
    regressed: bool = False
    issue_id: str | None = None
    ignore_until: IgnoreCondition | None = None
    merge_target: str | None = None
    duplicate_of: str | None = None
    release: str | None = None

    @property
    def has_issue(self) -> bool:
        return self.issue_id is not None


_ALL = frozenset(ProblemStatus)
_TRIAGEABLE = frozenset({ProblemStatus.NEW, ProblemStatus.ONGOING, ProblemStatus.REGRESSED})
_OVERRIDABLE = _TRIAGEABLE | {ProblemStatus.IGNORED}
_SET_IGNORE = (ProblemEffect.SET_IGNORE_UNTIL,)
_CLEAR_IGNORE = (ProblemEffect.CLEAR_IGNORE_UNTIL,)
_SUPPRESS = (ProblemEffect.CLEAR_IGNORE_UNTIL, ProblemEffect.CREATE_SUPPRESSION)


def _occurrence_rules(event: ProblemEvent) -> tuple[Rule, ...]:
    """再次出现与复现检查失败：已解决的问题出现在更新的版本上转为回归，其余状态不变。"""
    resolved = frozenset({ProblemStatus.RESOLVED})
    return (
        Rule(event, resolved, ProblemStatus.REGRESSED, when=(("regressed", True), ("has_issue", True)),
             effects=(ProblemEffect.ISSUE_REGRESSED,), carry=("issue_id",)),
        Rule(event, resolved, ProblemStatus.REGRESSED, when=(("regressed", True),)),
        Rule(event, _ALL, None),
    )


def _resolve_rules(event: ProblemEvent) -> tuple[Rule, ...]:
    return (
        Rule(event, RESOLVABLE, ProblemStatus.RESOLVED, when=(("ready_to_resolve", True),),
             effects=(ProblemEffect.RECORD_RESOLVED_RELEASE,), carry=("release",)),
        Rule(event, _ALL, None),
    )


def _disposition_rules(event: ProblemEvent, sources: frozenset[ProblemStatus]) -> tuple[Rule, ...]:
    """分诊去向到问题状态(design 3.6)：误报与已接受的取舍为永久忽略，暂不修按恢复条件忽略。"""
    def when(disposition: Disposition) -> tuple[tuple[str, object], ...]:
        return (("disposition", disposition),)

    ignored, ongoing = ProblemStatus.IGNORED, ProblemStatus.ONGOING
    return (
        Rule(event, sources, ignored, when=when(Disposition.FALSE_POSITIVE), effects=_SUPPRESS),
        Rule(event, sources, ignored, when=when(Disposition.ACCEPTED_TRADEOFF), effects=_CLEAR_IGNORE),
        Rule(event, sources, ongoing, when=when(Disposition.AWAITING_DEPLOY), effects=_CLEAR_IGNORE),
        Rule(event, sources, ongoing, when=when(Disposition.CREATE_ISSUE), effects=_CLEAR_IGNORE),
        Rule(event, sources, ignored, when=when(Disposition.DEFERRED), effects=_SET_IGNORE, carry=("ignore_until",)),
        Rule(event, sources, None, when=when(Disposition.MANUAL_QUEUE)),
    )


def _close_rules() -> tuple[Rule, ...]:
    """关联 Issue 关闭后的同步(design 4.7)：不修与修复未采纳按恢复条件忽略，不是缺陷判为误报。"""
    def when(reason: CloseReason) -> tuple[tuple[str, object], ...]:
        return (("close_reason", reason),)

    event, ignored = ProblemEvent.ISSUE_CLOSED, ProblemStatus.IGNORED
    return (
        Rule(event, _ALL, None, when=when(CloseReason.FIXED)),
        Rule(event, _ALL, ignored, when=when(CloseReason.FIX_REJECTED), effects=_SET_IGNORE, carry=("ignore_until",)),
        Rule(event, _ALL, ignored, when=when(CloseReason.WONT_FIX), effects=_SET_IGNORE, carry=("ignore_until",)),
        Rule(event, _ALL, ignored, when=when(CloseReason.NOT_A_BUG), effects=_SUPPRESS),
        Rule(event, _ALL, None, when=when(CloseReason.DUPLICATE), effects=(ProblemEffect.MOVE_TO_DUPLICATE_ISSUE,),
             carry=("duplicate_of",)),
    )


TRANSITIONS: tuple[Rule, ...] = (
    Rule(ProblemEvent.REPRODUCED, frozenset({ProblemStatus.PENDING}), ProblemStatus.NEW),
    # 重放未复现：保持待确认并标记间歇，之后不再重放；某次运行再次出现时转为新(redesign/02-aggregate.md 第 2、3 节)
    Rule(ProblemEvent.NOT_REPRODUCED, frozenset({ProblemStatus.PENDING}), None,
         effects=(ProblemEffect.MARK_INTERMITTENT,)),
    Rule(ProblemEvent.PROMOTED, frozenset({ProblemStatus.PENDING}), ProblemStatus.NEW),
    *_occurrence_rules(ProblemEvent.SEEN_AGAIN),
    *_occurrence_rules(ProblemEvent.REGRESSION_CHECK_FAILED),
    *_resolve_rules(ProblemEvent.COVERED_RUN_WITHOUT_OCCURRENCE),
    *_resolve_rules(ProblemEvent.RESOLVED_ON_NEW_COMMIT),
    *_disposition_rules(ProblemEvent.TRIAGED, _TRIAGEABLE),
    *_disposition_rules(ProblemEvent.OVERRIDDEN, _OVERRIDABLE),
    Rule(ProblemEvent.USER_IGNORED, _ALL, ProblemStatus.IGNORED, effects=_SET_IGNORE, carry=("ignore_until",)),
    Rule(ProblemEvent.USER_FALSE_POSITIVE, _ALL, ProblemStatus.IGNORED, effects=_SUPPRESS),
    Rule(ProblemEvent.USER_REOPENED, frozenset({ProblemStatus.RESOLVED, ProblemStatus.IGNORED}), ProblemStatus.NEW,
         effects=(ProblemEffect.CLEAR_IGNORE_UNTIL, ProblemEffect.RESET_CLEAN_RUNS)),
    Rule(ProblemEvent.IGNORE_EXPIRED, frozenset({ProblemStatus.IGNORED}), ProblemStatus.NEW, effects=_CLEAR_IGNORE),
    Rule(ProblemEvent.MERGED, _ALL, None, effects=(ProblemEffect.MERGE_INTO,), carry=("merge_target",)),
    Rule(ProblemEvent.REBUILT, _ALL, None),
    Rule(ProblemEvent.RETRIAGE_REQUESTED, _OVERRIDABLE, None),
    *_close_rules(),
)


def transition(
    state: ProblemStatus, event: ProblemEvent, context: ProblemContext = ProblemContext()
) -> tuple[ProblemStatus, tuple[SideEffect, ...]]:
    """问题状态机：按 TRANSITIONS 查表，不在表中的组合抛出 InvalidTransition。

    每次转换(包括状态不变的)都由调用方写一条 problem_events，这一点不作为副作用列出。
    """
    return apply(TRANSITIONS, state, event, context)
