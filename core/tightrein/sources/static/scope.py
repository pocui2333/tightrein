"""静态巡检的扫描范围(architecture/04 5.2 第 1 步)：上次巡检的 commit 到只读 worktree 的 HEAD。

- incremental：base 与 HEAD 相同时跳过(没有新提交)；否则范围为 base..HEAD 中改动且在 HEAD 中仍然存在的文件；
  没有 base(第一次巡检)时范围为全部文件；
- full、baseline：范围为全部受检文件(已跟踪与未被忽略的文件)，changed_files 仍为 base..HEAD 的改动，供扩展与审查
  参考；没有 base 时 changed_files 为全部文件。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from tightrein.domain.enums import ProbeLevel
from tightrein.vcs.git_read import GitReader

NO_NEW_COMMITS = "上次巡检以来没有新提交"


@dataclass(frozen=True)
class Scope:
    level: ProbeLevel
    base_commit: str | None
    head: str
    files: tuple[str, ...]
    changed_files: tuple[str, ...]

    @property
    def first_run(self) -> bool:
        return self.base_commit is None

    @property
    def whole_repo(self) -> bool:
        """确定性工具与审查不限于改动文件的档位。"""
        return self.level is not ProbeLevel.INCREMENTAL


def _existing(repo: Path, paths: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted(path for path in set(paths) if (repo / path).is_file()))


def resolve(git: GitReader, repo: Path, base_commit: str | None, level: ProbeLevel) -> tuple[Scope | None, str | None]:
    """返回 (范围, 跳过原因)；HEAD 读不到时抛出 ValueError。"""
    head = git.head(repo).commit
    if head is None:
        raise ValueError(f"{repo} 没有 HEAD")
    if level is ProbeLevel.INCREMENTAL and base_commit is not None and head.startswith(base_commit):
        return None, NO_NEW_COMMITS
    changed = () if base_commit is None else _existing(repo, git.diff(repo, base_commit, head).paths)
    everything = _existing(repo, git.files(repo))
    files = everything if level is not ProbeLevel.INCREMENTAL or base_commit is None else changed
    return Scope(level, base_commit, head, files, changed if base_commit is not None else everything), None
