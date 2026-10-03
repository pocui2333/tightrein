"""worktree 的查询与只读 worktree 的切换(architecture/02 4.6)。

只读 worktree 的切换只移动它自己的游离 HEAD，不建分支、不动用户的工作区，不需要逐次确认：目标 commit 在本地已存在时
不 fetch，否则(包括没有给出 commit、取 origin/<主分支> 时)先 fetch，fetch 失败或超时以 NetworkError 结束并写明需要
fetch 的原因；再确认它是干净的(被忽略的构建产物不算)，然后 `git checkout --detach <commit>`。它上面有 guards 的锁定标记时抛出 WorktreeLocked，
由调用方先执行 guards.recover；不干净说明有东西写入了只读 worktree，抛出 WorktreeDirty 并停止，不清理。
修复 worktree 的创建与清理是写操作，由 operations 构造待确认操作。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from tightrein.store.files.layout import WorkspaceLayout
from tightrein.vcs.errors import NetworkError, WorktreeDirty, WorktreeLocked
from tightrein.vcs.git_read import GitReader
from tightrein.vcs.parse import WorktreeInfo


@dataclass(frozen=True)
class SyncResult:
    worktree: Path
    previous: str | None
    commit: str

    @property
    def moved(self) -> bool:
        return self.previous != self.commit


def find(git: GitReader, repo: Path, worktree: Path) -> WorktreeInfo | None:
    """repo 的 worktree 列表中路径为 worktree 的一项。"""
    wanted = worktree.resolve()
    return next((item for item in git.worktree_list(repo) if Path(item.path).resolve() == wanted), None)


def sync_readonly(git: GitReader, layout: WorkspaceLayout, commit: str | None = None,
                  main_branch: str = "main") -> SyncResult:
    """把只读 worktree 切换到 commit；commit 为空时切换到 fetch 之后的 origin/<主分支>。"""
    worktree = layout.readonly_worktree()
    marker = layout.readonly_guard(worktree.name)
    if marker.exists():
        raise WorktreeLocked(f"{worktree} 上有锁定标记 {marker}，先恢复只读锁定")
    if commit is None or not git.has_commit(worktree, commit):
        wanted = commit or f"origin/{main_branch} 的最新提交"
        try:
            git.fetch(worktree)
        except NetworkError as error:
            raise NetworkError(f"本地没有 {wanted}，需要 fetch，但 fetch 失败：{error}；检查网络后重试，或用 --commit "
                               "指定本地已有的 commit", argv=error.argv, returncode=error.returncode,
                               stderr=error.stderr) from error
    status = git.status(worktree)
    if not status.clean:
        raise WorktreeDirty(f"只读 worktree {worktree} 中有未提交的改动，已停止，不做清理", paths=status.changed_paths)
    target = git.rev_parse(worktree, commit or f"origin/{main_branch}")
    previous = git.head(worktree).commit
    if previous != target:
        git.process.git(worktree, "checkout", "--quiet", "--detach", target)
    return SyncResult(worktree, previous, target)
