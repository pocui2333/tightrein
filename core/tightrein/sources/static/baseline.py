"""基线审查的分批(architecture/04 5.4)：把扫描范围内的源代码文件按目录模块分成若干批，每批一次只读审查任务。

- 取值在 sources.static.baseline：maxClaims(取证的主张上限)、batchFiles、batchLines(每批的文件数与总行数上限)、
  exclude(排除的路径模式，写法同 protectedPaths，可用 exclude+ 追加)；
- 排除：匹配 exclude 的文件、开头 8 KB 中含 NUL 字节的文件(二进制)与空文件；
- 分批：文件按路径排序后按所在目录分组；相邻的目录(小模块)在不超过每批的文件数与行数上限时并入同一批，
  减少审查调用次数；一个目录超过上限时按文件顺序拆成几批，单个文件超过行数上限时独占一批。
"""

from __future__ import annotations

import posixpath
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from tightrein.config.project import ProjectConfig
from tightrein.guards.protected import matching_pattern

SETTINGS = "sources.static.baseline"
BINARY_PROBE_BYTES = 8192
ROOT_MODULE = "(根目录)"


@dataclass(frozen=True)
class BaselineSettings:
    max_claims: int
    batch_files: int
    batch_lines: int
    exclude: tuple[str, ...]

    @classmethod
    def from_config(cls, config: ProjectConfig) -> BaselineSettings:
        return cls(int(config.get(f"{SETTINGS}.maxClaims")), int(config.get(f"{SETTINGS}.batchFiles")),
                   int(config.get(f"{SETTINGS}.batchLines")), tuple(config.get(f"{SETTINGS}.exclude")))


@dataclass(frozen=True)
class SourceFile:
    path: str
    lines: int


@dataclass(frozen=True)
class Batch:
    number: int
    total: int
    module: str
    files: tuple[SourceFile, ...]

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(item.path for item in self.files)

    @property
    def lines(self) -> int:
        return sum(item.lines for item in self.files)

    def describe(self) -> str:
        return f"第 {self.number}/{self.total} 批({self.module}，{len(self.files)} 个文件、{self.lines} 行)"


@dataclass(frozen=True)
class BaselinePlan:
    batches: tuple[Batch, ...]
    excluded: int


def _line_count(content: bytes) -> int:
    if not content:
        return 0
    return content.count(b"\n") + (0 if content.endswith(b"\n") else 1)


def source_files(repo: Path, files: Sequence[str], exclude: Sequence[str]) -> tuple[list[SourceFile], int]:
    """返回 (要审查的文件, 排除的文件数)。"""
    kept: list[SourceFile] = []
    for path in sorted(files):
        if matching_pattern(path, exclude) is not None:
            continue
        content = (repo / path).read_bytes()
        if b"\0" in content[:BINARY_PROBE_BYTES]:
            continue
        lines = _line_count(content)
        if lines:
            kept.append(SourceFile(path, lines))
    return kept, len(files) - len(kept)


def _module(paths: Sequence[str]) -> str:
    common = posixpath.commonpath([posixpath.dirname(path) for path in paths])
    return common or ROOT_MODULE


def _chunks(items: list[SourceFile], max_files: int, max_lines: int) -> list[list[SourceFile]]:
    """按顺序切分，每段不超过上限；单个文件超过行数上限时独占一段。"""
    chunks: list[list[SourceFile]] = []
    current: list[SourceFile] = []
    for item in items:
        if current and (len(current) >= max_files or sum(f.lines for f in current) + item.lines > max_lines):
            chunks.append(current)
            current = []
        current.append(item)
    if current:
        chunks.append(current)
    return chunks


def plan(repo: Path, files: Sequence[str], settings: BaselineSettings) -> BaselinePlan:
    kept, excluded = source_files(repo, files, settings.exclude)
    directories: dict[str, list[SourceFile]] = {}
    for item in kept:
        directories.setdefault(posixpath.dirname(item.path), []).append(item)
    groups: list[list[SourceFile]] = []
    current: list[SourceFile] = []
    for directory, items in directories.items():
        lines = sum(item.lines for item in items)
        fits = len(items) <= settings.batch_files and lines <= settings.batch_lines
        joinable = (current and fits and len(current) + len(items) <= settings.batch_files
                    and sum(item.lines for item in current) + lines <= settings.batch_lines)
        if joinable:
            current += items
            continue
        if current:
            groups.append(current)
        if fits:
            current = list(items)
        else:
            groups += _chunks(items, settings.batch_files, settings.batch_lines)
            current = []
    if current:
        groups.append(current)
    batches = tuple(Batch(number, len(groups), _module([item.path for item in group]), tuple(group))
                    for number, group in enumerate(groups, start=1))
    return BaselinePlan(batches, excluded)
