"""检查范围：上次成功巡检的目标 commit 到只读 worktree 的 HEAD；以及哪些文件值得交给模型审查。

档位：
- incremental(日常)：base..HEAD 中改动、且在 HEAD 中仍然存在的文件(删除的文件不审)；base 与 HEAD 相同(按前缀比较)
  时跳过；
- full(手动)：确定性工具扫全部文件，审查 base..HEAD 的改动，并做变体扫描；
- baseline(接入时一次)：确定性工具扫全部文件，按目录分批审查现有代码，并做变体扫描。
没有 base(第一次巡检)时不论要求哪一档都按 baseline。增量起点只在巡检成功(done、partial)时前进，失败的运行没审到的
改动下次还会审。

交给模型审查前再筛两遍(确定性工具照常跑全部范围)：
- 只改文档、测试、配置、锁文件的不审(nonCode 加项目的测试路径模式)；
- 同一份文件内容(按内容的 sha256)已审过的不审：例如改了又改回、基线审过之后没再动的。
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from tightrein.protocol.boundaries import matching_pattern
from tightrein.protocol.git import Git

NO_NEW_COMMITS = "上次巡检以来没有新提交"
READ_CHUNK = 1024 * 1024


class Level(StrEnum):
    INCREMENTAL = "incremental"
    FULL = "full"
    BASELINE = "baseline"


@dataclass(frozen=True)
class Scope:
    level: Level
    base: str | None
    head: str
    files: tuple[str, ...]  # 确定性工具的范围
    changed: tuple[str, ...]  # base..HEAD 中改动且仍存在的文件(没有 base 时为全部文件)

    @property
    def first_run(self) -> bool:
        return self.base is None

    @property
    def whole_repo(self) -> bool:
        """确定性工具不限于改动文件的档位。"""
        return self.level is not Level.INCREMENTAL


def resolve(git: Git, base: str | None, level: Level) -> Scope | None:
    """git 为只读 worktree 上的 Git；没有新提交的增量档返回 None(跳过)。"""
    head = git.head().commit
    if head is None:
        raise ValueError(f"{git.repo} 没有 HEAD")
    if base is None:
        level = Level.BASELINE
    elif level is Level.INCREMENTAL and head.startswith(base):
        return None
    everything = _existing(git.repo, git.ls_files())
    if base is None:
        return Scope(level, None, head, everything, everything)
    changed = _existing(git.repo, (change.path for change in git.numstat(base, head)))
    return Scope(level, base, head, changed if level is Level.INCREMENTAL else everything, changed)


def reviewable(paths: Iterable[str], non_code: Sequence[str]) -> tuple[list[str], int]:
    """去掉文档、测试、配置、锁文件；返回要审的文件与去掉的个数。"""
    kept, skipped = [], 0
    for path in paths:
        if matching_pattern(path, non_code) is None:
            kept.append(path)
        else:
            skipped += 1
    return kept, skipped


def content_hashes(root: Path, paths: Iterable[str]) -> dict[str, str]:
    found = {}
    for path in paths:
        digest = hashlib.sha256()
        with (root / path).open("rb") as handle:
            for chunk in iter(lambda: handle.read(READ_CHUNK), b""):
                digest.update(chunk)
        found[path] = digest.hexdigest()
    return found


def unreviewed(hashes: Mapping[str, str], reviewed: Mapping[str, str]) -> list[str]:
    """内容哈希与上次审过的不同(或没审过)的文件。"""
    return [path for path, digest in hashes.items() if reviewed.get(path) != digest]


def _existing(root: Path, paths: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted(path for path in set(paths) if (root / path).is_file()))
