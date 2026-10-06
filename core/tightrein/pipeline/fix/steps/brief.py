"""分层代码摘要(code brief)：勘察通过后由程序生成一次，出计划、写复现测试、写代码与评审共用，各步不再各自重读代码。

- 核心：要改的与有问题的位置(勘察的 existing、problems 与 designIssue 的位置)，截取原文(前后各带 CONTEXT 行，最多 MAX_LINES 行)；
- 相关：可复用的实现、联动方与数据结构，只取所在的定义行(签名)与勘察的一句说明；
- 文件：涉及的全部文件，一行一个。
原文与签名由程序从修复 worktree 截取，不来自模型；勘察的位置在此之前已核对存在。写在 data/fixes/<编号>/brief.json。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from tightrein.store.files import atomic

FILE = "brief.json"
CORE_GROUPS = ("existing", "problems")
RELATED_GROUPS = ("reusable", "linkage", "dataStructure")
CONTEXT = 2
MAX_LINES = 40
LOCATION = re.compile(r"^(?P<path>.+?):(?P<start>\d+)(?:-(?P<end>\d+))?$")
# 定义行：Python、JS/TS、Go、C#、Java 等常见写法；找不到时用位置所在行
DEFINITION = re.compile(r"^\s*(?:(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:def|class|function|func|interface|type)\s|"
                        r"(?:public|private|protected|internal)\b[^;=]*\(|"
                        r"(?:export\s+)?(?:const|let|var)\s+\w+\s*=\s*(?:async\s*)?(?:\(|function))")
CORE_NOTE = ("以下原文由程序截取、位置已核对：直接采用，不要为了确认它们再去通读文件；只打开你要改动或核对的那几行。"
             "摘要里没有、又确实需要的代码再去查，并在输出中注明补看了哪里。")


def _parse(location: str) -> tuple[str, int, int] | None:
    found = LOCATION.match(location.strip().strip("`"))
    if found is None:
        return None
    start = int(found["start"])
    return found["path"], start, int(found["end"] or start)


def _lines(worktree: Path, path: str) -> list[str]:
    target = worktree / path
    if not target.is_file():
        return []
    return target.read_text(encoding="utf-8", errors="replace").splitlines()


def _excerpt(lines: Sequence[str], start: int, end: int) -> str:
    low, high = max(1, start - CONTEXT), min(len(lines), end + CONTEXT, start - CONTEXT + MAX_LINES - 1)
    return "\n".join(f"{number:>5}  {lines[number - 1]}" for number in range(low, high + 1))


def _signature(lines: Sequence[str], start: int) -> str:
    for number in range(min(start, len(lines)), 0, -1):
        if DEFINITION.match(lines[number - 1]):
            return f"{number}: {lines[number - 1].strip()}"
    return f"{start}: {lines[start - 1].strip()}" if 0 < start <= len(lines) else ""


def build(scouting: Mapping[str, Any], worktree: Path) -> dict[str, Any]:
    """由勘察结论生成摘要；同一位置只记一次，核心优先。"""
    core: list[dict[str, str]] = []
    related: list[dict[str, str]] = []
    seen: set[str] = set()
    findings = [(group, item) for group in (*CORE_GROUPS, *RELATED_GROUPS) for item in scouting.get(group) or []]
    design = scouting.get("designIssue") or {}
    findings += [("problems", {"location": location, "description": design.get("rootCause", "")})
                 for location in design.get("locations") or []]
    files: dict[str, None] = {}
    for group, item in findings:
        location = str(item.get("location", ""))
        parsed = _parse(location)
        if parsed is None or location in seen:
            continue
        seen.add(location)
        path, start, end = parsed
        files[path] = None
        lines = _lines(worktree, path)
        if not lines:
            continue
        description = str(item.get("description", ""))
        if group in CORE_GROUPS:
            core.append({"location": location, "description": description, "excerpt": _excerpt(lines, start, end)})
        else:
            related.append({"location": location, "description": description, "signature": _signature(lines, start)})
    return {"core": core, "related": related, "files": list(files)}


def render(brief: Mapping[str, Any] | None) -> str:
    """给执行器的「代码摘要」一节；没有摘要时为空。"""
    if not brief or not (brief.get("core") or brief.get("related")):
        return ""
    parts = ["## 代码摘要", "", CORE_NOTE, "", "<code_brief>"]
    if brief.get("core"):
        parts.append("### 核心(要改与有问题的位置)")
        for item in brief["core"]:
            parts += ["", f"`{item['location']}` {item['description']}", "```", item["excerpt"], "```"]
    if brief.get("related"):
        parts += ["", "### 相关(可复用、联动与数据结构，只列定义行)"]
        parts += [f"- `{item['location']}` {item['signature']}：{item['description']}" for item in brief["related"]]
    if brief.get("files"):
        parts += ["", "### 涉及的文件", *(f"- `{path}`" for path in brief["files"])]
    parts.append("</code_brief>")
    return "\n".join(parts)


def save(directory: Path, brief: Mapping[str, Any]) -> None:
    atomic.write_text(directory / FILE, json.dumps(brief, ensure_ascii=False, indent=2) + "\n")


def load(directory: Path) -> dict[str, Any] | None:
    path = directory / FILE
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
