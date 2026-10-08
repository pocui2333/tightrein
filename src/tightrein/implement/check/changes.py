"""改动的读取：自检、审查、交付共用同一份。

- 改动 = 基准到工作目录的已跟踪文件改动 + 未跟踪的新文件(全部行算新增)：只看 git diff 会漏掉新文件；
- 补丁文本把未跟踪的新文件以新增文件的形式附在后面(跳过符号链接与非文本文件)，审查与交付看到的是完整改动；
- 文件状态(内容哈希)用来找出「这一轮新改了哪些文件」与「检查命令执行后工作区有没有被改」。
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from tightrein.protocol.boundaries import Change
from tightrein.protocol.git import Git

UNTRACKED = "?"
DIFF_HEADER = "diff --git a/"
HUNK = "@@"
ABSENT = ""


@dataclass(frozen=True)
class Lines:
    """一个文件新增与删除的行(不含 +/- 前缀)。"""

    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()


@dataclass(frozen=True)
class Changes:
    files: tuple[Change, ...]
    lines: Mapping[str, Lines]
    patch: str

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(change.path for change in self.files)

    @property
    def untracked(self) -> frozenset[str]:
        return frozenset(change.path for change in self.files if change.status == UNTRACKED)


def collect(git: Git, base: str) -> Changes:
    files = tuple(git.numstat(base))
    tracked = git.diff(base)
    lines = dict(parse_diff(tracked))
    new_files: list[str] = []
    for change in files:
        if change.status != UNTRACKED:
            continue
        text = _text(git.repo / change.path)
        if text is None:
            continue
        lines[change.path] = Lines(added=tuple(text.splitlines()))
        new_files.append(_new_file_patch(change.path, text))
    return Changes(files, lines, _joined([tracked, *new_files]))


def parse_diff(text: str) -> dict[str, Lines]:
    """统一格式 diff 中每个文件新增与删除的行。"""
    found: dict[str, tuple[list[str], list[str]]] = {}
    current: tuple[list[str], list[str]] | None = None
    in_hunk = False
    for line in text.splitlines():
        if line.startswith(DIFF_HEADER):
            path = line.rsplit(" b/", 1)[-1]
            current = found.setdefault(path, ([], []))
            in_hunk = False
        elif line.startswith(HUNK):
            in_hunk = True
        elif in_hunk and current is not None:
            if line.startswith("+"):
                current[0].append(line[1:])
            elif line.startswith("-"):
                current[1].append(line[1:])
    return {path: Lines(tuple(added), tuple(removed)) for path, (added, removed) in found.items()}


def select_files(patch: str, paths: Iterable[str]) -> str:
    """补丁中只留这些文件的部分(修正轮次只审这一轮又改了的文件)。"""
    wanted = set(paths)
    kept: list[str] = []
    keeping = False
    for line in patch.splitlines(keepends=True):
        if line.startswith(DIFF_HEADER):
            keeping = line.rstrip("\n").rsplit(" b/", 1)[-1] in wanted
        if keeping:
            kept.append(line)
    return "".join(kept)


def file_states(worktree: Path, paths: Iterable[str]) -> dict[str, str]:
    """各文件内容的 sha256；不存在的为空串。"""
    states = {}
    for path in paths:
        target = worktree / path
        states[path] = hashlib.sha256(target.read_bytes()).hexdigest() if target.is_file() else ABSENT
    return states


def changed_since(previous: Mapping[str, str], current: Mapping[str, str]) -> list[str]:
    """两次状态之间内容变了的文件(含新出现与消失的)。"""
    return sorted(path for path in set(previous) | set(current) if previous.get(path) != current.get(path))


def counted_lines(changes: Sequence[Change]) -> int:
    return sum(change.added + change.deleted for change in changes)


def _text(path: Path) -> str | None:
    if path.is_symlink() or not path.is_file():
        return None
    data = path.read_bytes()
    if b"\0" in data[:8192]:  # 二进制文件不进补丁
        return None
    return data.decode("utf-8", errors="replace")


def _new_file_patch(path: str, text: str) -> str:
    lines = text.splitlines()
    return (f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n+++ b/{path}\n"
            f"@@ -0,0 +1,{len(lines)} @@\n" + "".join(f"+{line}\n" for line in lines))


def _joined(parts: Sequence[str]) -> str:
    return "".join(part if not part or part.endswith("\n") else part + "\n" for part in parts)
