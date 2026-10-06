"""待确认操作的确认、拒绝、执行与过期(architecture/02 4.5)。

确认只来自用户(tightrein approve)；删除类操作需要确认两次；执行方为 user 的操作不能经 confirm 执行。项目声明不逐次
确认的操作与按规则自动决定的操作经 run_unattended 记为已确认后立即执行(vcs/unattended.py)，理由写 gate 事件。
执行：
1. 状态必须为 confirmed；
2. 查幂等键：已完成的直接记为 executed 并返回上次的结果；处于进行中(上次执行被中断)的，按实际状态判断是否已经完成，
   已完成的补记完成，未完成的删除进行中的键后继续；建修复 worktree 时分支与 worktree 已存在且对应的，直接复用；
3. 复核前置条件：以构造时的参数重新观察状态，任何一项不同即改为 expired，不执行任何命令；
4. 写入进行中的幂等键，逐条执行，每条的输出保存到 `raw/vcs/<操作编号>/`，任何一条失败即停止；
5. 成功：幂等键完成并记录结果，操作改为 executed；失败：操作改为 failed，记录失败的步骤、退出码与错误输出(已脱敏)，
   幂等键保持进行中。合并冲突、推送被拒绝、worktree 不干净按 porcelain 状态分类。
先查幂等键再复核前置条件：已经执行过的操作状态已变，先复核会被误判为过期。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any

from tightrein.config import layers
from tightrein.domain.clock import Clock
from tightrein.domain.enums import OperationExecutor, OperationKind, OperationStatus, Stage
from tightrein.observability.tracing import Tracer
from tightrein.store import idempotency
from tightrein.store.files import atomic
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.repos import pending_operations
from tightrein.vcs import parse
from tightrein.vcs.errors import (
    GhAuthError,
    MergeConflict,
    NetworkError,
    PushRejected,
    VcsError,
    WorktreeDirty,
)
from tightrein.vcs.gh_read import GhReader
from tightrein.vcs.git_read import GitReader
from tightrein.vcs.operations import PendingOperation, Target, load, observe, open_pull, save
from tightrein.vcs.process import GH_AUTH_EXIT_CODE, GIT_FATAL_EXIT_CODE, Completed, VcsProcess, tail
from tightrein.vcs.unattended import BRANCH_KINDS, KINDS

REUSABLE = frozenset({OperationKind.CREATE_FIX_WORKTREE, OperationKind.INIT_READONLY_WORKTREE})
OUTPUT_KINDS = frozenset({OperationKind.GITHUB_ISSUE, OperationKind.MERGE_PULL_REQUEST, OperationKind.PR_COMMENT,
                          OperationKind.REVERT_PULL_REQUEST})
AUTO_CONFIRM = "auto-confirm"


class OperationStateError(Exception):
    """操作当前的状态不允许这一步：确认非待确认的操作、执行未确认的操作、确认执行方为 user 的操作。"""


@dataclass(frozen=True)
class FollowUp:
    """发起模块登记的后续处理：执行成功后与被拒绝后调用。"""

    executed: Callable[[PendingOperation], None] | None = None
    rejected: Callable[[PendingOperation], None] | None = None


@dataclass(frozen=True)
class OperationResult:
    operation: PendingOperation
    ran: bool
    error: VcsError | None = None

    @property
    def status(self) -> OperationStatus:
        return self.operation.status


class OperationRunner:
    def __init__(self, conn: sqlite3.Connection, process: VcsProcess, git: GitReader, gh: GhReader,
                 layout: WorkspaceLayout, run_id: str, *, tracer: Tracer | None = None,
                 follow_ups: Mapping[Stage, FollowUp] | None = None, expire_days: int | None = None) -> None:
        """expire_days 为待确认的 git 操作的过期天数(runtime.vcs.operationExpireDays)，缺省取核心缺省值。"""
        self.conn = conn
        self.expire_after = timedelta(days=int(layers.core_value("runtime.vcs.operationExpireDays"))
                                      if expire_days is None else expire_days)
        self.process = process
        self.git = git
        self.gh = gh
        self.layout = layout
        self.run_id = run_id
        self.tracer = tracer
        self.follow_ups = dict(follow_ups or {})

    def _event(self, operation: PendingOperation, decision: str, reason: str | None = None) -> None:
        if self.tracer is not None:
            self.tracer.event("user_action", decision=decision, reason=reason, artifact=operation.id,
                              attributes={"kind": operation.kind.value, "subjectId": operation.subject_id})

    # 确认与拒绝

    def confirm(self, operation_id: str, *, confirmed_by: str, clock: Clock) -> PendingOperation:
        operation = load(self.conn, operation_id)
        if operation.status is not OperationStatus.PENDING:
            raise OperationStateError(f"{operation_id} 的状态为 {operation.status.label}，不能确认")
        given = operation.confirmations_given + 1
        status = OperationStatus.CONFIRMED if given >= operation.confirmations_required else OperationStatus.PENDING
        confirmed = replace(operation, confirmations_given=given, status=status, decided_at=clock.now())
        save(self.conn, confirmed)
        self._event(confirmed, "confirm", f"{confirmed_by} 第 {given} 次确认")
        return confirmed

    def reject(self, operation_id: str, *, note: str | None, clock: Clock) -> PendingOperation:
        operation = load(self.conn, operation_id)
        if operation.status not in (OperationStatus.PENDING, OperationStatus.CONFIRMED):
            raise OperationStateError(f"{operation_id} 的状态为 {operation.status.label}，不能拒绝")
        rejected = replace(operation, status=OperationStatus.REJECTED, decided_at=clock.now(),
                           result={"note": note} if note else None)
        save(self.conn, rejected)
        self._event(rejected, "reject", note)
        follow_up = self.follow_ups.get(rejected.stage)
        if follow_up is not None and follow_up.rejected is not None:
            follow_up.rejected(rejected)
        return rejected

    def run_unattended(self, operation_id: str, *, reason: str, clock: Clock) -> OperationResult:
        """不等用户确认，记为已确认后立即执行(vcs/unattended.py)：只接受白名单内、需要一次确认、执行方为 vcs 的待确认
        操作；提交、合并主干与推送的分支是主分支时拒绝。确认的理由写一条 gate 事件，其余与 execute 相同。"""
        operation = load(self.conn, operation_id)
        if operation.kind not in KINDS or operation.confirmations_required != 1:
            raise OperationStateError(f"{operation_id}({operation.kind.label})只能由用户确认")
        if operation.status is not OperationStatus.PENDING:
            raise OperationStateError(f"{operation_id} 的状态为 {operation.status.label}，不能确认")
        target = operation.preconditions.get("target")
        if operation.kind in BRANCH_KINDS and target is not None and target["branch"] in (None, target["main"]):
            raise OperationStateError(f"{operation_id} 作用于主分支或游离 HEAD，不直接执行")
        confirmed = replace(operation, confirmations_given=operation.confirmations_required,
                            status=OperationStatus.CONFIRMED, decided_at=clock.now())
        save(self.conn, confirmed)
        if self.tracer is not None:
            self.tracer.event("gate", decision=AUTO_CONFIRM, reason=reason, artifact=operation.id,
                              attributes={"kind": operation.kind.value, "subjectId": operation.subject_id})
        return self.execute(operation_id, clock=clock)

    def expire_stale(self, clock: Clock) -> list[PendingOperation]:
        """执行方为 vcs、超过 expire_after 未确认的操作改为 expired。"""
        expired = []
        for record in pending_operations.find(self.conn, status=OperationStatus.PENDING):
            operation = PendingOperation.from_record(record)
            if operation.executor is OperationExecutor.VCS and clock.now() - operation.created_at > self.expire_after:
                expired.append(save(self.conn, replace(operation, status=OperationStatus.EXPIRED,
                                                       result={"reason": f"超过 {self.expire_after.days} 天未确认，需要重新生成"})))
        return expired

    # 执行

    def execute(self, operation_id: str, *, clock: Clock) -> OperationResult:
        operation = load(self.conn, operation_id)
        if operation.status is not OperationStatus.CONFIRMED:
            raise OperationStateError(f"{operation_id} 的状态为 {operation.status.label}，确认后才能执行")
        target = Target.from_dict(operation.preconditions["target"]) if "target" in operation.preconditions else None
        key = operation.idempotency_key
        record = idempotency.get(self.conn, key)
        if record is not None and record.status == idempotency.DONE:
            return self._executed(operation, {**(record.result or {}), "alreadyExecuted": True}, clock, ran=False)
        if record is not None:
            reconciled = self._reconcile(operation, target)
            if reconciled is not None:
                idempotency.complete(self.conn, key, reconciled, clock)
                return self._executed(operation, {**reconciled, "alreadyExecuted": True}, clock, ran=False)
            idempotency.abandon(self.conn, key)
        elif operation.kind in REUSABLE and target is not None:
            reused = self._reconcile(operation, target)
            if reused is not None:
                idempotency.begin(self.conn, key, clock)
                idempotency.complete(self.conn, key, reused, clock)
                return self._executed(operation, {**reused, "reused": True}, clock, ran=False)
        if target is not None:
            differences = self._differences(operation, target)
            if differences:
                expired = save(self.conn, replace(operation, status=OperationStatus.EXPIRED,
                                                  result={"changed": differences}))
                return OperationResult(expired, ran=False)
        idempotency.begin(self.conn, key, clock)
        try:
            outputs = self._run_steps(operation, target)
            result = self._result(operation, target, outputs)
        except VcsError as error:
            failed = save(self.conn, replace(operation, status=OperationStatus.FAILED, result=self._failure(error)))
            return OperationResult(failed, ran=True, error=error)
        idempotency.complete(self.conn, key, result, clock)
        return self._executed(operation, result, clock, ran=True)

    def _executed(self, operation: PendingOperation, result: Mapping[str, Any], clock: Clock,
                  ran: bool) -> OperationResult:
        executed = save(self.conn, replace(operation, status=OperationStatus.EXECUTED, executed_at=clock.now(),
                                           result=dict(result)))
        follow_up = self.follow_ups.get(executed.stage)
        if follow_up is not None and follow_up.executed is not None:
            follow_up.executed(executed)
        return OperationResult(executed, ran=ran)

    def _differences(self, operation: PendingOperation, target: Target) -> list[str]:
        before = operation.preconditions.get("state", {})
        now = observe(operation.kind, target, self.git, self.gh)
        return sorted(key for key in set(before) | set(now) if before.get(key) != now.get(key))

    def _run_steps(self, operation: PendingOperation, target: Target | None) -> list[Completed]:
        raw = self.layout.vcs_raw_dir(self.run_id, operation.id)
        outputs = []
        if operation.kind is OperationKind.CLEANUP and target is not None and target.worktree is not None:
            status = self.git.status(target.worktree)
            if not status.clean:
                raise WorktreeDirty(f"{target.worktree} 有未提交的改动，不删除", paths=status.changed_paths)
        for number, step in enumerate(operation.commands, start=1):
            completed = self.process.run(step.argv, step.cwd)
            atomic.write_text(raw / f"step-{number}.log", self._log(completed))
            if completed.returncode != 0:
                raise self._classify(operation, step.argv, completed, target)
            outputs.append(completed)
        return outputs

    def _log(self, completed: Completed) -> str:
        redact = self.process.redactor.text
        return (f"$ {redact(' '.join(completed.argv))}\nexit code: {completed.returncode}\n"
                f"--- stdout ---\n{redact(completed.stdout)}\n--- stderr ---\n{redact(completed.stderr)}\n")

    def _classify(self, operation: PendingOperation, argv: tuple[str, ...], completed: Completed,
                  target: Target | None) -> VcsError:
        stderr = self.process.redactor.text(tail(completed.stderr, self.process.stderr_tail_lines))
        details = {"argv": argv, "returncode": completed.returncode, "stderr": stderr}
        message = f"{' '.join(argv[:3])} 退出码 {completed.returncode}：{stderr or '没有错误输出'}"
        worktree = target.worktree if target is not None and target.worktree is not None else None
        if operation.kind is OperationKind.MERGE_MAIN and worktree is not None:
            conflicts = self.git.status(worktree).unmerged
            if conflicts:
                return MergeConflict(f"合并 origin/main 出现冲突：{', '.join(conflicts)}", files=conflicts, **details)
        if operation.kind is OperationKind.PUSH:
            rejected = parse.parse_push(completed.stdout).rejected
            if rejected:
                return PushRejected(f"远程拒绝推送 {', '.join(rejected)}，先同步主干", rejected=rejected, **details)
            if completed.returncode == GIT_FATAL_EXIT_CODE:
                return NetworkError(message, **details)
        if argv[0] == self.process.gh_path and completed.returncode == GH_AUTH_EXIT_CODE:
            return GhAuthError(message, **details)
        return VcsError(message, **details)

    def _failure(self, error: VcsError) -> dict[str, Any]:
        failure: dict[str, Any] = {"error": type(error).__name__, "message": str(error), "argv": list(error.argv),
                                   "exitCode": error.returncode, "stderr": error.stderr}
        if isinstance(error, MergeConflict):
            failure["conflictFiles"] = list(error.files)
        if isinstance(error, PushRejected):
            failure["rejected"] = list(error.rejected)
        if isinstance(error, WorktreeDirty):
            failure["dirtyPaths"] = list(error.paths)
        return failure

    def _result(self, operation: PendingOperation, target: Target | None,
                outputs: list[Completed]) -> dict[str, Any]:
        if operation.kind in OUTPUT_KINDS:
            return {"outputs": [completed.stdout.strip() for completed in outputs]}
        if target is None:
            return {}
        worktree = target.worktree or target.repo
        kind = operation.kind
        if kind in (OperationKind.COMMIT, OperationKind.MERGE_MAIN, OperationKind.COMMIT_MERGE,
                    OperationKind.ABORT_MERGE, OperationKind.PUSH):
            return {"branch": target.branch, "commit": self.git.head(worktree).commit}
        if kind is OperationKind.PULL_REQUEST:
            return {"url": outputs[-1].stdout.strip()}
        if kind in (OperationKind.CREATE_FIX_WORKTREE, OperationKind.INIT_READONLY_WORKTREE):
            return {"worktree": str(worktree), "branch": target.branch}
        return {"worktree": str(worktree), "branch": target.branch, "removed": True}

    def _reconcile(self, operation: PendingOperation, target: Target | None) -> dict[str, Any] | None:
        """上次执行被中断时按实际状态判断是否已经完成；已完成时返回结果，未完成或无法判断时返回 None。"""
        if target is None or operation.kind in OUTPUT_KINDS:
            return None
        worktree = target.worktree or target.repo
        state = operation.preconditions.get("state", {})
        kind = operation.kind
        if kind in REUSABLE:
            if not worktree.exists():
                return None
            head = self.git.head(worktree)
            if kind is OperationKind.CREATE_FIX_WORKTREE and head.branch != target.branch:
                return None
            return {"worktree": str(worktree), "branch": target.branch}
        if kind is OperationKind.CLEANUP:
            gone = not worktree.exists() and not self.git.branch_exists(target.repo, target.branch or "")
            return {"worktree": str(worktree), "branch": target.branch, "removed": True} if gone else None
        if kind is OperationKind.PULL_REQUEST:
            number = open_pull(self.gh, target.repo, target.branch or "")
            if number is None or number == state.get("pullRequest"):
                return None
            return {"url": self.gh.pr_view(target.repo, str(number)).url}
        head = self.git.head(worktree).commit
        if kind is OperationKind.PUSH:
            try:
                remote = self.git.rev_parse(worktree, f"refs/remotes/origin/{target.branch}")
            except VcsError:
                return None
            return {"branch": target.branch, "commit": head} if remote == state.get("head") else None
        if self.git.merge_head(worktree) is not None or head is None:
            return None
        if kind is OperationKind.COMMIT_MERGE:
            merged = state.get("mergeHead")
            done = merged is not None and self.git.is_ancestor(worktree, merged, head)
            return {"branch": target.branch, "commit": head} if done else None
        if kind in (OperationKind.COMMIT, OperationKind.MERGE_MAIN):
            before = state.get("head")
            done = before is not None and head != before and self.git.is_ancestor(worktree, before, head)
            return {"branch": target.branch, "commit": head} if done else None
        return None
