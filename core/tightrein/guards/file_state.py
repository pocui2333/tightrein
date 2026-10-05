"""文件状态快照与改动计算(architecture/02 3.4、3.8 第 3 步)。

快照记录每个文件的大小、修改时间与内容的 sha256；符号链接记录链接目标文本的哈希，不跟随。运行后再取快照时，
大小与修改时间都没变的文件沿用运行前的哈希，不再读取内容。键为相对快照根目录的 `/` 分隔路径。
读不了内容的文件(没有读权限)以大小与修改时间代替哈希，照常参与比较，不中断快照；取状态与读取之间被删掉的不计入。
"""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

CHUNK_BYTES = 1 << 20


@dataclass(frozen=True)
class FileState:
    size: int
    mtime_ns: int
    sha256: str

    def to_dict(self) -> dict[str, int | str]:
        return {"size": self.size, "mtimeNs": self.mtime_ns, "sha256": self.sha256}


Snapshot = dict[str, FileState]


def _digest(path: Path, info: os.stat_result) -> str:
    digest = hashlib.sha256()
    if stat.S_ISLNK(info.st_mode):
        digest.update(os.readlink(path).encode("utf-8"))
        return digest.hexdigest()
    with open(path, "rb") as handle:
        while chunk := handle.read(CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _state(path: Path, previous: FileState | None) -> FileState | None:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    if not (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)):
        return None
    if previous is not None and (previous.size, previous.mtime_ns) == (info.st_size, info.st_mtime_ns):
        return previous
    try:
        digest = _digest(path, info)
    except FileNotFoundError:
        return None
    except OSError:
        digest = f"unreadable:{info.st_size}:{info.st_mtime_ns}"
    return FileState(info.st_size, info.st_mtime_ns, digest)


def snapshot_files(root: Path, paths: Iterable[str], previous: Mapping[str, FileState] | None = None) -> Snapshot:
    """root 下给定的文件(通常为 git 跟踪与未忽略的未跟踪文件)；不存在的与目录不计入。"""
    result: Snapshot = {}
    for relative in paths:
        state = _state(root / relative, (previous or {}).get(relative))
        if state is not None:
            result[relative] = state
    return result


def walk(root: Path, skip_dir: Callable[[str], bool] | None = None) -> list[str]:
    """root 下全部文件与符号链接的相对路径，不跟随符号链接；root 为文件时返回 `["."]`。skip_dir 对目录名返回真时
    不进入该目录(虚拟环境、依赖目录等)，不先遍历再过滤。"""
    if not root.exists() and not root.is_symlink():
        return []
    if not root.is_dir() or root.is_symlink():
        return ["."]
    found = []
    for directory, directories, names in os.walk(root, followlinks=False):
        if skip_dir is not None:
            directories[:] = [name for name in directories if not skip_dir(name)]
        for name in names:
            found.append((Path(directory) / name).relative_to(root).as_posix())
    return sorted(found)


def snapshot_tree(root: Path, previous: Mapping[str, FileState] | None = None) -> Snapshot:
    return snapshot_files(root, walk(root), previous)


@dataclass(frozen=True)
class Changes:
    added: tuple[str, ...] = ()
    modified: tuple[str, ...] = ()
    deleted: tuple[str, ...] = ()

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(sorted({*self.added, *self.modified, *self.deleted}))

    def __bool__(self) -> bool:
        return bool(self.added or self.modified or self.deleted)


def compare(before: Mapping[str, FileState], after: Mapping[str, FileState]) -> Changes:
    return Changes(
        tuple(sorted(set(after) - set(before))),
        tuple(sorted(path for path in set(before) & set(after) if before[path].sha256 != after[path].sha256)),
        tuple(sorted(set(before) - set(after))),
    )
