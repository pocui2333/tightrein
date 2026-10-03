"""发现问题的版本到取证版本之间的差异事实(architecture/06 4.3)。

问题发现于 staging 部署的 commit(problem.last_seen_release)，取证在 main 的最新 commit 上进行。候选文件取自主张的入口
与信号中本项目堆栈帧、静态位置里的文件；两者之间修改过这些文件的提交作为事实交给取证角色，由角色判断根因是否已在 main
上被修改。查询只经注入的只读 git(log)；失败时写明原因，不影响分诊。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from tightrein.domain.signal import Signal
from tightrein.pipeline.triage.steps.claims import Claim
from tightrein.vcs.errors import VcsError
from tightrein.vcs.parse import Commit

LABEL = "main 差异"
UNCHANGED = "期间未修改"


class LogReader(Protocol):
    def log(self, repo: Path, rev_range: str, paths: Sequence[str] | None = None,
            limit: int | None = None) -> list[Commit]: ...


@dataclass(frozen=True)
class MainDiff:
    base: str | None
    head: str
    files: tuple[str, ...]
    commits: tuple[dict[str, str], ...] = ()
    note: str | None = None

    def value(self) -> Any:
        if self.note is not None:
            return self.note
        if not self.commits:
            return f"{self.base}..{self.head} 期间候选文件{UNCHANGED}：{'、'.join(self.files)}"
        return {"range": f"{self.base}..{self.head}", "files": list(self.files), "commits": list(self.commits)}


def _file(location: str) -> str | None:
    """「文件:行号」或「文件:符号」中的文件(带目录与扩展名的相对路径)；路由、页面与符号不算文件。"""
    candidate = location.partition(":")[0].strip()
    if " " in candidate or "/" not in candidate or candidate.startswith("/") or "." not in Path(candidate).name:
        return None
    return candidate


def candidate_files(claim: Claim, found: Sequence[Signal]) -> tuple[str, ...]:
    files = [_file(entry) for entry in claim.entry_points]
    for signal in found:
        files += [frame.get("file") for frame in signal.ctx("projectFrames") or [] if isinstance(frame, dict)]
        files.append(_file(signal.location))
    return tuple(dict.fromkeys(file for file in files if file))


def collect(git: LogReader, worktree: Path, base: str | None, head: str, files: Sequence[str]) -> MainDiff:
    if not files:
        return MainDiff(base, head, (), note="没有可定位的候选文件，未比较")
    if base is None:
        return MainDiff(base, head, tuple(files), note="发现问题时的版本未知，未比较")
    try:
        commits = git.log(worktree, f"{base}..{head}", list(files))
    except VcsError as error:
        return MainDiff(base, head, tuple(files), note=f"查询提交记录失败：{error}")
    return MainDiff(base, head, tuple(files),
                    tuple({"commit": commit.commit, "subject": commit.subject} for commit in commits))
