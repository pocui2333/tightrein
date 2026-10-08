"""基线审查(collect.static.baseline)：接入时一次，按目录分批审查现有代码本身(没有 diff)，每批一次只读调用。

- 取值在 controls."collect.static".baseline：maxClaims(基线取证的主张上限)、batchFiles、batchLines(每批的文件数与
  总行数上限)、exclude(排除的路径模式，写法同受保护文件，可用 exclude+ 追加)；
- 排除：匹配 exclude 的文件、开头 8 KB 中含 NUL 字节的文件(二进制)与空文件；
- 分批：文件按路径排序后按所在目录分组；相邻的小目录在不超过每批文件数与行数上限时并入同一批，减少调用次数；
  一个目录超过上限时按文件顺序拆成几批，单个文件超过行数上限时独占一批；
- 确定性工具的结果按文件分到所在的批，不在任何一批的(例如依赖清单)放进第一批，不会丢；
- 预算用尽(额度或用量到限)时其余批次不再运行，说明里写清还剩几批，改天再跑。
"""

from __future__ import annotations

import posixpath
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tightrein.agents.call import call
from tightrein.collect.static import review
from tightrein.collect.static.claims import ToolFinding
from tightrein.collect.static.review import BASELINE, Caller, Review
from tightrein.protocol.boundaries import matching_pattern
from tightrein.protocol.runtime import Runtime

BINARY_PROBE_BYTES = 8192
ROOT_MODULE = "(根目录)"


@dataclass(frozen=True)
class BaselineSettings:
    max_claims: int
    batch_files: int
    batch_lines: int
    exclude: tuple[str, ...]

    @classmethod
    def from_section(cls, section: Mapping[str, Any]) -> BaselineSettings:
        values = section["baseline"]
        return cls(int(values["maxClaims"]), int(values["batchFiles"]), int(values["batchLines"]),
                   tuple(values["exclude"]))


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


@dataclass
class BaselineRun:
    reviews: list[Review] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    reviewed: list[str] = field(default_factory=list)  # 审完的文件(按内容哈希记为已审)


def source_files(repo: Path, files: Sequence[str], exclude: Sequence[str]) -> tuple[list[SourceFile], int]:
    """返回(要审查的文件, 排除的文件数)。"""
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


def plan(repo: Path, files: Sequence[str], settings: BaselineSettings) -> BaselinePlan:
    kept, excluded = source_files(repo, files, settings.exclude)
    directories: dict[str, list[SourceFile]] = {}
    for item in kept:
        directories.setdefault(posixpath.dirname(item.path), []).append(item)
    groups: list[list[SourceFile]] = []
    current: list[SourceFile] = []
    for items in directories.values():
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


def assign_findings(batches: Sequence[Batch], findings: Sequence[ToolFinding]) -> list[list[ToolFinding]]:
    owner = {path: index for index, batch in enumerate(batches) for path in batch.paths}
    assigned: list[list[ToolFinding]] = [[] for _ in batches]
    if not assigned:
        return assigned
    for finding in findings:
        assigned[owner.get(finding.file, 0)].append(finding)
    return assigned


def run(runtime: Runtime, *, workdir: Path, files: Sequence[str], findings: Sequence[ToolFinding],
        settings: BaselineSettings, knowledge: str, caller: Caller = call) -> BaselineRun:
    """逐批审查；环境级越界或预算用尽时停下，其余批次不跑。"""
    planned = plan(workdir, files, settings)
    result = BaselineRun()
    if not planned.batches:
        result.notes.append(f"基线审查：排除 {planned.excluded} 个文件后没有要审查的源代码文件")
        return result
    for batch, assigned in zip(planned.batches, assign_findings(planned.batches, findings)):
        listing = "\n".join(f"- `{item.path}`({item.lines} 行)" for item in batch.files)
        variables = {"batch": f"{batch.describe()}，模块 `{batch.module}`", "files": listing,
                     "findings": review.findings_text(assigned), "knowledge": knowledge}
        done = review.to_review(f"基线审查{batch.describe()}", review.call_point(
            runtime, BASELINE, variables, workdir=workdir, round=batch.number, caller=caller))
        result.reviews.append(done)
        if done.ok:
            result.reviewed += batch.paths
        if done.environment_violated:
            return result
        remaining = batch.total - batch.number
        if done.exhausted and remaining:
            result.notes.append(f"额度或用量已到上限，其余 {remaining} 批未审查；之后再按 baseline 档运行")
            break
    result.notes.append(f"基线审查共 {len(planned.batches)} 批，排除 {planned.excluded} 个文件，"
                        f"已运行 {len(result.reviews)} 批")
    return result


def _line_count(content: bytes) -> int:
    if not content:
        return 0
    return content.count(b"\n") + (0 if content.endswith(b"\n") else 1)


def _module(paths: Sequence[str]) -> str:
    common = posixpath.commonpath([posixpath.dirname(path) for path in paths])
    return common or ROOT_MODULE


def _chunks(items: list[SourceFile], max_files: int, max_lines: int) -> list[list[SourceFile]]:
    """按顺序切分，每段不超过上限；单个文件超过行数上限时独占一段。"""
    chunks: list[list[SourceFile]] = []
    current: list[SourceFile] = []
    lines = 0
    for item in items:
        if current and (len(current) >= max_files or lines + item.lines > max_lines):
            chunks.append(current)
            current, lines = [], 0
        current.append(item)
        lines += item.lines
    if current:
        chunks.append(current)
    return chunks
