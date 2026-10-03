"""同步主干(architecture/07 19.2)：比较 origin/main、冲突文件的剩余标记与解决结果的取舍。"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

MARKERS = ("<<<<<<< ", ">>>>>>> ")
FIX_SIDE = "fix-side"
MAIN_SIDE = "main-side"
BOTH = "both"


def resolution(fix_side: str | None, main_side: str | None, resolved: str) -> str:
    """解决结果与修复侧一致、与主干侧一致，还是两侧合并。"""
    if resolved == fix_side:
        return FIX_SIDE
    if resolved == main_side:
        return MAIN_SIDE
    return BOTH


def markers_left(worktree: Path, files: Sequence[str]) -> list[str]:
    left = []
    for path in files:
        target = worktree / path
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines() if target.is_file() else []
        if any(line.startswith(MARKERS) for line in lines):
            left.append(path)
    return left


def hunks(text: str) -> list[tuple[str, str]]:
    """冲突标记之间两侧的片段：(修复侧，主干侧)。"""
    found: list[tuple[str, str]] = []
    ours: list[str] = []
    theirs: list[str] = []
    side = None
    for line in text.splitlines():
        if line.startswith("<<<<<<< "):
            side, ours, theirs = "ours", [], []
        elif line.startswith("=======") and side == "ours":
            side = "theirs"
        elif line.startswith(">>>>>>> ") and side == "theirs":
            found.append(("\n".join(ours), "\n".join(theirs)))
            side = None
        elif side == "ours":
            ours.append(line)
        elif side == "theirs":
            theirs.append(line)
    return found
