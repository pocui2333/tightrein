"""git 状态的前后比较(architecture/02 3.6)。快照经 vcs 的只读查询取得，比较的是前后差异，不是绝对状态：
修复 worktree 中上一轮留下的未提交改动不算违规。

- HEAD：分支不同为 git-branch-switched；分支相同而 commit 不同为 git-commit-created；游离 HEAD 的 commit 变化时，
  新 commit 以原 commit 为祖先的算新建提交，否则算切换。
- 本地分支与标签：当前分支自身随提交前进已由上一项报告，不重复报告；其余增删改为 git-ref-changed。
- 远程配置、stash、worktree 列表(路径、锁定与其他 worktree 的分支；本 worktree 的分支由第一项比较)、
  合并变基拣选等状态文件的变化各有对应的违规类型。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tightrein.domain.enums import ViolationKind
from tightrein.guards.report import Violation
from tightrein.vcs.errors import RefNotFound
from tightrein.vcs.git_read import GitReader

SHORT = 12


@dataclass(frozen=True)
class GitSnapshot:
    head: str | None
    branch: str | None
    refs: dict[str, str]
    remotes: dict[str, tuple[str, ...]]
    stash: tuple[str, ...]
    worktrees: tuple[tuple[str, str | None, bool], ...]
    operations: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "head": self.head, "branch": self.branch, "refs": self.refs,
            "remotes": {key: list(values) for key, values in self.remotes.items()}, "stash": list(self.stash),
            "worktrees": [list(item) for item in self.worktrees], "operations": list(self.operations),
        }


def take(git: GitReader, repo: Path) -> GitSnapshot:
    """读取失败时抛出 VcsError，由调用方判为 git-unreadable。"""
    head = git.head(repo)
    own = os.path.realpath(repo)
    worktrees = tuple(sorted(
        (item.path, None if os.path.realpath(item.path) == own else item.branch, item.locked)
        for item in git.worktree_list(repo)
    ))
    return GitSnapshot(head.commit, head.branch, git.refs(repo), git.remotes(repo), git.stash(repo), worktrees,
                       git.operations_in_progress(repo))


def _short(commit: str | None) -> str:
    return commit[:SHORT] if commit else "(无)"


def _head(before: GitSnapshot, after: GitSnapshot, git: GitReader, repo: Path) -> list[Violation]:
    if before.branch != after.branch:
        return [Violation(ViolationKind.GIT_BRANCH_SWITCHED, None,
                          f"分支从 {before.branch or '游离 HEAD'} 切换为 {after.branch or '游离 HEAD'}")]
    if before.head == after.head:
        return []
    created = before.branch is not None
    if not created and before.head is not None and after.head is not None:
        try:
            created = git.is_ancestor(repo, before.head, after.head)
        except RefNotFound:
            created = False
    if created:
        return [Violation(ViolationKind.GIT_COMMIT_CREATED, None,
                          f"新建了提交 {after.head}(原 HEAD {before.head})；不会自动撤销，如需撤销由用户自行执行 "
                          f"git reset --soft {before.head}")]
    return [Violation(ViolationKind.GIT_BRANCH_SWITCHED, None,
                      f"HEAD 从 {_short(before.head)} 切换到 {_short(after.head)}")]


def compare(before: GitSnapshot, after: GitSnapshot, git: GitReader, repo: Path) -> list[Violation]:
    violations = _head(before, after, git, repo)
    own = f"refs/heads/{before.branch}" if before.branch is not None and before.branch == after.branch else None
    for name in sorted(set(before.refs) | set(after.refs)):
        if name != own and before.refs.get(name) != after.refs.get(name):
            violations.append(Violation(ViolationKind.GIT_REF_CHANGED, name,
                                        f"{_short(before.refs.get(name))} -> {_short(after.refs.get(name))}"))
    if before.remotes != after.remotes:
        changed = sorted(key for key in set(before.remotes) | set(after.remotes)
                         if before.remotes.get(key) != after.remotes.get(key))
        violations.append(Violation(ViolationKind.GIT_REMOTE_CHANGED, None, f"远程配置被修改：{', '.join(changed)}"))
    if before.stash != after.stash:
        violations.append(Violation(ViolationKind.GIT_STASH_CHANGED, None,
                                    f"stash 从 {len(before.stash)} 条变为 {len(after.stash)} 条"))
    if before.worktrees != after.worktrees:
        violations.append(Violation(ViolationKind.GIT_WORKTREE_CHANGED, None, "worktree 列表被修改"))
    started = sorted(set(after.operations) - set(before.operations))
    if started:
        violations.append(Violation(ViolationKind.GIT_OPERATION_STARTED, None,
                                    f"开始了未完成的 git 操作：{', '.join(started)}"))
    return violations
