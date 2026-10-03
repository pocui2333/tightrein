"""推送(architecture/07 19.3)：将推送的提交；首次推送与 origin/main 比较。"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from tightrein.vcs.errors import VcsError


class PushReader(Protocol):
    def rev_parse(self, repo: Path, ref: str) -> str: ...

    def oneline(self, repo: Path, rev_range: str) -> str: ...


def commits_to_push(git: PushReader, worktree: Path, branch: str, main: str) -> str:
    try:
        git.rev_parse(worktree, f"refs/remotes/origin/{branch}")
        base = f"origin/{branch}"
    except VcsError:
        base = f"origin/{main}"
    return git.oneline(worktree, f"{base}..HEAD").strip()
