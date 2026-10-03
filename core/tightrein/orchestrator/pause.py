"""暂停与恢复(redesign/09-loop.md 第 6 节)：全局暂停是本机状态目录中的标记文件(UserLayout.pause_flag)，单个工作区的暂停
记在 workspace_meta.paused。暂停后 run 与 tick 不发起新的运行，进行中的运行在当前步骤完成后停下、无人值守推进不再开始
下一个对象；用户当场发起的 continue 与各模块命令不受影响。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from tightrein.domain.clock import Clock, format_iso
from tightrein.store.files import atomic
from tightrein.store.repos import workspace_meta

class Paused(Exception):
    """暂停中，不发起新的运行；消息为暂停的原因。"""


GLOBAL = "已全局暂停(tightrein resume 恢复)"
WORKSPACE = "本工作区已暂停(tightrein resume --workspace <工作区> 恢复)"


def pause_global(flag: Path, clock: Clock, note: str | None = None) -> None:
    atomic.write_text(flag, f"{format_iso(clock.now())} {note or ''}".rstrip() + "\n")


def resume_global(flag: Path) -> bool:
    """返回之前是否处于暂停。"""
    existed = flag.is_file()
    flag.unlink(missing_ok=True)
    return existed


def pause_workspace(conn: sqlite3.Connection, clock: Clock, note: str | None = None) -> None:
    workspace_meta.set_value(conn, workspace_meta.PAUSED, note or format_iso(clock.now()), clock.now())


def resume_workspace(conn: sqlite3.Connection) -> bool:
    existed = workspace_meta.get(conn, workspace_meta.PAUSED) is not None
    workspace_meta.remove(conn, workspace_meta.PAUSED)
    return existed


def reason(conn: sqlite3.Connection, flag: Path | None) -> str | None:
    """暂停的原因；没有暂停时为空。"""
    if flag is not None and flag.is_file():
        return GLOBAL
    if workspace_meta.get(conn, workspace_meta.PAUSED) is not None:
        return WORKSPACE
    return None
