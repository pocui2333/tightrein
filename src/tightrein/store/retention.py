"""按保留期清理(records.md「保留期」)，每次运行开始时执行一次。

policy 的键与 settings 的 records.retention 对应，值为秒(调用方已按 parse_duration 换算)；没给的键不清理：
- runs：运行目录(含 events.jsonl)与 runs 表中的记录；
- raw：各对象目录中只在失败或调试时保存的 prompt 与 raw 文件(可能含代码与密钥，保留期比日志短)；
- operations：已完成的幂等键(进行中的要先对账，不清理)。

年龄一律取自运行编号中的时间，不用文件修改时间：复制、解压、改动都会改掉修改时间。
对象目录(Issue、问题)中的 prompt 与 raw 文件，按同一步的 handoff.json 中记下的运行编号判断。
Issue 与问题目录、复盘记录、知识库永久保留。
"""

from __future__ import annotations

import re
import shutil
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from tightrein.protocol.naming import Clock, format_iso, run_started
from tightrein.store.db import transaction
from tightrein.store.files.directories import run_dirs, subdirectories
from tightrein.store.files.json import read_json
from tightrein.store.files.layout import WorkspaceLayout

RUNS = "runs"
RAW = "raw"
OPERATIONS = "operations"

# <序号>-<阶段.模块[.小步骤][.r轮]>-<prompt|raw>[-<后缀>].<扩展名>；第一组是同一步 handoff 的文件名前缀
_RAW_FILE = re.compile(r"^(\d{2}-[a-z0-9_.]+)-(?:prompt|raw)(?:-[^.]*)?\.[A-Za-z0-9]+$")


def purge(layout: WorkspaceLayout, conn: sqlite3.Connection, clock: Clock, policy: dict[str, float]) -> dict[str, int]:
    """执行一次清理，返回各类删除的数量。先在一个事务中删数据库记录，成功后再删文件。"""
    now = clock.now()
    cutoffs = {kind: now - timedelta(seconds=seconds) for kind, seconds in policy.items()}
    removed: dict[str, int] = {}
    expired_runs: list[Path] = []
    with transaction(conn):
        if RUNS in cutoffs:
            running = {row[0] for row in conn.execute("SELECT id FROM runs WHERE status = 'running'")}
            expired_runs = [
                directory for directory, started in run_dirs(layout)
                if started < cutoffs[RUNS] and directory.name not in running
            ]
            conn.execute(
                "DELETE FROM runs WHERE started_at < ? AND status != 'running'", (format_iso(cutoffs[RUNS]),)
            )
        if OPERATIONS in cutoffs:
            removed[OPERATIONS] = conn.execute(
                "DELETE FROM operations WHERE status = 'done' AND updated_at < ?", (format_iso(cutoffs[OPERATIONS]),)
            ).rowcount
    if RUNS in cutoffs:
        for directory in expired_runs:
            shutil.rmtree(directory)
        removed[RUNS] = len(expired_runs)
    if RAW in cutoffs:
        removed[RAW] = _purge_raw_files(layout, cutoffs[RAW])
    return removed


def _purge_raw_files(layout: WorkspaceLayout, cutoff: datetime) -> int:
    count = 0
    for directory, started in run_dirs(layout):
        if started >= cutoff:
            continue
        for path in directory.iterdir():
            if path.is_file() and _RAW_FILE.match(path.name):
                path.unlink()
                count += 1
    for parent in (layout.issues_dir, layout.problems_dir):
        for directory in subdirectories(parent):
            for path in directory.iterdir():
                step_started = _step_run_started(path)
                if step_started is not None and step_started < cutoff:
                    path.unlink()
                    count += 1
    return count


def _step_run_started(path: Path) -> datetime | None:
    """对象目录中 prompt、raw 文件所属运行的开始时间；不是这类文件或找不到可信的运行编号时为 None(不删)。"""
    match = _RAW_FILE.match(path.name)
    if match is None or not path.is_file():
        return None
    handoff = path.with_name(f"{match.group(1)}-handoff.json")
    try:
        run = read_json(handoff)["run"]
        return run_started(run)
    except (OSError, ValueError, KeyError, TypeError):
        return None
