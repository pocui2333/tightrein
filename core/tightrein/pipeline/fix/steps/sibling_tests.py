"""第 5 步写复现测试的参考：离根因文件最近的已有测试(redesign/05-fix.md 第 5 步)。

从仓库已跟踪的文件中挑出匹配 testPaths 与 fix.repro.testFilePatterns 的测试文件，按与根因文件的接近程度排序：
文件名包含根因文件名(去掉扩展名)的优先，其次是与根因文件共同的目录层数。取前 fix.repro.siblingTests 个，
每个取开头(导入、夹具、辅助函数)加完整的第一个测试，合计不超过 fix.repro.siblingLines 行，
交给写测试的模型照着写，沿用其中的导入路径与夹具，避免臆造模块路径。
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from tightrein.guards.protected import matching_pattern

# 常见测试框架中一个测试的开头：pytest/unittest、JS(jest/vitest/node:test/mocha)、Go、JUnit、xUnit/NUnit
TEST_START = re.compile(r"^\s*(async\s+def\s+test|def\s+test|class\s+Test|(test|it|describe)\s*\(|func\s+Test|@Test\b|"
                        r"\[(Fact|Test|Theory)\])")


NAME_MATCH = 100  # 文件名包含根因文件名时的分数，高于任何共同目录层数


@dataclass(frozen=True)
class SiblingTest:
    path: str
    excerpt: str


def find(tracked: Sequence[str], root_files: Sequence[str], test_paths: Sequence[str],
         patterns: Sequence[str], limit: int) -> list[str]:
    """离根因文件最近的已有测试文件；没有根因文件时按路径顺序取。"""
    tests = [path for path in tracked if matching_pattern(path, test_paths) is not None
             and any(fnmatch.fnmatch(PurePosixPath(path).name, pattern) for pattern in patterns)]
    return sorted(tests, key=lambda path: (-_closeness(path, root_files), path))[:limit]


def _closeness(test: str, root_files: Sequence[str]) -> int:
    """文件名包含根因文件名记 NAME_MATCH 分，再加共同的目录层数；取各根因文件中的最高分。"""
    test_path = PurePosixPath(test)
    best = 0
    for root in root_files:
        root_path = PurePosixPath(root)
        named = NAME_MATCH if root_path.stem and root_path.stem in test_path.stem else 0
        shared = len(set(root_path.parent.parts) & set(test_path.parent.parts))
        best = max(best, named + shared)
    return best


def excerpt(text: str, max_lines: int) -> str:
    """开头(导入、夹具、辅助函数)加完整的第一个测试，到第二个测试之前为止，不超过 max_lines 行。"""
    lines = text.splitlines()
    starts = [index for index, line in enumerate(lines) if TEST_START.match(line)]
    end = starts[1] if len(starts) > 1 else len(lines)
    return "\n".join(lines[:min(end, max_lines)]).rstrip()


def read(worktree: Path, paths: Sequence[str], max_lines: int) -> list[SiblingTest]:
    found = []
    for path in paths:
        try:
            text = (worktree / path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # 二进制或已删除的文件不作参考
        found.append(SiblingTest(path, excerpt(text, max_lines)))
    return found
