"""条目涉及的文件删除或大改时标为待确认(knowledge/README.md「过期」)。只比对文件，不调用模型。

- 基准是条目写入时项目的 commit(头信息 commit)；比对到当前的 HEAD；
- 文件：在 HEAD 中已删除，或增删行数之和与写入时的行数之比达到 changeRatio，即为大改；
- 目录前缀(`path:` 以 `/` 结尾)：其下已没有任何文件时才算删除；目录下的改动不逐个算；
- 同一个 commit 只取一次 numstat，基准 commit 已不存在(历史被改写)的条目跳过并给出警告。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from tightrein.knowledge.entries import PATH_TAG, Entry, active, load, mark_stale
from tightrein.protocol.boundaries import Change
from tightrein.store.files.layout import WorkspaceLayout

HEAD = "HEAD"


class FileHistory(Protocol):
    """用到的 protocol.git.Git 的只读方法。"""

    def has_commit(self, ref: str) -> bool: ...

    def numstat(self, base: str, head: str | None = None) -> list[Change]: ...

    def show(self, rev: str, path: str) -> str | None: ...

    def ls_files(self) -> tuple[str, ...]: ...


@dataclass(frozen=True)
class StaleMark:
    entry: str
    reason: str


@dataclass(frozen=True)
class StaleReport:
    marked: list[StaleMark]
    warnings: list[str]


def check(layout: WorkspaceLayout, git: FileHistory, *, change_ratio: float, today: str) -> StaleReport:
    """检查全部有效条目，把涉及的文件删除或大改的标为待确认并写回文件。"""
    loaded = load(layout)
    warnings = list(loaded.warnings)
    marked: list[StaleMark] = []
    tracked: frozenset[str] | None = None
    changes: dict[str, dict[str, Change]] = {}
    for entry in active(loaded.entries):
        files, directories = _paths(entry)
        if entry.commit is None or not (files or directories):
            continue
        if entry.commit not in changes:
            if not git.has_commit(entry.commit):
                warnings.append(f"{entry.id}：基准 commit {entry.commit} 已不存在，跳过过期检查")
                continue
            changes[entry.commit] = {change.path: change for change in git.numstat(entry.commit, HEAD)}
        if directories and tracked is None:
            tracked = frozenset(git.ls_files())
        reason = _reason(entry.commit, files, directories, changes[entry.commit], tracked or frozenset(), git,
                         change_ratio)
        if reason is not None:
            mark_stale(entry, reason, today)
            marked.append(StaleMark(entry.id, reason))
    return StaleReport(marked, warnings)


# 内部


def _paths(entry: Entry) -> tuple[list[str], list[str]]:
    values = [location[len(PATH_TAG):] for location in entry.locations if location.startswith(PATH_TAG)]
    return [value for value in values if not value.endswith("/")], [value for value in values if value.endswith("/")]


def _reason(commit: str, files: Sequence[str], directories: Sequence[str], changes: dict[str, Change],
            tracked: frozenset[str], git: FileHistory, change_ratio: float) -> str | None:
    for path in files:
        change = changes.get(path)
        if change is None:
            continue
        if change.status == "D":
            return f"{path} 已删除"
        before = git.show(commit, path)
        lines = before.count("\n") if before else 0
        ratio = (change.added + change.deleted) / max(lines, 1)
        if ratio >= change_ratio:
            return f"{path} 大改：增 {change.added} 行、删 {change.deleted} 行，写入时 {lines} 行"
    for directory in directories:
        if not any(path.startswith(directory) for path in tracked):
            return f"{directory} 下已没有文件"
    return None
