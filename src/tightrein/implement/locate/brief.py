"""定位与代码笔记之间的一层：位置核对、模型输出转成笔记条目、笔记够不够用。

笔记的格式与截取在 assess/notes.py(评估与实施共用一份)：核心位置由程序截取原文(前后 2 行，最多 40 行)，相关位置
只记定义行；同一位置只记一次，核心优先。原文与签名由程序从修复 worktree 截取，不来自模型。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from tightrein.assess.checks import Snapshot, parse_location
from tightrein.assess.checks import complete as complete_locations
from tightrein.assess.notes import CORE, RELATED, CodeNotes

# 定位输出中写位置的键
LOCATION_KEYS = ("core", "related", "incidentalFindings")


def location_problem(worktree: Path, value: Any) -> str | None:
    """位置核对：接受 `文件:行号[-止]` 或 `{file, line}`；文件解析后必须在 worktree 之内(挡住 `../`)，结束行不超过文件
    行数。合格时返回 None。与评估的证据核对是同一份实现(assess/checks.py)，定位、方案的根因假说都用它。"""
    return Snapshot(worktree).problem(value)


def complete(output: Mapping[str, Any], worktree: Path) -> dict[str, Any]:
    """位置补全(与评估共用 assess/checks.complete)：只写了文件名或缺前几级目录的，按 worktree 中唯一的路径后缀补全；
    补不全的原样留下，由 check 判为不合格。"""
    return dict(complete_locations(output, Snapshot(worktree), keys=LOCATION_KEYS).value)


def findings(output: Mapping[str, Any]) -> list[dict[str, str]]:
    """定位的输出 → 笔记条目的输入：core 记核心(截原文)，related 记相关(只记签名)。"""
    return [{"location": item["location"], "description": item["description"], "role": role}
            for role, key in ((CORE, "core"), (RELATED, "related")) for item in output.get(key) or []]


def check(output: Mapping[str, Any], worktree: Path) -> list[str]:
    """每个位置都核对真实存在；不合格的带原因交回重做。"""
    return [f"位置不存在或越界：{problem}" for item in findings(output)
            if (problem := location_problem(worktree, item["location"])) is not None]


def sufficient(notes: CodeNotes | None, worktree: Path) -> bool:
    """评估留下的笔记够不够直接拟方案：有核心位置，且每个核心位置在修复 worktree 中仍然成立。"""
    if notes is None:
        return False
    core = [entry for entry in notes.entries if entry.role == CORE]
    return bool(core) and all(location_problem(worktree, entry.location) is None for entry in core)


def missing_core(notes: CodeNotes | None, worktree: Path) -> list[str]:
    """笔记中已不成立的核心位置(代码在评估之后变了)：交给定位重新确认。"""
    if notes is None:
        return []
    return [entry.location for entry in notes.entries
            if entry.role == CORE and location_problem(worktree, entry.location) is not None]


def files(entries: Sequence[Mapping[str, str]]) -> list[str]:
    return list(dict.fromkeys(parsed[0] for item in entries
                              if (parsed := parse_location(item["location"])) is not None))
