"""主干差异：发现问题的版本到取证版本之间改过候选文件的提交，作为事实交给取证。

问题发现于部署的 commit(problems.last_commit)，取证在 origin/主干的最新 commit 上(修复要提交到主干)。候选文件只取
像文件的(带目录与扩展名、不是路由)：主张的入口与信号中的堆栈帧、位置。由模型判断根因是否已在主干上修好
(fixedOnMain)；查询失败只写原因，不影响评估。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from tightrein.assess.claims import Claim, frames_of
from tightrein.protocol.git import Git, GitError
from tightrein.store.tables.occurrences import Occurrence

LABEL = "主干差异"


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
            return f"{self.base}..{self.head} 期间候选文件未修改：{'、'.join(self.files)}"
        return {"range": f"{self.base}..{self.head}", "files": list(self.files), "commits": list(self.commits)}


def file_of(location: str | None) -> str | None:
    """「文件:行号」或「文件:符号」中的文件(带目录与扩展名的相对路径)；路由、页面与符号不算文件。"""
    if not location:
        return None
    candidate = location.partition(":")[0].strip()
    if " " in candidate or "/" not in candidate or candidate.startswith("/") or "." not in PurePosixPath(candidate).name:
        return None
    return candidate


def candidate_files(claim: Claim, found: Sequence[Occurrence]) -> tuple[str, ...]:
    files = [file_of(entry) for entry in claim.entry_points]
    for item in found:
        files += [frame.get("file") for frame in frames_of(item)]
        files.append(file_of(item.evidence.get("location")))
    return tuple(dict.fromkeys(file for file in files if file))


def collect(git: Git, base: str | None, head: str, files: Sequence[str]) -> MainDiff:
    if not files:
        return MainDiff(base, head, (), note="没有可定位的候选文件，未比较")
    if base is None:
        return MainDiff(base, head, tuple(files), note="发现问题时的版本未知，未比较")
    if base == head:
        return MainDiff(base, head, tuple(files))
    try:
        commits = git.log(f"{base}..{head}", list(files))
    except GitError as error:
        return MainDiff(base, head, tuple(files), note=f"查询提交记录失败：{error}")
    return MainDiff(base, head, tuple(files),
                    tuple({"commit": commit.commit, "subject": commit.subject} for commit in commits))
