"""Issue 状态机(44c)：转换规则写成数据表，按顺序取第一条命中(原状态 + 事件 + 条件)；副作用只返回、不执行。

- `transition` 是纯函数：算出转换后的记录，不在表中的组合抛 InvalidTransition(写明当前状态与此时可用的事件)；
- `apply_event` 是唯一的写入口：转换、执行副作用(连带问题、回填误判、请求重新评估、GitHub 同步)、记历史、写
  record 文件与 issues 表，全部在一个事务中；失败时 Issue 文件恢复原文；
- 关闭原因只经表中的规则写入：用户关闭(CANCEL、REJECT)只接受不修、重复、不是缺陷；已修复只由 ACCEPT 写入，
  修复未采纳只由发布在 PR 被关闭时以 FAIL 写入，拆分只由 SPLIT_BACK 写入；
- hold 不是状态：只出现在待决定上(停下的原因)，离开待决定时一并清除。
- 回归(REGRESS)、重开(REOPEN)退回待修且上一次已动过手时，开下一次修复尝试(attempts.begin_next)。

CANCEL、FAIL 的 reason 为关闭原因或停下原因的代码(见 CLOSE_REASONS、FAIL_REASONS)；REJECT 的 reason 是关闭原因
代码时照用，是说明文字时按不修关闭；其余事件的 reason 是写进历史的说明。
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from tightrein.assess.issue import attempts
from tightrein.protocol.naming import Clock, format_iso
from tightrein.store.tables.issues import Issue

if TYPE_CHECKING:
    from tightrein.protocol.runtime import Runtime

IssueRecord = Issue


class IssueStatus(StrEnum):
    NEEDS_DECISION = "needs_decision"  # 待决定：放行关卡，或停下等用户
    TODO = "todo"
    IMPLEMENTING = "implementing"
    RELEASING = "releasing"
    ACCEPTING = "accepting"  # 已合并，验收观察中
    DONE = "done"
    CANCELLED = "cancelled"
    HELD = "held"  # 手动接管：tightrein 不再碰它


class IssueEvent(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    START = "start"
    DELIVER = "deliver"
    MERGE = "merge"
    ACCEPT = "accept"
    REGRESS = "regress"
    FAIL = "fail"
    CANCEL = "cancel"
    REOPEN = "reopen"
    TAKE = "take"
    GIVE = "give"
    SPLIT_BACK = "split_back"


class Effect(StrEnum):
    """交给 apply_event 执行的副作用。"""

    SYNC_PROBLEMS = "sync_problems"  # 按关闭原因处理关联问题
    FILL_FALSE_CONFIRM = "fill_false_confirm"  # 评估结论回填「误判为成立」
    FILL_CORRECT = "fill_correct"  # 评估结论回填「判对」
    REQUEST_RETRIAGE = "request_retriage"  # 关联问题请求重新评估
    GITHUB = "github"  # 新建、放行、关闭、重开时同步到 GitHub


# 关闭原因
FIXED = "fixed"
FIX_REJECTED = "fix_rejected"
WONT_FIX = "wont_fix"
DUPLICATE = "duplicate"
NOT_A_BUG = "not_a_bug"
SPLIT = "split"
CLOSE_REASONS = frozenset({FIXED, FIX_REJECTED, WONT_FIX, DUPLICATE, NOT_A_BUG, SPLIT})
USER_CLOSE_REASONS = frozenset({WONT_FIX, DUPLICATE, NOT_A_BUG})
# 停下原因中有特别处理的两种；其余为任意说明
NOT_REPRODUCED = "not_reproduced"
FAIL_REASONS = frozenset({NOT_REPRODUCED, FIX_REJECTED})

OPEN = frozenset({IssueStatus.NEEDS_DECISION, IssueStatus.TODO, IssueStatus.IMPLEMENTING, IssueStatus.RELEASING,
                  IssueStatus.ACCEPTING})
CLOSED = frozenset({IssueStatus.DONE, IssueStatus.CANCELLED})
UNCLOSED = frozenset(IssueStatus) - CLOSED  # 含手动接管
# 进入这些状态时所在的阶段(watch、status 显示用)；不在表中的清空阶段、步骤与轮次
STAGE_OF = {IssueStatus.IMPLEMENTING: "implement", IssueStatus.RELEASING: "release",
            IssueStatus.ACCEPTING: "release"}
ACCEPT_STEP = "release.accept"
USER = "user"

# extra 中的键
CLOSE_REASON = "closeReason"
HOLD = "hold"
HELD_FROM = "heldFrom"
HISTORY = "history"
PROBLEMS = "problems"
DUPLICATE_OF = "duplicateOf"
APPROVED_AT = "approvedAt"


class InvalidTransition(Exception):
    def __init__(self, issue: str, status: str, event: IssueEvent, allowed: list[str], reason: str | None) -> None:
        detail = f"(原因 {reason})" if reason else ""
        super().__init__(f"Issue {issue} 当前为 {status}，不接受 {event.value}{detail}；此时可用：{'、'.join(allowed) or '无'}")
        self.issue = issue
        self.status = status
        self.event = event
        self.allowed = allowed


@dataclass(frozen=True)
class Rule:
    """一行转换规则：when 中每项为 (事实名, 允许的取值)；target 为 None 表示状态不变，RESTORE 表示回到接管前的状态。"""

    event: IssueEvent
    sources: frozenset[IssueStatus]
    target: IssueStatus | str | None
    when: tuple[tuple[str, frozenset[str]], ...] = ()
    effects: tuple[Effect, ...] = ()
    close_reason: str | None = None  # 写入的关闭原因；"reason" 表示取事件的 reason


@dataclass(frozen=True)
class Facts:
    """规则条件用到的事实。"""

    reason: str
    origin: str
    held_stage: str  # 停在待决定时所在的阶段(hold.stage)；没有为空串
    held_status: str  # 停在待决定之前的状态(hold.status)；没有为空串


RESTORE = "restore"
FROM_REASON = "reason"
_S = IssueStatus
_USER_CLOSE = (("reason", USER_CLOSE_REASONS),)
_CLOSE_EFFECTS = (Effect.SYNC_PROBLEMS, Effect.GITHUB)

TRANSITIONS: tuple[Rule, ...] = (
    # 发布阶段停下的(冲突、CI 失败、交付与工作区不符、验收中要人看的)，放行后回到停下前的状态接着做：
    # 按 FAIL 时记下的状态与阶段区分，验收中停下的回到验收，其余发布阶段的回到发布
    Rule(IssueEvent.APPROVE, frozenset({_S.NEEDS_DECISION}), _S.ACCEPTING,
         when=(("held_status", frozenset({_S.ACCEPTING.value})),), effects=(Effect.GITHUB,)),
    Rule(IssueEvent.APPROVE, frozenset({_S.NEEDS_DECISION}), _S.RELEASING, when=(("held_stage", frozenset({"release"})),),
         effects=(Effect.GITHUB,)),
    # 实施中停下的(关卡、轮数、越界等)：用户的决定由 implement/approve 记下后放行，回到实施从停下的步骤接着做
    Rule(IssueEvent.APPROVE, frozenset({_S.NEEDS_DECISION}), _S.IMPLEMENTING,
         when=(("held_stage", frozenset({"implement"})),), effects=(Effect.GITHUB,)),
    Rule(IssueEvent.APPROVE, frozenset({_S.NEEDS_DECISION}), _S.TODO, effects=(Effect.GITHUB,)),
    # 用户自己提的需求创建即为待修：approve 不改状态，照常申请建修复分支
    Rule(IssueEvent.APPROVE, frozenset({_S.TODO}), None, when=(("origin", frozenset({USER})),)),
    Rule(IssueEvent.REJECT, frozenset({_S.NEEDS_DECISION}), _S.CANCELLED, when=_USER_CLOSE, effects=_CLOSE_EFFECTS,
         close_reason=FROM_REASON),
    # 拒绝时 reason 是用户的说明文字：按「不修」关闭
    Rule(IssueEvent.REJECT, frozenset({_S.NEEDS_DECISION}), _S.CANCELLED, effects=_CLOSE_EFFECTS,
         close_reason=WONT_FIX),
    # 发布时合并主干后改到了同一文件：退回实施重新审查
    Rule(IssueEvent.START, frozenset({_S.TODO, _S.IMPLEMENTING, _S.RELEASING}), _S.IMPLEMENTING),
    Rule(IssueEvent.DELIVER, frozenset({_S.IMPLEMENTING}), _S.RELEASING),
    Rule(IssueEvent.MERGE, frozenset({_S.RELEASING}), _S.ACCEPTING),
    Rule(IssueEvent.ACCEPT, frozenset({_S.ACCEPTING}), _S.DONE, effects=(Effect.FILL_CORRECT,), close_reason=FIXED),
    # 部署后确认失败回到待修；已关闭的因关联问题回归重新打开
    Rule(IssueEvent.REGRESS, frozenset({_S.ACCEPTING}), _S.TODO),
    Rule(IssueEvent.REGRESS, CLOSED, _S.TODO, effects=(Effect.GITHUB,)),
    Rule(IssueEvent.FAIL, frozenset({_S.RELEASING}), _S.CANCELLED, when=(("reason", frozenset({FIX_REJECTED})),),
         effects=_CLOSE_EFFECTS, close_reason=FIX_REJECTED),
    Rule(IssueEvent.FAIL, frozenset({_S.IMPLEMENTING}), _S.NEEDS_DECISION,
         when=(("reason", frozenset({NOT_REPRODUCED})),),
         effects=(Effect.FILL_FALSE_CONFIRM, Effect.REQUEST_RETRIAGE)),
    Rule(IssueEvent.FAIL, frozenset({_S.TODO, _S.IMPLEMENTING, _S.RELEASING, _S.ACCEPTING}), _S.NEEDS_DECISION),
    Rule(IssueEvent.CANCEL, OPEN, _S.CANCELLED, when=(("reason", frozenset({NOT_A_BUG})),),
         effects=(*_CLOSE_EFFECTS, Effect.FILL_FALSE_CONFIRM), close_reason=NOT_A_BUG),
    Rule(IssueEvent.CANCEL, OPEN, _S.CANCELLED, when=_USER_CLOSE, effects=_CLOSE_EFFECTS, close_reason=FROM_REASON),
    Rule(IssueEvent.REOPEN, CLOSED, _S.TODO, effects=(Effect.GITHUB,)),
    Rule(IssueEvent.TAKE, OPEN, _S.HELD),
    Rule(IssueEvent.GIVE, frozenset({_S.HELD}), RESTORE),
    Rule(IssueEvent.SPLIT_BACK, frozenset({_S.NEEDS_DECISION, _S.TODO, _S.IMPLEMENTING}), _S.CANCELLED,
         effects=(Effect.GITHUB,), close_reason=SPLIT),
)


def resolve(issue: IssueRecord, event: IssueEvent, reason: str | None) -> Rule:
    hold = issue.extra.get(HOLD) or {}
    facts = Facts(reason=reason or "", origin=issue.origin, held_stage=str(hold.get("stage") or ""),
                  held_status=str(hold.get("status") or ""))
    for rule in TRANSITIONS:
        if rule.event is event and issue.status in rule.sources and _holds(rule, facts):
            return rule
    raise InvalidTransition(issue.id, issue.status, event, allowed_events(issue), reason)


def allowed_events(issue: IssueRecord) -> list[str]:
    return sorted({rule.event.value for rule in TRANSITIONS if issue.status in rule.sources})


def transition(issue: IssueRecord, event: IssueEvent, *, reason: str | None, clock: Clock) -> IssueRecord:
    """转换后的记录(新对象，不改原记录)；历史由 apply_event 写。"""
    rule = resolve(issue, event, reason)
    extra = copy.deepcopy(issue.extra)
    status = _target(rule, issue, extra)
    stage, step, round_ = issue.stage, issue.step, issue.round
    # 停下(待决定)与接管保留所在的步骤，交还与重新开始时从那里看起
    if status != issue.status and status not in (_S.NEEDS_DECISION, _S.HELD) and issue.status != _S.HELD:
        stage = STAGE_OF.get(_S(status))
        if stage != issue.stage:
            step, round_ = None, None
        if status == _S.ACCEPTING and issue.status == _S.RELEASING:
            step = ACCEPT_STEP
    if stage is None and _S(status) in STAGE_OF:
        stage = STAGE_OF[_S(status)]  # 进行中必须带阶段：没记下的(交还、同状态的重新开始)按状态补上
    if status == _S.HELD:
        extra[HELD_FROM] = issue.status
    elif issue.status == _S.HELD:
        extra.pop(HELD_FROM, None)
    if status == _S.NEEDS_DECISION and event is not IssueEvent.APPROVE:
        extra[HOLD] = {"reason": reason or event.value, "since": format_iso(clock.now()), "stage": issue.stage,
                       "status": issue.status}
    elif status != _S.NEEDS_DECISION:
        extra.pop(HOLD, None)
    if event is IssueEvent.APPROVE and issue.status == _S.NEEDS_DECISION and not extra.get(APPROVED_AT):
        # 合并队列按第一次放行的时间排序：发布中停下后再放行，不重新排到队尾
        extra[APPROVED_AT] = format_iso(clock.now())
    if rule.close_reason is not None:
        extra[CLOSE_REASON] = reason if rule.close_reason == FROM_REASON else rule.close_reason
    elif status not in CLOSED:
        extra.pop(CLOSE_REASON, None)
    held_by = (reason or USER) if status == _S.HELD else None if issue.status == _S.HELD else issue.held_by
    record = replace(issue, status=str(status), stage=stage, step=step, round=round_, held_by=held_by, extra=extra)
    if event in NEW_ATTEMPT and attempts.started(issue):
        # 回归、重开后重新修复：开下一次尝试，不沿用上一次的分支、PR、合并提交与发布进度(见 attempts.py)
        record = attempts.begin_next(record, clock, event.value)
    check(record)
    return record


def effects_for(issue: IssueRecord, event: IssueEvent, reason: str | None) -> tuple[Effect, ...]:
    return resolve(issue, event, reason).effects


def check(issue: IssueRecord) -> None:
    """不变式：完成的关闭原因只能是已修复；已关闭的必有关闭原因、未关闭的没有；hold 只在待决定上；接管必有接管者；
    进行中(实施、发布、验收观察)必带所在阶段。"""
    reason = issue.extra.get(CLOSE_REASON)
    status = _S(issue.status)
    if (status == _S.DONE) != (reason == FIXED):
        raise ValueError(f"Issue {issue.id}：完成的关闭原因只能是 {FIXED}，{FIXED} 只能是完成")
    if (status in CLOSED) != (reason is not None) or (reason is not None and reason not in CLOSE_REASONS):
        raise ValueError(f"Issue {issue.id}：状态 {status} 与关闭原因 {reason!r} 不符")
    if issue.extra.get(HOLD) is not None and status != _S.NEEDS_DECISION:
        raise ValueError(f"Issue {issue.id}：hold 只出现在待决定上")
    if (status == _S.HELD) != (issue.held_by is not None):
        raise ValueError(f"Issue {issue.id}：只有手动接管的 Issue 带接管者")
    if status in STAGE_OF and issue.stage != STAGE_OF[status]:
        raise ValueError(f"Issue {issue.id}：状态 {status} 必须带阶段 {STAGE_OF[status]}，现为 {issue.stage!r}")


def apply_event(runtime: Runtime, issue_id: str, event: IssueEvent, *, reason: str | None = None,
                actor: str = USER, note: str | None = None, updates: Mapping[str, Any] | None = None,
                duplicate_of: str | None = None, sync_github: bool = True) -> IssueRecord:
    """转换并执行副作用，文件、索引与数据库在一个事务中；失败时恢复 Issue 文件原文并重新抛出。

    updates 可一并写入 branch、pr、merge_commit、deploy、step、round；sync_github 为假时不回写 GitHub(由 GitHub 上的
    操作读回引起的转换)。"""
    from tightrein.assess.issue import files, github
    from tightrein.store.db import transaction
    from tightrein.store.tables import issues

    unknown = sorted(set(updates or {}) - UPDATABLE)
    if unknown:
        raise ValueError(f"转换时只能一并写入 {'、'.join(sorted(UPDATABLE))}：{'、'.join(unknown)}")
    if event in (IssueEvent.CANCEL, IssueEvent.REJECT) and reason == DUPLICATE and not duplicate_of:
        raise ValueError("以重复关闭时必须给出另一个 Issue")
    before = issues.get(runtime.conn, issue_id)
    if before is None:
        raise LookupError(f"没有 Issue {issue_id}")
    after = transition(before, event, reason=reason, clock=runtime.clock)
    if updates:
        after = replace(after, **updates)
    if duplicate_of:
        after.extra[DUPLICATE_OF] = duplicate_of
    effects = resolve(before, event, reason).effects
    after.extra.setdefault(HISTORY, []).append(history_entry(runtime.clock, event, actor, reason, note))
    from tightrein.assess import persist

    touched = [issue_id, *([duplicate_of] if duplicate_of else [])]
    problem_files = persist.touched_paths(runtime, list(before.extra.get(PROBLEMS) or []))
    with files.restoring(runtime.workspace, touched, problem_files), transaction(runtime.conn):
        for effect in effects:
            if effect is not Effect.GITHUB:
                _run_effect(runtime, before, after, effect, actor, note)
        files.write(runtime, after)
    if before.status == _S.NEEDS_DECISION and after.status != _S.NEEDS_DECISION:
        # 放行关卡的待审核文档已经决定过了，不留给 watch 当成还在等
        runtime.workspace.human_document(issue_id, "pending").unlink(missing_ok=True)
    if Effect.GITHUB in effects and sync_github:
        github.sync(runtime, after)
    return after


def history_entry(clock: Clock, event: IssueEvent, actor: str, reason: str | None, note: str | None) -> dict[str, Any]:
    return {"at": format_iso(clock.now()), "event": event.value, "actor": actor, "reason": reason, "note": note}


def close(runtime: Runtime, issue_id: str, reason: str, *, note: str | None = None,
          duplicate_of: str | None = None) -> IssueRecord:
    """用户关闭(命令 close)：只接受不修、重复、不是缺陷。"""
    if reason not in USER_CLOSE_REASONS:
        raise ValueError(f"关闭原因只能是 {'、'.join(sorted(USER_CLOSE_REASONS))}；{reason} 不能由用户写入")
    if reason == DUPLICATE and (duplicate_of is None or duplicate_of == issue_id):
        raise ValueError("以重复关闭时必须给出另一个 Issue")
    return apply_event(runtime, issue_id, IssueEvent.CANCEL, reason=reason, note=note, duplicate_of=duplicate_of)


def regression_event(issue: IssueRecord) -> IssueEvent | None:
    """关联问题回归时 Issue 应收到的事件：验收观察中与已关闭的为 REGRESS；还在修的不用处理。"""
    return IssueEvent.REGRESS if issue.status in CLOSED or issue.status == _S.ACCEPTING else None


def approve(runtime: Runtime, issue_id: str, note: str | None = None) -> IssueRecord:
    """放行(命令 approve)：待决定 → 待修；用户需求已是待修时不改状态，照常申请建修复分支。"""
    return apply_event(runtime, issue_id, IssueEvent.APPROVE, note=note)


def on_regression(runtime: Runtime, issue_id: str, problem_id: str) -> IssueRecord | None:
    """关联问题回归(采集判定)：验收观察中的判为部署确认失败、已关闭的重新打开为待修；还在修的不用处理。"""
    from tightrein.store.tables import issues

    issue = issues.get(runtime.conn, issue_id)
    if issue is None or regression_event(issue) is None:
        return None
    return apply_event(runtime, issue_id, IssueEvent.REGRESS, actor="collect", note=f"关联问题 {problem_id} 回归")


def edit_problems(record: IssueRecord, text: str) -> list[str]:
    """用户编辑后的正文按必需小节校验；状态与关闭原因不在正文里，只能经命令改。"""
    from tightrein.assess.issue.body import HEADINGS, split
    from tightrein.assess.issue.create import REQUIRED

    found = split(text)
    return [f"缺少小节「{HEADINGS[key]['zh']}」或它是空的" for key in REQUIRED[record.origin] if not found.get(key)]


def save_edit(runtime: Runtime, issue_id: str, text: str) -> list[str]:
    """校验并保存用户编辑后的正文：不合格时返回问题、什么都不改(由命令行让用户重改或放弃，放弃即保留原文件)；
    通过后在历史中记下改了哪几节。"""
    from tightrein.assess.issue import files
    from tightrein.assess.issue.body import split
    from tightrein.store.db import transaction
    from tightrein.store.tables import issues

    record = issues.get(runtime.conn, issue_id)
    if record is None:
        raise LookupError(f"没有 Issue {issue_id}")
    problems = edit_problems(record, text)
    if problems:
        return problems
    before, after = split(files.read_body(runtime.workspace, issue_id)), split(text)
    changed = sorted(key for key in set(before) | set(after) if before.get(key) != after.get(key) and key != "history")
    record.extra.setdefault(HISTORY, []).append({
        "at": format_iso(runtime.clock.now()), "event": "edit", "actor": USER, "reason": None,
        "note": f"修改了 {'、'.join(changed) or '格式'}"})
    with files.restoring(runtime.workspace, [issue_id]), transaction(runtime.conn):
        files.write(runtime, record, body=text)
    return []


NEW_ATTEMPT = frozenset({IssueEvent.REGRESS, IssueEvent.REOPEN})  # 退回待修后要重新开始一次修复的事件
UPDATABLE = frozenset({"branch", "pr", "merge_commit", "deploy", "step", "round", "gate"})


def _holds(rule: Rule, facts: Facts) -> bool:
    return all(getattr(facts, name) in allowed for name, allowed in rule.when)


def _target(rule: Rule, issue: IssueRecord, extra: dict[str, Any]) -> str:
    if rule.target is None:
        return issue.status
    if rule.target == RESTORE:
        previous = extra.get(HELD_FROM)
        if previous is None:
            raise ValueError(f"Issue {issue.id} 没有记下接管前的状态")
        return str(previous)
    return str(rule.target)


def _run_effect(runtime: Runtime, before: IssueRecord, after: IssueRecord, effect: Effect, actor: str,
                note: str | None) -> None:
    from tightrein.assess import persist

    problems = list(before.extra.get(PROBLEMS) or [])
    if effect is Effect.SYNC_PROBLEMS:
        persist.close_problems(runtime, after, problems, after.extra[CLOSE_REASON], actor=actor)
    elif effect is Effect.FILL_FALSE_CONFIRM:
        why = "修复前复现不了" if after.status == _S.NEEDS_DECISION else "用户以「不是缺陷」关闭"
        persist.fill_outcome(runtime, problems, persist.FALSE_CONFIRM, f"Issue {before.id} {why}")
    elif effect is Effect.FILL_CORRECT:
        persist.fill_outcome(runtime, problems, persist.CORRECT, f"Issue {before.id} 验收通过")
    elif effect is Effect.REQUEST_RETRIAGE:
        persist.request_retriage(runtime, problems, f"Issue {before.id} 修复前复现不了" + (f"：{note}" if note else ""))
