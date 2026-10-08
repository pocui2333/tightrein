"""worktree 的新建、复用、只读锁定与清理(protocol/git.md)。不动用户的主工作区。

- 修复 worktree：先 fetch(只读)，从 origin/<主分支> 用 `worktree add -b` 新建；已存在且分支对应时直接复用。项目要求的
  被忽略目录(如 `.claude/`、`node_modules`)从主工作区软链接进去，否则测试与 agent 跑不起来。
- 只读 worktree：游离 HEAD，不建分支；切换时目标 commit 本地已有就不 fetch；不干净说明有东西写入，停下不清理(保留
  现场)。agent 运行期间再去掉写权限(chmod)：先写标记文件(原权限、进程号、主机)再改权限，中途崩溃也能按标记恢复；
  这是工具只读权限与快照比对之外的又一层。
- 清理：先确认 worktree 干净，再 `worktree remove`(不加 --force)、`branch -d`(只删已合并的，从不用 -D)、
  `fetch --prune`；目录与本地分支都已不在的算完成，重复执行不出错。
"""

from __future__ import annotations

import json
import os
import socket
import stat
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from tightrein.protocol.git.git import Git, NetworkError, WorktreeDirty, WriteScope
from tightrein.protocol.naming import Clock, format_iso
from tightrein.store.files.atomic import write_text

WRITE_BITS = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
ROOT = "."
MARKER_PATTERN = "readonly-*.json"

Chmod = Callable[[Path, int], None]


class ReadonlyLocked(Exception):
    """只读 worktree 上留有锁定标记：先恢复写权限(recover_readonly)再切换。"""


class ReadonlyRestoreFailed(Exception):
    """恢复写权限失败；标记文件保留，下次启动时重试。"""


@dataclass(frozen=True)
class SyncResult:
    worktree: Path
    previous: str | None
    commit: str

    @property
    def moved(self) -> bool:
        return self.previous != self.commit


@dataclass(frozen=True)
class ReadonlyMarker:
    worktree: Path
    pid: int
    host: str
    started_at: str
    modes: dict[str, int]  # 相对路径 → 原有权限；根目录为 `.`


def create_fix(git: Git, path: Path, branch: str, *, links: Sequence[str] = (), scope: WriteScope) -> Path:
    """从 fetch 后的 origin/<主分支> 新建修复分支与 worktree；links 为从主工作区链接进来的被忽略目录或文件。"""
    git.fetch()
    base = git.rev_parse(f"origin/{git.main_branch}")
    git.worktree_add(path, branch=branch, base=base, scope=scope)
    for link in links:
        name = link.strip("/")
        target = path / name
        if not target.exists() and not target.is_symlink() and (git.repo / name).exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(git.repo / name)
    return path


def create_readonly(git: Git, path: Path, *, scope: WriteScope) -> Path:
    """游离 HEAD 的只读 worktree；本地已有 origin/<主分支> 就直接用，不 fetch(之后每次运行再切到目标 commit)。"""
    if not git.has_commit(f"origin/{git.main_branch}"):
        git.fetch()
    git.worktree_add(path, branch=None, base=git.rev_parse(f"origin/{git.main_branch}"), scope=scope)
    return path


def sync_readonly(git: Git, path: Path, *, marker: Path, commit: str | None = None) -> SyncResult:
    """把只读 worktree 的游离 HEAD 切到 commit；commit 为空时切到 fetch 之后的 origin/<主分支>。"""
    if marker.exists():
        raise ReadonlyLocked(f"{path} 上有锁定标记 {marker}，先恢复写权限")
    worktree = git.at(path)
    wanted = commit or f"origin/{git.main_branch}"
    if commit is None or not worktree.has_commit(commit):
        try:
            worktree.fetch()
        except NetworkError as error:
            raise NetworkError(f"本地没有 {commit or wanted + ' 的最新提交'}，需要 fetch，但 fetch 失败：{error}",
                               argv=error.argv, exit_code=error.exit_code, stderr=error.stderr) from error
    status = worktree.status()
    if not status.clean:
        raise WorktreeDirty(f"只读 worktree {path} 中有未提交的改动，已停止，不做清理", paths=status.changed_paths)
    target = worktree.rev_parse(wanted)
    previous = worktree.head().commit
    if previous != target:
        worktree.checkout_detached(target)
    return SyncResult(path, previous, target)


