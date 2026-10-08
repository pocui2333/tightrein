"""归因：对每处根因行在取证 commit 上做 git blame，再查包含该提交的 PR。

去掉未提交的(全 0)与重复的提交；第一处根因的提交为主引入提交。blame 或 PR 查询失败只写进说明，不影响判定。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from tightrein.protocol.git import Git, GitError, GitHub

UNCOMMITTED = "0" * 40


@dataclass(frozen=True)
class Introduced:
    commit: str
    author: str | None
    pr: int | None

    def to_json(self) -> dict[str, Any]:
        return {"commit": self.commit, "author": self.author, "pr": self.pr}


@dataclass(frozen=True)
class Attribution:
    introduced_by: tuple[Introduced, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)


def attribute(git: Git, github: GitHub | None, commit: str, root_causes: Sequence[Mapping[str, Any]]) -> Attribution:
    """git 为只读 worktree 上的 Git；github 为空时不查 PR。"""
    found: dict[str, Introduced] = {}
    notes: list[str] = []
    for cause in root_causes:
        try:
            lines = git.blame(cause["file"], cause["line"], cause["line"], commit)
        except GitError as error:
            notes.append(f"{cause['file']}:{cause['line']} 的 git blame 失败：{error}")
            continue
        if not lines or lines[0].commit == UNCOMMITTED or lines[0].commit in found:
            continue
        blamed = lines[0]
        pr = None
        if github is not None:
            try:
                pull = github.pr_for_commit(blamed.commit)
                pr = None if pull is None else pull.number
            except GitError as error:
                notes.append(f"{blamed.commit[:12]} 的 PR 查询失败：{error}")
        found[blamed.commit] = Introduced(blamed.commit, blamed.author, pr)
    return Attribution(tuple(found.values()), tuple(notes))
