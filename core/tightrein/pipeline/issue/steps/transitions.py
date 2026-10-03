"""Issue 的状态转换与连带处理(architecture/06 10.4，design 4.7)。

所有转换经 domain.issue.transition 计算，不在转换表中的组合抛出 InvalidTransition。每次转换在「历史」一节追加一行，
写一条 issue_events，并执行副作用：写入或清除关闭原因、设置或清除 hold(离开待决定时一并清除)、写入 phase(状态改变而规则
没有给出时清空)、按关闭原因同步关联问题(经 ChangeSet 与
apply.commit，事件带 detail.context；不是缺陷时生成抑制规则，重复时问题改挂到被重复的 Issue 并加入它的 problems)、
回填分诊结论的实际结果、修复前复现不了时关联问题写入 retriage-requested 事件；关键节点为 GitHub 镜像写待发评论
(github_comments)。fix 与 release 转换时可以一并写入
branch、pr 两个字段(updates)；不改变状态的记录(建分支、推送、部署结果等)经 annotate 只写历史行与这些字段。文件、索引与数据库在一个事务中更新，失败时 Issue 文件恢复原内容。
用户关闭只接受不修、重复、不是缺陷；已修复与修复未采纳只由 verify 与 release 经同一个函数写入。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import tzinfo
from pathlib import Path

from tightrein.config.project import ProjectConfig
from tightrein.domain.clock import Clock
from tightrein.domain.enums import CloseReason, IssueEffect, IssueEvent, IssueStatus, ProblemEvent, TriageOutcome
from tightrein.domain.issue import TRANSITIONS, IssueContext, problem_context_for_close
from tightrein.domain.issue import transition as issue_transition
from tightrein.domain.state_machine import InvalidTransition
from tightrein.pipeline.aggregate.changeset import ChangeSet
from tightrein.pipeline.aggregate.steps import apply
from tightrein.pipeline.issue.render import issue as template
from tightrein.pipeline.issue.steps import github_comments
from tightrein.store.db import transaction
from tightrein.store.files import issue_files
from tightrein.store.files.issue_files import IssueDocument
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.repos import issue_events, issues, problems, triage
from tightrein.store.repos.issue_events import IssueEventRecord
from tightrein.store.repos.issues import IssueRecord
from tightrein.store.repos.problem_events import OPERATION_AUTO, OPERATION_USER

USER = "user"
USER_CLOSE_REASONS = frozenset({CloseReason.WONT_FIX, CloseReason.DUPLICATE, CloseReason.NOT_A_BUG})
FIELD_EFFECTS = frozenset({IssueEffect.CLOSE, IssueEffect.REOPEN, IssueEffect.SET_HOLD, IssueEffect.CLEAR_HOLD,
                           IssueEffect.SET_PHASE})
USER_COMMANDS = {IssueEvent.APPROVE: "approve", IssueEvent.USER_CLOSED: "close", IssueEvent.USER_REOPENED: "reopen"}
UPDATABLE_FIELDS = frozenset({"branch", "pr"})
ANNOTATE_FIELDS = UPDATABLE_FIELDS | {"hold", "github"}


class IssueCommandRejected(ValueError):
    """命令的前提不满足：Issue 不存在、关闭原因不允许用户指定、重复关闭缺少或指向无效的 Issue。"""


@dataclass(frozen=True)
class IssueEnv:
    conn: sqlite3.Connection
    layout: WorkspaceLayout
    clock: Clock
    config: ProjectConfig
    zone: tzinfo | None = None


def record_of(env: IssueEnv, issue_id: str) -> IssueRecord:
    record = issues.get(env.conn, issue_id)
    if record is None:
        raise IssueCommandRejected(f"没有 Issue {issue_id}")
    return record


def _history(event: IssueEvent, actor: str, note: str | None, reason: CloseReason | None) -> str:
    text = f"{event.label}(操作者 {actor})"
    if reason is not None:
        text += f"，关闭原因：{reason.label}"
    return text + (f"：{note}" if note else "")


def _sync_problems(env: IssueEnv, issue_id: str, problem_ids: tuple[str, ...], reason: CloseReason,
                   duplicate_of: str | None, actor: str) -> None:
    changeset = ChangeSet.start(env.conn, None, env.clock.now(), env.config.whole_threshold("suppressionDays"))
    operation = OPERATION_USER if actor == USER else OPERATION_AUTO
    for problem_id in problem_ids:
        problem = problems.get(env.conn, problem_id)
        if problem is None:
            continue
        context = problem_context_for_close(problem, reason, duplicate_of)
        changeset.transition(problem, ProblemEvent.ISSUE_CLOSED, context, operation=operation,
                             reason=f"Issue {issue_id} 以「{reason.label}」关闭")
    apply.commit(env.conn, changeset, env.clock, env.layout, side_effects=True)


def _move_to(env: IssueEnv, target_id: str, problem_ids: tuple[str, ...], source_id: str,
             originals: dict[Path, str]) -> None:
    record = record_of(env, target_id)
    path = env.layout.root / record.path
    current = issue_files.read(path)
    originals.setdefault(path, path.read_text(encoding="utf-8"))
    added = tuple(problem_id for problem_id in problem_ids if problem_id not in current.issue.problems)
    body = current.body
    for problem_id in added:
        found = problems.get(env.conn, problem_id)
        body = template.append_related(body, problem_id, found.title if found else '')
    body = template.append_history(body, env.clock.now(), f"Issue {source_id} 以重复关闭，并入问题 "
                                   f"{'、'.join(added) or '无'}", env.zone)
    issue = replace(current.issue, problems=(*current.issue.problems, *added), updated_at=env.clock.now())
    issue_files.write(env.conn, env.layout, IssueDocument(issue, body, current.run_id))


def _request_retriage(env: IssueEnv, issue_id: str, problem_ids: tuple[str, ...], note: str | None) -> None:
    changeset = ChangeSet.start(env.conn, None, env.clock.now(), env.config.whole_threshold("suppressionDays"))
    reason = f"Issue {issue_id} 修复前复现不了，需要重新分诊" + (f"：{note}" if note else "")
    for problem_id in problem_ids:
        problem = problems.get(env.conn, problem_id)
        if problem is not None:
            changeset.transition(problem, ProblemEvent.RETRIAGE_REQUESTED, reason=reason)
    apply.commit(env.conn, changeset, env.clock, env.layout, side_effects=True)


def _fill_outcome(env: IssueEnv, problem_ids: tuple[str, ...], outcome: TriageOutcome) -> None:
    for problem_id in problem_ids:
        latest = triage.latest(env.conn, problem_id)
        if latest is not None and latest.result.outcome is None:
            triage.set_outcome(env.conn, problem_id, latest.result.attempt, outcome, env.clock.now())


def apply_event(env: IssueEnv, record: IssueRecord, event: IssueEvent, context: IssueContext = IssueContext(), *,
                actor: str = USER, note: str | None = None,
                updates: Mapping[str, object] | None = None, comment: bool = True) -> IssueRecord:
    """comment 为假时不为 GitHub 镜像写关键节点评论(由 GitHub 上的操作读回引起的转换)。"""
    unknown = sorted(set(updates or {}) - UPDATABLE_FIELDS)
    if unknown:
        raise ValueError(f"转换时只能一并写入 {'、'.join(sorted(UPDATABLE_FIELDS))}：{'、'.join(unknown)}")
    path = env.layout.root / record.path
    current = issue_files.read(path)
    before = current.issue
    context = replace(context, phase=before.phase, held=before.hold is not None)
    try:
        status, effects = issue_transition(before.status, event, context)
    except InvalidTransition as error:
        allowed = sorted({command for rule in TRANSITIONS if before.status in rule.sources
                          for item, command in USER_COMMANDS.items() if rule.event is item})
        raise IssueCommandRejected(f"Issue {before.id} 当前为「{before.status.label}」，不能{event.label}；"
                                   f"可用的命令：{'、'.join(allowed) or '无'}") from error
    fields: dict[str, object] = {**(updates or {}), "status": status, "updated_at": env.clock.now()}
    if status is not before.status:
        fields["phase"] = None
    if status is not IssueStatus.NEEDS_DECISION:
        fields["hold"] = None
    close_reason: CloseReason | None = None
    for effect in effects:
        if effect.kind is IssueEffect.CLOSE:
            close_reason = effect.detail["close_reason"]
            fields["close_reason"] = close_reason
        elif effect.kind is IssueEffect.REOPEN:
            fields["close_reason"] = None
        elif effect.kind is IssueEffect.SET_HOLD:
            fields["hold"] = effect.detail["hold"]
        elif effect.kind is IssueEffect.CLEAR_HOLD:
            fields["hold"] = None
        elif effect.kind is IssueEffect.SET_PHASE:
            fields["phase"] = effect.detail["phase"]
    issue = replace(before, **fields)
    originals = {path: path.read_text(encoding="utf-8")}
    try:
        with transaction(env.conn):
            for effect in effects:
                if effect.kind in FIELD_EFFECTS:
                    continue
                if effect.kind is IssueEffect.SYNC_PROBLEMS:
                    reason = effect.detail["close_reason"]
                    _sync_problems(env, before.id, before.problems, reason, context.duplicate_of, actor)
                    if reason is CloseReason.DUPLICATE and context.duplicate_of is not None:
                        _move_to(env, context.duplicate_of, before.problems, before.id, originals)
                elif effect.kind is IssueEffect.FILL_TRIAGE_OUTCOME:
                    _fill_outcome(env, before.problems, effect.detail["outcome"])
                elif effect.kind is IssueEffect.REQUEST_RETRIAGE:
                    _request_retriage(env, before.id, before.problems, note)
                else:
                    raise ValueError(f"issue 模块不处理副作用 {effect.kind.value}")
            body = template.append_history(current.body, env.clock.now(),
                                           _history(event, actor, note, close_reason), env.zone)
            written = issue_files.write(env.conn, env.layout, IssueDocument(issue, body, current.run_id))
            issue_events.append(env.conn, IssueEventRecord(before.id, env.clock.now(), event.value, actor,
                                                           before.status, status, close_reason, note))
            text = github_comments.transition_comment(event, status, close_reason, note, env.config.language)
            if comment and text is not None:
                github_comments.queue(env.conn, env.config, written.issue, text, env.clock.now())
    except BaseException:
        for original, text in originals.items():
            original.write_text(text, encoding="utf-8")
        raise
    return written


def annotate(env: IssueEnv, issue_id: str, text: str, *, updates: Mapping[str, object] | None = None) -> IssueRecord:
    """不改变状态时在「历史」追加一行，可一并写入 branch、pr、hold 或 github(例如建立修复分支、推送、部署失败、
    进入 master、worktree 准备失败时转人工、建好 GitHub 镜像)；不写 issue_events。"""
    unknown = sorted(set(updates or {}) - ANNOTATE_FIELDS)
    if unknown:
        raise ValueError(f"只能一并写入 {'、'.join(sorted(ANNOTATE_FIELDS))}：{'、'.join(unknown)}")
    record = record_of(env, issue_id)
    current = issue_files.read(env.layout.root / record.path)
    issue = replace(current.issue, **(updates or {}), updated_at=env.clock.now())
    body = template.append_history(current.body, env.clock.now(), text, env.zone)
    return issue_files.write(env.conn, env.layout, IssueDocument(issue, body, current.run_id))


def approve(env: IssueEnv, issue_id: str, note: str | None = None) -> IssueRecord:
    """用户需求的 Issue 创建时即为待修(免审阅)，放行时不再转换状态，由调用方照常申请建修复分支。"""
    record = record_of(env, issue_id)
    if record.issue.is_manual and record.issue.status is IssueStatus.TODO:
        return record
    return apply_event(env, record, IssueEvent.APPROVE, note=note)


def close(env: IssueEnv, issue_id: str, reason: CloseReason, *, note: str | None = None,
          duplicate_of: str | None = None) -> IssueRecord:
    if reason not in USER_CLOSE_REASONS:
        raise IssueCommandRejected(f"关闭原因只能是 {'、'.join(item.value for item in USER_CLOSE_REASONS)}；"
                                   f"{reason.value} 由 verify 与 release 写入")
    if reason is CloseReason.DUPLICATE:
        if duplicate_of is None or duplicate_of == issue_id:
            raise IssueCommandRejected("以重复关闭时必须用 --duplicate-of 给出另一个 Issue")
        record_of(env, duplicate_of)
    context = IssueContext(close_reason=reason, duplicate_of=duplicate_of)
    return apply_event(env, record_of(env, issue_id), IssueEvent.USER_CLOSED, context, note=note)


def reopen(env: IssueEnv, issue_id: str, note: str | None = None) -> IssueRecord:
    return apply_event(env, record_of(env, issue_id), IssueEvent.USER_REOPENED, note=note)
