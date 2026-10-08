"""为审查与取证准备输入：由程序截取 diff 与改动所在的整个函数，审查不再自己通读、搜索(同实施的代码笔记)。

- 改动的行取 diff 中新文件一侧的行号；向上找最近的定义行(def、class、function、func、带访问修饰符的方法等，写法与
  评估的代码笔记共用)，再向下找函数结束：定义行带 `{` 的按括号配平，否则按缩进(下一行非空且缩进不深于定义行即结束)；
- 函数超过 functionLines 行，或找不到定义行时，只取改动前后 contextLines 行；
- 同一文件的多个片段按行号合并、去重；全部片段超过 changesChars 时截断并写明，审查需要时自己再读；
- 取证只给程序截取的那一段(主张所在的函数，或前后几行)；
- 相关知识按本次的文件匹配(knowledge.match)，只把命中的条目拼进提示，有条数与 token 上限。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tightrein.assess.notes import DEFINITION
from tightrein.knowledge import entries as knowledge_entries
from tightrein.knowledge import match as knowledge_match
from tightrein.protocol.git import Git
from tightrein.store.files.layout import WorkspaceLayout

HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
NEW_FILE = re.compile(r"^\+\+\+ b/(.+)$")
TRUNCATED = "\n...(已截断：其余改动请按 diff 自己读)"
PATTERNS = "patterns"
CONVENTIONS = "conventions"
NONE = "(无)"


@dataclass(frozen=True)
class Limits:
    context_lines: int
    function_lines: int
    changes_chars: int
    knowledge_entries: int
    knowledge_tokens: int

    @classmethod
    def from_section(cls, section: Mapping[str, Any]) -> Limits:
        knowledge = section["knowledge"]
        return cls(int(section["contextLines"]), int(section["functionLines"]), int(section["changesChars"]),
                   int(knowledge["entries"]), int(knowledge["tokens"]))


def changed_lines(diff: str) -> dict[str, list[int]]:
    """diff 中每个文件新增或改动的行(新文件一侧的行号)。"""
    found: dict[str, list[int]] = {}
    current: str | None = None
    line = 0
    for text in diff.splitlines():
        header = NEW_FILE.match(text)
        if header is not None:
            current = header.group(1)
            continue
        hunk = HUNK.match(text)
        if hunk is not None:
            line = int(hunk.group(1))
            continue
        if current is None or not line or text.startswith(("---", "\\")):
            continue
        if text.startswith("+"):
            found.setdefault(current, []).append(line)
            line += 1
        elif not text.startswith("-"):
            line += 1
    return found


def enclosing(lines: Sequence[str], line: int, limits: Limits) -> tuple[int, int]:
    """改动所在函数的起止行(1 起)；找不到或函数太长时为前后 context_lines 行。"""
    fallback = (max(1, line - limits.context_lines), min(len(lines), line + limits.context_lines))
    start = next((number for number in range(min(line, len(lines)), 0, -1) if DEFINITION.match(lines[number - 1])),
                 None)
    if start is None:
        return fallback
    end = _function_end(lines, start)
    if end < line or end - start + 1 > limits.function_lines:
        return fallback
    return start, end


def changes_text(git: Git, base: str, head: str, files: Sequence[str], root: Path, limits: Limits) -> str:
    """交给审查的改动：diff 原文，加上每个文件改动所在的整个函数(带行号)。"""
    if not files:
        return NONE
    diff = git.diff(base, head, list(files))
    parts = ["## diff", f"```diff\n{diff.rstrip()}\n```", "## 改动所在的函数"]
    for path, numbers in changed_lines(diff).items():
        lines = (root / path).read_text(encoding="utf-8", errors="replace").splitlines()
        ranges = _merge([enclosing(lines, number, limits) for number in numbers if 0 < number <= len(lines)])
        parts += [f"### {path}", *(f"```\n{_numbered(lines, start, end)}\n```" for start, end in ranges)]
    text = "\n\n".join(parts)
    return text if len(text) <= limits.changes_chars else text[:limits.changes_chars] + TRUNCATED


def excerpt(root: Path, path: str, line: int, limits: Limits) -> str:
    """取证用：主张所在的函数(或前后几行)，带行号。"""
    target = root / path
    if not target.is_file():
        return NONE
    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    if not 0 < line <= len(lines):
        return NONE
    start, end = enclosing(lines, line, limits)
    return f"```\n{_numbered(lines, start, end)}\n```"


def knowledge(layout: WorkspaceLayout, paths: Sequence[str], limits: Limits) -> str:
    found = knowledge_match.match(layout, list(paths), limit_entries=limits.knowledge_entries,
                                  limit_tokens=limits.knowledge_tokens)
    return knowledge_match.render(found, limit_tokens=limits.knowledge_tokens)


def defect_patterns(layout: WorkspaceLayout) -> list[knowledge_entries.Entry]:
    """变体扫描的种子：有效的缺陷模式条目。"""
    return [entry for entry in knowledge_entries.active(knowledge_entries.load(layout).entries)
            if entry.kind == PATTERNS]


def tradeoffs(layout: WorkspaceLayout) -> str:
    """已接受的取舍与约定的摘要(变体扫描命中时写进 excluded)。"""
    found = [entry for entry in knowledge_entries.active(knowledge_entries.load(layout).entries)
             if entry.kind == CONVENTIONS]
    return "\n".join(f"- {entry.id} {entry.title}：{entry.summary}" for entry in found) or NONE


def _function_end(lines: Sequence[str], start: int) -> int:
    header = lines[start - 1]
    if "{" in header or (start < len(lines) and lines[start].strip().startswith("{")):
        return _brace_end(lines, start)
    indent = _indent(header)
    end = start
    for number in range(start + 1, len(lines) + 1):
        text = lines[number - 1]
        if not text.strip():
            continue
        # 多行参数表的收尾行(`) -> X:`)与定义行同缩进，不算函数结束
        if _indent(text) <= indent and not text.lstrip().startswith((")", "]")):
            break
        end = number
    return end


def _brace_end(lines: Sequence[str], start: int) -> int:
    depth = 0
    opened = False
    for number in range(start, len(lines) + 1):
        for char in lines[number - 1]:
            if char == "{":
                depth += 1
                opened = True
            elif char == "}":
                depth -= 1
        if opened and depth <= 0:
            return number
    return len(lines)


def _indent(text: str) -> int:
    return len(text) - len(text.lstrip())


def _merge(ranges: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _numbered(lines: Sequence[str], start: int, end: int) -> str:
    return "\n".join(f"{number:>5}  {lines[number - 1]}" for number in range(start, end + 1))
