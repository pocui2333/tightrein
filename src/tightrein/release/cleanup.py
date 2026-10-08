"""清理：验收通过后删除本地的修复分支与修复目录，再 `fetch --prune`(远程分支在合并时已删)。

- 先确认 worktree 干净再 `git worktree remove`(不加 --force)；有未提交的内容就保留现场并说明，不删；
- 本地分支用 `git branch -d`，只删 git 认得出已合并的，从不用 -D：压缩合并时 git 可能认不出，这时保留分支并说明；
- 目录与本地分支都已不在的算完成，重复执行不出错(protocol/git/worktrees.cleanup)。
"""

from __future__ import annotations

from pathlib import Path

from tightrein.protocol.git import CommandFailed, WorktreeDirty
from tightrein.protocol.git.worktrees import cleanup as remove_fix
from tightrein.protocol.runtime import Runtime
from tightrein.release.record import POINT_CLEANUP

DONE = "done"


def cleanup(runtime: Runtime, issue: str, worktree: Path, branch: str) -> str:
    """返回 done，或保留下来的原因。"""
    try:
        remove_fix(runtime.git, worktree, branch, scope=runtime.scope(issue, POINT_CLEANUP))
    except WorktreeDirty as error:
        return f"修复目录 {worktree} 有未提交的内容，保留现场：{'、'.join(error.paths)}"
    except CommandFailed as error:
        return f"本地分支 {branch} 没有删除(git branch -d 认为它未合并，不用 -D)：{error.stderr or error}"
    return DONE
