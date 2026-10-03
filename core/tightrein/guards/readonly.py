"""只读锁定与恢复(architecture/02 3.5)。

锁定：先写标记文件 `data/guards/readonly-<worktree 名>.json`(worktree 路径、运行编号、进程号、主机、开始时间，以及每个
目录与文件原有的权限)，再对 worktree 中的全部目录与文件去掉写权限，不跟随符号链接；worktree 根目录的 `.git` 为文件时
同样去掉写权限，为目录(主仓库)时不处理。标记先于任何权限修改写入，中途崩溃也能按标记恢复。
恢复：按标记逐一还原原有权限(原本就不可写的保持不可写)，完成后删除标记；恢复失败时保留标记，由下次启动的 recover 重试。
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from tightrein.domain.clock import Clock, format_iso, parse_iso
from tightrein.store.files import atomic
from tightrein.store.locks import Holder, current_holder, process_alive

WRITE_BITS = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
MARKER_PATTERN = "readonly-*.json"
ROOT = "."

Chmod = Callable[[Path, int], None]


class ReadonlyError(Exception):
    pass


class ReadonlyLockError(ReadonlyError):
    """锁定失败；已锁定的部分已经恢复。"""


class ReadonlyRestoreError(ReadonlyError):
    """恢复失败；标记文件保留。"""

    def __init__(self, marker_path: Path, worktree: Path, reason: str) -> None:
        self.marker_path = marker_path
        self.worktree = worktree
        super().__init__(
            f"{worktree} 的写权限恢复失败({reason})；标记文件 {marker_path} 已保留，下次启动时自动重试，"
            f"也可以执行 chmod -R u+w {worktree} 后删除标记文件"
        )


@dataclass(frozen=True)
class ReadonlyMarker:
    worktree: Path
    run_id: str | None
    pid: int
    host: str
    started_at: datetime
    modes: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "worktree": str(self.worktree), "runId": self.run_id, "pid": self.pid, "host": self.host,
            "startedAt": format_iso(self.started_at), "modes": self.modes,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ReadonlyMarker:
        return cls(Path(data["worktree"]), data["runId"], int(data["pid"]), data["host"], parse_iso(data["startedAt"]),
                   {key: int(value) for key, value in data["modes"].items()})


def set_mode(path: Path, mode: int) -> None:
    os.chmod(path, mode, follow_symlinks=False)


def entries(worktree: Path) -> dict[str, int]:
    """worktree 中全部目录与文件(不含符号链接与 .git 目录)的原有权限；键为相对路径，根目录为 `.`。"""
    modes = {ROOT: stat.S_IMODE(os.lstat(worktree).st_mode)}
    for directory, directories, files in os.walk(worktree, followlinks=False):
        base = Path(directory)
        if base == worktree and ".git" in directories:
            directories.remove(".git")
        for name in [*directories, *files]:
            path = base / name
            info = os.lstat(path)
            if not stat.S_ISLNK(info.st_mode):
                modes[path.relative_to(worktree).as_posix()] = stat.S_IMODE(info.st_mode)
    return modes


def read_marker(marker_path: Path) -> ReadonlyMarker:
    return ReadonlyMarker.from_dict(json.loads(marker_path.read_text(encoding="utf-8")))


def lock(worktree: Path, marker_path: Path, clock: Clock, *, run_id: str | None = None,
         holder: Holder | None = None, chmod: Chmod = set_mode) -> ReadonlyMarker:
    owner = holder or current_holder()
    marker = ReadonlyMarker(worktree, run_id, owner.pid, owner.host, clock.now(), entries(worktree))
    atomic.write_text(marker_path, json.dumps(marker.to_dict(), ensure_ascii=False, indent=2) + "\n")
    try:
        for relative, mode in marker.modes.items():
            chmod(worktree / relative, mode & ~WRITE_BITS)
    except OSError as error:
        restore(marker_path, chmod=chmod)
        raise ReadonlyLockError(f"{worktree} 无法设为只读：{type(error).__name__}: {error}") from error
    return marker


def restore(marker_path: Path, *, chmod: Chmod = set_mode) -> ReadonlyMarker:
    marker = read_marker(marker_path)
    try:
        for relative, mode in marker.modes.items():
            try:
                chmod(marker.worktree / relative, mode)
            except FileNotFoundError:
                continue
    except OSError as error:
        raise ReadonlyRestoreError(marker_path, marker.worktree, f"{type(error).__name__}: {error}") from error
    marker_path.unlink()
    return marker


def recover(guards_dir: Path, *, alive: Callable[[int], bool] = process_alive,
            chmod: Chmod = set_mode) -> list[ReadonlyMarker]:
    """恢复持有进程已不存在的锁定，返回已恢复的标记；持有进程仍在运行的不处理。"""
    recovered = []
    for marker_path in sorted(guards_dir.glob(MARKER_PATTERN)):
        marker = read_marker(marker_path)
        if alive(marker.pid):
            continue
        recovered.append(restore(marker_path, chmod=chmod))
    return recovered