def cleanup(git: Git, path: Path, branch: str, *, scope: WriteScope) -> None:
    """删除修复 worktree 与本地分支；有未提交改动时抛出 WorktreeDirty，什么都不删。"""
    git.worktree_remove(path, scope=scope)
    git.branch_delete(branch, scope=scope)
    git.fetch(prune=True)


def lock_readonly(path: Path, marker: Path, clock: Clock, *, chmod: Chmod | None = None) -> ReadonlyMarker:
    """去掉只读 worktree 全部目录与文件的写权限(不跟随符号链接，跳过 .git 目录)；先写标记再改权限，失败时还原。
    标记已在说明另一个 agent 正在用(或上次没恢复)：同一只读 worktree 同时只允许一个 agent。"""
    if marker.exists():
        raise ReadonlyLocked(f"{path} 上已有锁定标记 {marker}")
    change = chmod or _set_mode
    found = ReadonlyMarker(path, os.getpid(), socket.gethostname(), format_iso(clock.now()), _modes(path))
    write_text(marker, json.dumps(_marker_data(found), ensure_ascii=False, indent=2) + "\n")
    try:
        for relative, mode in found.modes.items():
            change(path / relative, mode & ~WRITE_BITS)
    except OSError:
        restore_readonly(marker, chmod=change)
        raise
    return found


def restore_readonly(marker: Path, *, chmod: Chmod | None = None) -> ReadonlyMarker:
    """按标记还原原有权限(原本就不可写的保持不可写)，完成后删除标记；失败时保留标记。"""
    change = chmod or _set_mode
    found = _read_marker(marker)
    try:
        for relative, mode in found.modes.items():
            try:
                change(found.worktree / relative, mode)
            except FileNotFoundError:
                continue
    except OSError as error:
        raise ReadonlyRestoreFailed(f"{found.worktree} 的写权限恢复失败({type(error).__name__}: {error})；标记 "
                                    f"{marker} 已保留，下次启动时重试，也可以 chmod -R u+w 后删除标记") from error
    marker.unlink()
    return found


def recover_readonly(directory: Path, *, alive: Callable[[int], bool] | None = None,
                     chmod: Chmod | None = None) -> list[ReadonlyMarker]:
    """启动时恢复本机上持有进程已不在的锁定；持有进程仍在的不动(同一只读 worktree 同时只允许一个 agent)。"""
    running = alive or _process_alive
    host = socket.gethostname()
    recovered = []
    for marker in sorted(directory.glob(MARKER_PATTERN)):
        found = _read_marker(marker)
        if found.host == host and not running(found.pid):
            recovered.append(restore_readonly(marker, chmod=chmod))
    return recovered


def _modes(worktree: Path) -> dict[str, int]:
    modes = {ROOT: stat.S_IMODE(os.lstat(worktree).st_mode)}
    for directory, directories, files in os.walk(worktree, followlinks=False):
        base = Path(directory)
        if base == worktree and ".git" in directories:
            directories.remove(".git")
        for name in [*directories, *files]:
            info = os.lstat(base / name)
            if not stat.S_ISLNK(info.st_mode):
                modes[(base / name).relative_to(worktree).as_posix()] = stat.S_IMODE(info.st_mode)
    return modes


def _set_mode(path: Path, mode: int) -> None:
    os.chmod(path, mode, follow_symlinks=False)


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _marker_data(marker: ReadonlyMarker) -> dict[str, object]:
    return {"worktree": str(marker.worktree), "pid": marker.pid, "host": marker.host,
            "startedAt": marker.started_at, "modes": marker.modes}


def _read_marker(path: Path) -> ReadonlyMarker:
    data = json.loads(path.read_text(encoding="utf-8"))
    return ReadonlyMarker(Path(data["worktree"]), int(data["pid"]), data["host"], data["startedAt"],
                          {key: int(value) for key, value in data["modes"].items()})
