"""待确认操作(architecture/02 4.4)：git 与 gh 的写操作先构造成 PendingOperation 写入
pending_operations，交用户确认后才由 executor 执行。

- 每个操作记录将执行的每条命令、作用的仓库与分支、按文件名排序的文件清单、对工作区与历史的影响、是否影响远程、
  能否撤销与撤销方法；describe 把它们渲染成给用户看的说明。
- 前置条件是构造时观察到的状态(observe)，执行前以同样的参数重新观察并比较，任何一项不同即过期。
- 需要 fetch 的操作在构造时 fetch(只读)，执行步骤中不再 fetch：确认的就是实际合并或检出的 origin/main。
- 提交信息与 PR 描述写成文件放在 `raw/vcs/<操作编号>/`，以 `-F`、`--body-file` 传入，不经命令行转义。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from tightrein.config.project import ProjectConfig
from tightrein.contracts import validate
from tightrein.domain.clock import Clock, format_iso
from tightrein.domain.enums import OperationExecutor, OperationKind, OperationStatus, Stage
from tightrein.store import sequences
from tightrein.store.db import transaction
from tightrein.store.files import atomic
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.repos import pending_operations
from tightrein.store.repos.pending_operations import PendingOperationRecord
from tightrein.vcs.errors import VcsError
from tightrein.vcs.gh_read import OPEN, GhReader
from tightrein.vcs.git_read import GitReader

SCHEMA = "data/pending-operation.schema.json"
MESSAGE_FILE = "commit-message.txt"
BODY_FILE = "pr-body.md"
COMMENT_FILE = "comment.md"
DOUBLE_CONFIRMATION = 2
SINGLE_CONFIRMATION = 1


@dataclass(frozen=True)
class Step:
    argv: tuple[str, ...]
    cwd: Path
    description: str

    def to_dict(self) -> dict[str, Any]:
        return {"argv": list(self.argv), "cwd": str(self.cwd), "description": self.description}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Step:
        return cls(tuple(data["argv"]), Path(data["cwd"]), data["description"])


@dataclass(frozen=True)
class OperationDescription:
    repo: str
    branch: str | None
    files: tuple[str, ...]
    affects_remote: bool
    undo: str
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {"repo": self.repo, "branch": self.branch, "files": list(self.files),
                "affectsRemote": self.affects_remote, "undo": self.undo, "text": self.text}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> OperationDescription:
        return cls(data["repo"], data["branch"], tuple(data["files"]), data["affectsRemote"], data["undo"],
                   data["text"])


@dataclass(frozen=True)
class PendingOperation:
    id: str
    stage: Stage
    subject_id: str
    kind: OperationKind
    executor: OperationExecutor
    commands: tuple[Step, ...]
    description: OperationDescription
    impact: str
    reversible: bool
    preconditions: Mapping[str, Any]
    idempotency_key: str
    confirmations_required: int
    created_at: datetime
    confirmations_given: int = 0
    status: OperationStatus = OperationStatus.PENDING
    decided_at: datetime | None = None
    executed_at: datetime | None = None
    result: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """按 data/pending-operation.schema.json 校验后返回。"""
        data = {
            "id": self.id, "stage": self.stage.value, "subjectId": self.subject_id, "kind": self.kind.value,
            "executor": self.executor.value, "commands": [step.to_dict() for step in self.commands],
            "description": self.description.to_dict(), "impact": self.impact, "reversible": self.reversible,
            "preconditions": dict(self.preconditions), "idempotencyKey": self.idempotency_key,
            "confirmationsRequired": self.confirmations_required, "confirmationsGiven": self.confirmations_given,
            "status": self.status.value, "createdAt": format_iso(self.created_at),
            "decidedAt": None if self.decided_at is None else format_iso(self.decided_at),
            "executedAt": None if self.executed_at is None else format_iso(self.executed_at),
            "result": None if self.result is None else dict(self.result),
        }
        validate.check(SCHEMA, data)
        return data

    def to_record(self) -> PendingOperationRecord:
        return PendingOperationRecord(
            id=self.id, stage=self.stage, subject_id=self.subject_id, kind=self.kind, executor=self.executor,
            impact=self.impact, reversible=self.reversible, idempotency_key=self.idempotency_key,
            confirmations_required=self.confirmations_required, status=self.status, created_at=self.created_at,
            commands=[step.to_dict() for step in self.commands], description=self.description.to_dict(),
            preconditions=dict(self.preconditions), confirmations_given=self.confirmations_given,
            decided_at=self.decided_at, executed_at=self.executed_at,
            result=None if self.result is None else dict(self.result),
        )

    @classmethod
    def from_record(cls, record: PendingOperationRecord) -> PendingOperation:
        return cls(
            id=record.id, stage=record.stage, subject_id=record.subject_id, kind=record.kind, executor=record.executor,
            commands=tuple(Step.from_dict(step) for step in record.commands),
            description=OperationDescription.from_dict(record.description), impact=record.impact,
            reversible=record.reversible, preconditions=record.preconditions,
            idempotency_key=record.idempotency_key, confirmations_required=record.confirmations_required,
            created_at=record.created_at, confirmations_given=record.confirmations_given, status=record.status,
            decided_at=record.decided_at, executed_at=record.executed_at, result=record.result,
        )


def save(conn: sqlite3.Connection, operation: PendingOperation) -> PendingOperation:
    operation.to_dict()
    pending_operations.save(conn, operation.to_record())
    return operation


def load(conn: sqlite3.Connection, operation_id: str) -> PendingOperation:
    record = pending_operations.get(conn, operation_id)
    if record is None:
        raise LookupError(f"没有待确认操作 {operation_id}")
    return PendingOperation.from_record(record)


def sorted_files(files: Sequence[str]) -> tuple[str, ...]:
    """按文件名排序，文件名相同时按完整路径。"""
    return tuple(sorted(set(files), key=lambda path: (PurePosixPath(path).name, path)))


def describe(operation: PendingOperation) -> str:
    """给用户看的说明：命令、仓库与分支、文件清单、影响、是否影响远程、能否撤销与撤销方法。"""
    description = operation.description
    lines = [f"{operation.id} {operation.kind.label}({operation.kind.value})，对象 {operation.subject_id}",
             description.text, "将执行的命令："]
    for number, step in enumerate(operation.commands, start=1):
        lines.append(f"  {number}. {' '.join(step.argv)}  [在 {step.cwd}] {step.description}")
    if not operation.commands:
        lines.append("  (无)")
    lines.append(f"仓库：{description.repo}；分支：{description.branch or '(无)'}")
    if description.files:
        lines.append("文件(按文件名排序)：" + "、".join(description.files))
    lines.append(f"影响：{operation.impact}")
    lines.append(f"是否影响远程：{'是' if description.affects_remote else '否'}")
    lines.append(f"能否撤销：{'能' if operation.reversible else '不能'}；{description.undo}")
    if operation.confirmations_required == DOUBLE_CONFIRMATION:
        lines.append(f"删除类操作，需要确认 {DOUBLE_CONFIRMATION} 次；已确认 {operation.confirmations_given} 次")
    return "\n".join(lines)


NO_COMMAND_KINDS = frozenset({OperationKind.FIX_PLAN, OperationKind.LOCAL_MIGRATION})


def confirmation(conn: sqlite3.Connection, clock: Clock, *, stage: Stage, subject_id: str, kind: OperationKind,
                 repo: str, text: str, impact: str, preconditions: Mapping[str, Any], key: str) -> PendingOperation:
    """没有命令的待确认操作(修复计划、把迁移应用到测试库)：确认后由执行器直接记为 executed 并调用发起模块的后续处理。

    同一幂等键已有待确认的操作时直接返回它；前置条件(计划或迁移文件的哈希)由发起模块在后续处理中核对。
    """
    if kind not in NO_COMMAND_KINDS:
        raise ValueError(f"{kind.value} 须经 OperationPlanner 构造")
    for record in pending_operations.find(conn, subject_id=subject_id, status=OperationStatus.PENDING):
        if record.idempotency_key == key:
            return PendingOperation.from_record(record)
    with transaction(conn):
        operation = PendingOperation(
            id=sequences.next_operation_id(conn), stage=stage, subject_id=subject_id, kind=kind,
            executor=OperationExecutor.VCS, commands=(),
            description=OperationDescription(repo, None, (), False, "确认只记录决定，不改动仓库；拒绝后可以重新生成", text),
            impact=impact, reversible=True, preconditions=dict(preconditions), idempotency_key=key,
            confirmations_required=SINGLE_CONFIRMATION, created_at=clock.now(),
        )
        save(conn, operation)
    return operation


@dataclass(frozen=True)
class Target:
    """观察状态所需的参数：仓库、worktree 与分支。"""

    repo: Path
    worktree: Path | None = None
    branch: str | None = None
    main: str = "main"

    def to_dict(self) -> dict[str, Any]:
        return {"repo": str(self.repo), "worktree": None if self.worktree is None else str(self.worktree),
                "branch": self.branch, "main": self.main}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Target:
        return cls(Path(data["repo"]), None if data["worktree"] is None else Path(data["worktree"]), data["branch"],
                   data["main"])


def open_pull(gh: GhReader, repo: Path, branch: str) -> int | None:
    """该分支上打开的 PR 编号；没有时为 None。"""
    pull = gh.pr_for_branch(repo, branch)
    return pull.number if pull is not None and pull.state == OPEN else None


def observe(kind: OperationKind, target: Target, git: GitReader, gh: GhReader | None = None) -> dict[str, Any]:
    """与该类操作的前置条件相关的当前状态。"""
    worktree = target.worktree or target.repo
    if kind is OperationKind.CREATE_FIX_WORKTREE:
        return {"branchExists": git.branch_exists(target.repo, target.branch or ""),
                "worktreeExists": worktree.exists()}
    if kind is OperationKind.INIT_READONLY_WORKTREE:
        return {"worktreeExists": worktree.exists()}
    if kind in (OperationKind.GITHUB_ISSUE, OperationKind.MERGE_PULL_REQUEST, OperationKind.PR_COMMENT):
        return {}
    if kind is OperationKind.REVERT_PULL_REQUEST:
        return {"branchExists": git.branch_exists(target.repo, target.branch or ""),
                "worktreeExists": worktree.exists()}
    if kind is OperationKind.CLEANUP:
        return {"branchExists": git.branch_exists(target.repo, target.branch or ""),
                "worktreeExists": worktree.exists()}
    head = git.head(worktree)
    state: dict[str, Any] = {"branch": head.branch, "head": head.commit}
    if kind is OperationKind.COMMIT:
        state["diffHash"] = git.diff_hash(worktree, "HEAD")
    elif kind is OperationKind.MERGE_MAIN:
        state["originMain"] = git.rev_parse(worktree, f"origin/{target.main}")
    elif kind in (OperationKind.COMMIT_MERGE, OperationKind.ABORT_MERGE):
        state = {"branch": head.branch, "mergeHead": git.merge_head(worktree)}
    elif kind is OperationKind.PULL_REQUEST:
        if gh is None:
            raise ValueError("提 PR 的前置条件需要 gh")
        state["pullRequest"] = open_pull(gh, target.repo, target.branch or "")
    return state


@dataclass
class Draft:
    """构造中的操作；步骤参数中的 `{raw}` 在分配到操作编号后替换为 `raw/vcs/<操作编号>` 目录。"""

    kind: OperationKind
    stage: Stage
    subject_id: str
    target: Target
    steps: list[Step]
    files: Sequence[str]
    affects_remote: bool
    reversible: bool
    undo: str
    impact: str
    text: str
    key: str
    confirmations: int = SINGLE_CONFIRMATION
    extra: dict[str, Any] = field(default_factory=dict)


class OperationPlanner:
    """构造各类写操作并写入 pending_operations；构造只执行只读查询(含 fetch)，不改动仓库。"""

    def __init__(self, conn: sqlite3.Connection, git: GitReader, gh: GhReader, layout: WorkspaceLayout,
                 config: ProjectConfig, clock: Clock, run_id: str) -> None:
        self.conn = conn
        self.git = git
        self.gh = gh
        self.layout = layout
        self.config = config
        self.clock = clock
        self.run_id = run_id

    @property
    def repo(self) -> Path:
        return self.config.repo

    @property
    def main(self) -> str:
        return self.config.main_branch

    def _save(self, draft: Draft, files_to_write: Mapping[str, str] | None = None) -> PendingOperation:
        state = observe(draft.kind, draft.target, self.git, self.gh)
        with transaction(self.conn):
            operation_id = sequences.next_operation_id(self.conn)
            raw = self.layout.vcs_raw_dir(self.run_id, operation_id)
            steps = tuple(
                replace(step, argv=tuple(part.replace("{raw}", str(raw)) for part in step.argv)) for step in draft.steps
            )
            operation = PendingOperation(
                id=operation_id, stage=draft.stage, subject_id=draft.subject_id, kind=draft.kind,
                executor=OperationExecutor.VCS, commands=steps,
                description=OperationDescription(str(draft.target.worktree or draft.target.repo), draft.target.branch,
                                                 sorted_files(draft.files), draft.affects_remote, draft.undo,
                                                 draft.text),
                impact=draft.impact, reversible=draft.reversible,
                preconditions={"target": draft.target.to_dict(), "state": state, **draft.extra},
                idempotency_key=draft.key, confirmations_required=draft.confirmations, created_at=self.clock.now(),
            )
            save(self.conn, operation)
        for name, text in (files_to_write or {}).items():
            atomic.write_text(raw / name, text)
        return operation

    def _git_step(self, cwd: Path, description: str, *args: str) -> Step:
        return Step(("git", *args), cwd, description)

    def plan_create_fix_worktree(self, issue_id: str, branch: str) -> PendingOperation:
        worktree = self.layout.fix_worktree(issue_id)
        self.git.fetch(self.repo)
        base = self.git.rev_parse(self.repo, f"origin/{self.main}")
        steps = [self._git_step(self.repo, f"从 origin/{self.main} 新建分支与 worktree", "worktree", "add", "-b", branch,
                                str(worktree), base)]
        for link in self.config.data.get("git", {}).get("worktreeLinks", []):
            name = link.strip("/")
            steps.append(Step(("ln", "-s", str(self.repo / name), str(worktree / name)), self.repo,
                              f"从项目主工作区链接 {name}"))
        return self._save(Draft(
            OperationKind.CREATE_FIX_WORKTREE, Stage.FIX, issue_id, Target(self.repo, worktree, branch, self.main),
            steps,
            (), False, True, f"撤销方法：git worktree remove {worktree}；git branch -d {branch}",
            "新建本地分支与 worktree 目录，不影响远程",
            f"从 origin/{self.main}({base[:12]})新建修复分支 {branch} 与 worktree {worktree}",
            f"worktree:{issue_id}", extra={"base": base},
        ))

    def plan_init_readonly_worktree(self) -> PendingOperation:
        """本地已有 origin/<主分支> 时直接用它，不 fetch(之后每次运行会把只读 worktree 切到目标 commit)。"""
        worktree = self.layout.readonly_worktree()
        if not self.git.has_commit(self.repo, f"origin/{self.main}"):
            self.git.fetch(self.repo)
        base = self.git.rev_parse(self.repo, f"origin/{self.main}")
        steps = [self._git_step(self.repo, "新建游离 HEAD 的只读 worktree", "worktree", "add", "--detach", str(worktree),
                                base)]
        return self._save(Draft(
            OperationKind.INIT_READONLY_WORKTREE, Stage.COLLECT, self.config.name, Target(self.repo, worktree, None,
                                                                                         self.main),
            steps, (), False, True, f"撤销方法：git worktree remove {worktree}",
            "新建一个游离 HEAD 的 worktree，不建分支、不影响远程；此后每次运行会把它切换到目标 commit",
            f"在 {worktree} 初始化只读 worktree，检出 origin/{self.main}({base[:12]})", "readonly-worktree",
            extra={"base": base},
        ))

    def plan_commit(self, issue_id: str, worktree: Path, message: str, files: Sequence[str]) -> PendingOperation:
        if not files:
            raise ValueError("提交的文件清单不能为空")
        head = self.git.head(worktree)
        diff_hash = self.git.diff_hash(worktree, "HEAD")
        steps = [
            self._git_step(worktree, "暂存本次修复的文件", "add", "--", *sorted_files(files)),
            self._git_step(worktree, "以提交信息文件提交", "commit", "-F", f"{{raw}}/{MESSAGE_FILE}"),
        ]
        return self._save(Draft(
            OperationKind.COMMIT, Stage.RELEASE, issue_id, Target(self.repo, worktree, head.branch, self.main), steps,
            files, False, True, "撤销方法：推送前由用户执行 git reset --soft HEAD~1 撤销该提交",
            "修复分支新增一个提交，不影响远程", f"提交信息：\n{message}", f"commit:{issue_id}:{diff_hash}",
        ), {MESSAGE_FILE: message if message.endswith("\n") else message + "\n"})

    def plan_merge_main(self, issue_id: str, worktree: Path) -> PendingOperation:
        self.git.fetch(worktree)
        head = self.git.head(worktree)
        origin_main = self.git.rev_parse(worktree, f"origin/{self.main}")
        steps = [self._git_step(worktree, f"把 origin/{self.main} 合并进修复分支", "merge", "--no-ff", "--no-edit",
                                f"origin/{self.main}")]
        return self._save(Draft(
            OperationKind.MERGE_MAIN, Stage.RELEASE, issue_id, Target(self.repo, worktree, head.branch, self.main),
            steps, (), False, True, "撤销方法：合并中可以 git merge --abort，完成后推送前可以回到合并前的 commit "
            f"{head.commit}", "修复分支新增合并提交；有冲突时停在合并中",
            f"合并 origin/{self.main}({origin_main[:12]})，使用 merge，不使用 rebase", f"merge:{head.branch}:{origin_main}",
        ))

    def _merge_head(self, worktree: Path) -> str:
        merge_head = self.git.merge_head(worktree)
        if merge_head is None:
            raise VcsError(f"{worktree} 没有进行中的合并")
        return merge_head

    def plan_commit_merge(self, issue_id: str, worktree: Path, conflict_files: Sequence[str]) -> PendingOperation:
        merge_head = self._merge_head(worktree)
        head = self.git.head(worktree)
        steps = [
            self._git_step(worktree, "暂存解决冲突后的文件", "add", "--", *sorted_files(conflict_files)),
            self._git_step(worktree, "完成合并提交", "commit", "--no-edit"),
        ]
        return self._save(Draft(
            OperationKind.COMMIT_MERGE, Stage.RELEASE, issue_id, Target(self.repo, worktree, head.branch, self.main),
            steps, conflict_files, False, True, f"撤销方法：推送前可以回到合并前的 commit {head.commit}",
            "用户解决冲突后完成合并提交，不影响远程", "完成冲突解决后的合并提交",
            f"merge-commit:{head.branch}:{merge_head}",
        ))

    def plan_abort_merge(self, issue_id: str, worktree: Path) -> PendingOperation:
        merge_head = self._merge_head(worktree)
        head = self.git.head(worktree)
        steps = [self._git_step(worktree, "放弃进行中的合并", "merge", "--abort")]
        return self._save(Draft(
            OperationKind.ABORT_MERGE, Stage.RELEASE, issue_id, Target(self.repo, worktree, head.branch, self.main),
            steps, (), False, False, "不需要撤销：放弃后可以重新执行 release sync 再次合并",
            "放弃进行中的合并，丢弃合并中的全部改动", "放弃合并 origin/main", f"abort:{head.branch}:{merge_head}",
        ))

    def plan_push(self, issue_id: str, worktree: Path, branch: str) -> PendingOperation:
        head = self.git.head(worktree)
        steps = [self._git_step(worktree, f"推送到 origin/{branch}", "push", "--porcelain", "-u", "origin", branch)]
        return self._save(Draft(
            OperationKind.PUSH, Stage.RELEASE, issue_id, Target(self.repo, worktree, branch, self.main), steps, (),
            True, False, "远程上的提交不做强制撤销；可以关闭 PR 或推送新的修正提交", "远程新建或更新该分支",
            f"推送分支 {branch}，包含 commit {head.commit}", f"push:{branch}:{head.commit}",
        ))

    def plan_pull_request(self, issue_id: str, worktree: Path, branch: str, title: str,
                          body: str) -> PendingOperation | None:
        """分支没有打开的 PR 时创建；有打开的 PR 时只在描述有变化时更新；没有变化时返回 None。"""
        existing = open_pull(self.gh, self.repo, branch)
        body_file = f"{{raw}}/{BODY_FILE}"
        if existing is None:
            steps = [Step(("gh", "pr", "create", "--base", self.main, "--head", branch, "--title", title, "--body-file",
                           body_file), worktree, "创建 PR")]
            text = f"创建 PR：{title}(目标分支 {self.main})"
            impact = "GitHub 上新建 PR"
        else:
            if self.gh.pr_body(self.repo, existing).strip() == body.strip():
                return None
            steps = [Step(("gh", "pr", "edit", str(existing), "--body-file", body_file), worktree,
                          f"更新 PR #{existing} 的描述")]
            text = f"更新 PR #{existing} 的描述"
            impact = "GitHub 上更新 PR 的描述"
        return self._save(Draft(
            OperationKind.PULL_REQUEST, Stage.RELEASE, issue_id, Target(self.repo, worktree, branch, self.main), steps,
            (), True, True, "撤销方法：在 GitHub 上关闭 PR", impact, text, f"pr:{branch}",
        ), {BODY_FILE: body if body.endswith("\n") else body + "\n"})

    def plan_github_issue(self, issue_id: str, steps: Sequence[tuple[tuple[str, ...], str]], files: Mapping[str, str],
                          text: str, key: str, extra: Mapping[str, Any]) -> PendingOperation:
        """GitHub Issue 镜像的一次对齐(建 Issue、改标签、发评论、关闭或重开)：steps 为 (gh 的参数, 说明)，参数中的
        `{raw}` 换成操作目录，files 为写进该目录的正文；extra 记下执行后由发起模块登记的结果所需的内容。没有前置状态。"""
        return self._save(Draft(
            OperationKind.GITHUB_ISSUE, Stage.ISSUE, issue_id, Target(self.repo, None, None, self.main),
            [Step(("gh", *args), self.repo, description) for args, description in steps], (), True, True,
            "撤销方法：在 GitHub 上关闭或编辑该 Issue、删除评论", "GitHub 上新建或更新镜像 Issue(标签、评论、开关状态)",
            text, key, extra=dict(extra),
        ), files)

    def plan_merge_pull_request(self, issue_id: str, number: int, slug: str, branch: str, head: str,
                                method: str, *, auto: bool = False) -> PendingOperation:
        """合并 PR 并删除远程修复分支(gates.merge)。带 --repo 时 gh 不删除本地分支(本地分支与 worktree 仍由
        cleanup 删除)；--match-head-commit 保证合并的就是判断合并条件时的头部 commit。auto 为真时开启 GitHub 原生的
        自动合并(仓库有分支保护与必需检查时)：由平台在必需检查全部通过后合并。没有前置状态。"""
        flags = ("--auto",) if auto else ()
        what = "开启 PR #{number} 的自动合并(检查通过后由 GitHub 合并)" if auto else "合并 PR #{number}"
        steps = [Step(("gh", "pr", "merge", str(number), f"--{method}", *flags, "--delete-branch",
                       "--match-head-commit", head, "--repo", slug), self.repo,
                      what.format(number=number) + f"，以 {method} 合并并删除远程分支 {branch}")]
        return self._save(Draft(
            OperationKind.MERGE_PULL_REQUEST, Stage.RELEASE, issue_id, Target(self.repo, None, branch, self.main),
            steps, (), True, False, "合并后不做强制撤销；需要时提撤销该合并的 PR(release revert)，远程分支可从 PR 页面恢复",
            f"{self.main} 新增合并提交，删除远程分支 {branch}",
            f"{what.format(number=number)}({method})，头部 commit {head[:12]}",
            f"merge-pr:{number}:{head}" + (":auto" if auto else ""),
        ))

    def plan_pr_comment(self, issue_id: str, number: int, slug: str, body: str, key: str) -> PendingOperation:
        """在 PR 上发一条评论(AI 评审结论，只作说明)。没有前置状态。"""
        steps = [Step(("gh", "pr", "comment", str(number), "--body-file", f"{{raw}}/{COMMENT_FILE}", "--repo", slug),
                      self.repo, f"在 PR #{number} 上发评论")]
        return self._save(Draft(
            OperationKind.PR_COMMENT, Stage.RELEASE, issue_id, Target(self.repo, None, None, self.main), steps, (),
            True, True, "撤销方法：在 GitHub 上删除该评论", "GitHub 上 PR 新增一条评论", f"在 PR #{number} 上发 AI 评审结论",
            key,
        ), {COMMENT_FILE: body if body.endswith("\n") else body + "\n"})

    def plan_revert_pull_request(self, issue_id: str, merge_commit: str, branch: str, title: str, body: str,
                                 mainline: bool) -> PendingOperation:
        """提撤销某次合并的 PR：从 origin/<主分支> 建分支与临时 worktree，git revert 合并提交(merge 方式合并的加
        -m 1)，推送并创建 PR；不合并，交用户决定。构造时 fetch(只读)。"""
        worktree = self.layout.fix_worktree(issue_id).with_name(f"revert-{issue_id}")
        self.git.fetch(self.repo)
        base = self.git.rev_parse(self.repo, f"origin/{self.main}")
        revert = ("revert", "--no-edit", *(("-m", "1") if mainline else ()), merge_commit)
        steps = [
            self._git_step(self.repo, f"从 origin/{self.main} 新建撤销分支与 worktree", "worktree", "add", "-b", branch,
                           str(worktree), base),
            self._git_step(worktree, f"撤销合并提交 {merge_commit[:12]}", *revert),
            self._git_step(worktree, f"推送到 origin/{branch}", "push", "--porcelain", "-u", "origin", branch),
            Step(("gh", "pr", "create", "--base", self.main, "--head", branch, "--title", title, "--body-file",
                  f"{{raw}}/{BODY_FILE}"), worktree, "创建撤销 PR(不合并)"),
        ]
        return self._save(Draft(
            OperationKind.REVERT_PULL_REQUEST, Stage.RELEASE, issue_id, Target(self.repo, worktree, branch, self.main),
            steps, (), True, True, f"撤销方法：在 GitHub 上关闭撤销 PR；删除远程与本地分支 {branch}、worktree {worktree}",
            f"远程新建分支 {branch} 与一个撤销 PR，{self.main} 不变", f"提撤销合并提交 {merge_commit[:12]} 的 PR：{title}",
            f"revert:{issue_id}:{merge_commit}", extra={"base": base, "mergeCommit": merge_commit},
        ), {BODY_FILE: body if body.endswith("\n") else body + "\n"})

    def plan_cleanup(self, issue_id: str, worktree: Path, branch: str) -> PendingOperation:
        steps = [
            self._git_step(self.repo, "删除修复 worktree(有未提交改动时 git 会拒绝)", "worktree", "remove", str(worktree)),
            self._git_step(self.repo, "删除本地修复分支(只删除已合并的分支)", "branch", "-d", branch),
            self._git_step(self.repo, "清理已删除的远程分支记录", "fetch", "--prune"),
        ]
        return self._save(Draft(
            OperationKind.CLEANUP, Stage.RELEASE, issue_id, Target(self.repo, worktree, branch, self.main), steps, (),
            False, True, "git branch -d 只删除已合并的分支；删除后可按远程分支或合并提交重建",
            "删除 worktree 目录与本地分支", f"删除修复 worktree {worktree} 与本地分支 {branch}", f"cleanup:{issue_id}",
            confirmations=DOUBLE_CONFIRMATION,
        ))
