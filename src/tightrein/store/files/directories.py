"""按对象目录逐个处理时的列举(保留期清理、从文件重建共用)。路径本身仍只由 layout 计算。"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from tightrein.protocol.naming import run_started
from tightrein.store.files.layout import WorkspaceLayout


def subdirectories(parent: Path) -> list[Path]:
    """parent 下的子目录，按名字排序；parent 不存在时为空。"""
    return sorted(path for path in parent.iterdir() if path.is_dir()) if parent.is_dir() else []


def run_dirs(layout: WorkspaceLayout) -> Iterator[tuple[Path, datetime]]:
    """名字是合格运行编号的运行目录与它的开始时间；其余(手放的文件、不认识的目录)跳过不动。"""
    for directory in subdirectories(layout.runs_dir):
        try:
            started = run_started(directory.name)
        except ValueError:
            continue
        yield directory, started
