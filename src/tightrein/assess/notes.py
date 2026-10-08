"""代码笔记：评估读了代码就留下，实施的定位、方案、编码、审查直接用，同一段代码在整条流水线中基本只读一遍。

格式与原修复环节的分层代码摘要(旧 pipeline/fix/steps/brief.py)统一：
- 核心(core)：要改或出问题的位置，由程序截取原文(前后各 CONTEXT 行，最多 MAX_LINES 行)；
- 相关(related)：可复用的实现、联动方与数据结构，只记所在的定义行(签名)与一句说明；
- 涉及的文件一行一个；另记推测的触发条件。
原文与签名一律由程序从只读 worktree 截取，不来自模型；模型只给位置与一句说明。
问题的笔记写在 `00-problem-notes.json`，写成 Issue 时复制为 `00-issue-notes.json`。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from tightrein.store.files.json import read_json, write_json
from tightrein.store.files.layout import WorkspaceLayout

CORE = "core"
RELATED = "related"
ROLES = (CORE, RELATED)
CONTEXT = 2
MAX_LINES = 40
NOTES = "notes"
LOCATION = re.compile(r"^(?P<path>[^\s:][^:]*?):(?P<start>\d+)(?:-(?P<end>\d+))?$")
# 定义行：Python、JS/TS、Go、C#、Java 等常见写法；找不到时用位置所在行
DEFINITION = re.compile(r"^\s*(?:(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:def|class|function|func|interface|type)\s|"
                        r"(?:public|private|protected|internal)\b[^;=]*\(|"
                        r"(?:export\s+)?(?:const|let|var)\s+\w+\s*=\s*(?:async\s*)?(?:\(|function))")
CORE_NOTE = ("以下原文由程序截取、位置已核对：直接采用，不要为了确认它们再去通读文件；只打开要改动或核对的那几行。"
             "笔记里没有、又确实需要的代码再去查，并在输出中注明补看了哪里。")


@dataclass(frozen=True)
class NoteEntry:
    location: str  # 文件:行号 或 文件:起-止
    role: str  # core、related
    description: str
    excerpt: str | None = None  # core：带行号的原文
    signature: str | None = None  # related：所在的定义行


@dataclass
class CodeNotes:
    subject: str
    commit: str  # 截取时的 commit
    entries: list[NoteEntry] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    trigger: str | None = None  # 推测的触发条件

    def add(self, findings: Sequence[Mapping[str, Any]], worktree: Path) -> None:
        """只补缺的：已记过的位置不再截取；同一位置先记为相关、后来成了核心的，改记为核心。"""
        known = {entry.location: entry for entry in self.entries}
        wanted = [item for item in findings
                  if str(item.get("location", "")) not in known
                  or (_role(item) == CORE and known[str(item["location"])].role == RELATED)]
        for entry in build_entries(wanted, worktree):
            if entry.location in known:
                self.entries = [entry if old.location == entry.location else old for old in self.entries]
            else:
                self.entries.append(entry)
            known[entry.location] = entry
            path = _parse(entry.location)
            if path is not None and path[0] not in self.files:
                self.files.append(path[0])

    def render(self) -> str:
        """给提示用的文本；没有条目时为「无」。"""
        if not self.entries and not self.trigger:
            return "无"
        parts = [f"截取于 commit {self.commit}。{CORE_NOTE}"]
        core = [entry for entry in self.entries if entry.role == CORE]
        related = [entry for entry in self.entries if entry.role == RELATED]
        if core:
            parts.append("### 核心(要改与出问题的位置)")
            for entry in core:
                parts += [f"`{entry.location}` {entry.description}", "```", entry.excerpt or "", "```"]
        if related:
            parts.append("### 相关(可复用、联动与数据结构，只列定义行)")
            parts += [f"- `{entry.location}` {entry.signature or ''}：{entry.description}" for entry in related]
        if self.files:
            parts += ["### 涉及的文件", *(f"- `{path}`" for path in self.files)]
        if self.trigger:
            parts += ["### 推测的触发条件", self.trigger]
        return "\n\n".join(parts)

    def to_json(self) -> dict[str, Any]:
        return {"subject": self.subject, "commit": self.commit, "trigger": self.trigger, "files": list(self.files),
                "entries": [asdict(entry) for entry in self.entries]}

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> CodeNotes:
        return cls(subject=data["subject"], commit=data["commit"], trigger=data.get("trigger"),
                   files=list(data.get("files") or []), entries=[NoteEntry(**item) for item in data["entries"]])


def build_entries(findings: Sequence[Mapping[str, Any]], worktree: Path) -> list[NoteEntry]:
    """findings 每条为 {location, description, role}(role 缺省为 core)；位置解析不了或文件不存在的跳过。
    同一位置只记一次，核心优先。"""
    chosen: dict[str, Mapping[str, Any]] = {}
    for item in findings:
        location = str(item.get("location", "")).strip().strip("`")
        if _parse(location) is None:
            continue
        if location not in chosen or (_role(item) == CORE and _role(chosen[location]) == RELATED):
            chosen[location] = item
    cache: dict[str, list[str]] = {}
    entries = []
    for location, item in chosen.items():
        path, start, end = _parse(location)  # type: ignore[misc]
        if path not in cache:
            cache[path] = _lines(worktree, path)
        lines = cache[path]
        if not lines or start > len(lines):
            continue
        description = str(item.get("description", ""))
        if _role(item) == CORE:
            entries.append(NoteEntry(location, CORE, description, excerpt=_excerpt(lines, start, end)))
        else:
            entries.append(NoteEntry(location, RELATED, description, signature=_signature(lines, start)))
    return entries


def path_of(layout: WorkspaceLayout, subject: str) -> Path:
    return layout.shared_file(subject, NOTES, "json")


def load(layout: WorkspaceLayout, subject: str) -> CodeNotes | None:
    path = path_of(layout, subject)
    return CodeNotes.from_json(read_json(path)) if path.is_file() else None


def save(layout: WorkspaceLayout, notes: CodeNotes) -> None:
    write_json(path_of(layout, notes.subject), notes.to_json())


def copy_to(layout: WorkspaceLayout, notes: CodeNotes, subject: str) -> CodeNotes:
    """问题的笔记交给 Issue(写成 Issue 时)；已有的 Issue 笔记按「只补缺」合并。"""
    existing = load(layout, subject)
    if existing is None:
        moved = CodeNotes(subject, notes.commit, list(notes.entries), list(notes.files), notes.trigger)
    else:
        moved = existing
        known = {entry.location for entry in moved.entries}
        moved.entries += [entry for entry in notes.entries if entry.location not in known]
        moved.files += [path for path in notes.files if path not in moved.files]
        moved.trigger = moved.trigger or notes.trigger
    save(layout, moved)
    return moved


def _role(item: Mapping[str, Any]) -> str:
    role = item.get("role") or CORE
    if role not in ROLES:
        raise ValueError(f"代码笔记的 role 只能是 {'、'.join(ROLES)}：{role!r}")
    return str(role)


def _parse(location: str) -> tuple[str, int, int] | None:
    found = LOCATION.match(location.strip().strip("`"))
    if found is None:
        return None
    start = int(found["start"])
    return found["path"], start, max(int(found["end"] or start), start)


def _lines(worktree: Path, path: str) -> list[str]:
    target = (worktree / path).resolve()
    if worktree.resolve() not in target.parents or not target.is_file():
        return []
    return target.read_text(encoding="utf-8", errors="replace").splitlines()


def _excerpt(lines: Sequence[str], start: int, end: int) -> str:
    low = max(1, start - CONTEXT)
    high = min(len(lines), end + CONTEXT, low + MAX_LINES - 1)
    return "\n".join(f"{number:>5}  {lines[number - 1]}" for number in range(low, high + 1))


def _signature(lines: Sequence[str], start: int) -> str:
    for number in range(min(start, len(lines)), 0, -1):
        if DEFINITION.match(lines[number - 1]):
            return f"{number}: {lines[number - 1].strip()}"
    return f"{start}: {lines[start - 1].strip()}"
