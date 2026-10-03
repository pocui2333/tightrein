"""归因(architecture/06 4.8)：只读的 git blame 与 PR 查询。

每处根因行取最后修改它的提交与作者，再查包含该提交的已合并 PR；多处根因指向不同提交时全部列出，第一处根因的提交
为主引入提交。查询失败(文件在取证 commit 上被移动、没有 gh 等)时对应字段为空并在 note 中说明，不影响判定。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from tightrein.domain.triage import IntroducedBy, RootCause
from tightrein.vcs.errors import VcsError
from tightrein.vcs.parse import BlameLine, PullState

UNCOMMITTED = "0" * 40


class BlameReader(Protocol):
    def blame(self, repo: Path, path: str, line_start: int, line_end: int, rev: str = "HEAD") -> list[BlameLine]: ...


class PullReader(Protocol):
    def pr_for_commit(self, repo: Path, commit: str) -> PullState | None: ...


@dataclass(frozen=True)
class Attribution:
    introduced_by: tuple[IntroducedBy, ...] = ()
    note: str | None = None


def attribute(git: BlameReader, prs: PullReader | None, repo: Path, worktree: Path, commit: str,
              root_causes: Sequence[RootCause]) -> Attribution:
    found: dict[str, IntroducedBy] = {}
    problems: list[str] = []
    for cause in root_causes:
        try:
            lines = git.blame(worktree, cause.file, cause.line, cause.line, commit)
        except VcsError as error:
            problems.append(f"{cause.file}:{cause.line} 的 git blame 失败：{error}")
            continue
        if not lines or lines[0].commit == UNCOMMITTED or lines[0].commit in found:
            continue
        blamed = lines[0]
        pr = None
        if prs is not None:
            try:
                pull = prs.pr_for_commit(repo, blamed.commit)
                pr = None if pull is None else pull.number
            except VcsError as error:
                problems.append(f"{blamed.commit[:12]} 的 PR 查询失败：{error}")
        found[blamed.commit] = IntroducedBy(blamed.commit, blamed.author, pr)
    return Attribution(tuple(found.values()), "；".join(problems) or None)
