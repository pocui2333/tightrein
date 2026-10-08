"""从文件重建数据库(`tightrein admin rebuild`)。文件存内容、数据库只存索引与状态，所以除计数外都能重建。

每张能重建的表登记一个重建函数：先清空该表，再由函数按文件写回，返回写回的行数。全部表在一个事务中完成，
任一函数抛 RebuildError(一次列出全部不合格的文件)即整体回滚，数据库保持原样。
重建后把编号序列推到文件中已用的最大编号之后，避免再发出被文件占用的编号。

本模块登记 runs；problems(连同 occurrences)、issues 的重建函数由去重与评估模块用 register 登记。
counters(预算与熔断计数)、operations(幂等键)、state(读取位置)没有对应的文件，不重建、不清空。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

from tightrein.protocol.naming import parse_iso
from tightrein.store.db import transaction
from tightrein.store.files.directories import run_dirs, subdirectories
from tightrein.store.files.json import read_json
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.tables import runs, sequences
from tightrein.store.tables.runs import Run

Rebuilder = Callable[[WorkspaceLayout, sqlite3.Connection], int]

# 表 → (序列名, 编号前缀)：重建后按表中最大编号推进序列
_SEQUENCES = {"problems": (sequences.PROBLEM, "P-"), "issues": (sequences.ISSUE, "")}


class RebuildError(Exception):
    def __init__(self, table: str, errors: list[str]) -> None:
        super().__init__(f"{table} 无法重建，{len(errors)} 个文件不合格：" + "；".join(errors))
        self.table = table
        self.errors = errors


def register(table: str, rebuilder: Rebuilder) -> None:
    """登记一张表的重建函数；同一张表再次登记即替换。按登记顺序执行。"""
    REBUILDERS[table] = rebuilder


def rebuild(layout: WorkspaceLayout, conn: sqlite3.Connection) -> dict[str, int]:
    """重建全部已登记的表，返回各表写回的行数。"""
    counts: dict[str, int] = {}
    with transaction(conn):
        for table, rebuilder in REBUILDERS.items():
            conn.execute(f'DELETE FROM "{table}"')
            counts[table] = rebuilder(layout, conn)
        for table, (name, prefix) in _SEQUENCES.items():
            row = conn.execute(
                f'SELECT MAX(CAST(substr(id, {len(prefix) + 1}) AS INTEGER)) FROM "{table}"'
            ).fetchone()
            if row[0] is not None:
                sequences.ensure_at_least(conn, name, int(row[0]))
    return counts


def rebuild_runs(layout: WorkspaceLayout, conn: sqlite3.Connection) -> int:
    """每个运行目录一行。编号、阶段、开始时间取自目录名；状态与结束时间取自各对象目录中引用它的 handoff.json：
    有失败的步骤为 failed，否则有步骤为 done，一步都没有为 interrupted；结束时间取最晚一份交接的时间。
    触发方式没有写进文件，重建后为空。
    """
    steps: dict[str, list[tuple[str, str | None]]] = {}
    errors: list[str] = []
    for path in _handoffs(layout):
        try:
            data = read_json(path)
            steps.setdefault(data["run"], []).append((data["status"], data.get("createdAt")))
        except (OSError, ValueError, KeyError, TypeError) as error:
            errors.append(f"{path}：{error}")
    if errors:
        raise RebuildError("runs", errors)
    count = 0
    for directory, started in run_dirs(layout):
        found = steps.get(directory.name, [])
        times = [created for _, created in found if created]
        status = "failed" if any(item == "failed" for item, _ in found) else "done" if found else "interrupted"
        run = Run(
            id=directory.name, stage=directory.name.rsplit("-", 1)[1], trigger=None, status=status,
            started_at=started, ended_at=parse_iso(max(times)) if times else None,
        )
        runs.TABLE.insert(conn, run, started)
        count += 1
    return count


def _handoffs(layout: WorkspaceLayout) -> Iterator[Path]:
    for parent in (layout.runs_dir, layout.issues_dir, layout.problems_dir):
        for directory in subdirectories(parent):
            yield from sorted(directory.glob("*-handoff.json"))


def rebuild_problems(layout: WorkspaceLayout, conn: sqlite3.Connection) -> int:
    """problems 由采集重放去重的变更日志(collect/dedup/output.py)。problems 必须排在 issues 之前：评估的快照要叠加
    在重放结果之上。在函数内导入，store 不依赖采集。"""
    from tightrein.collect.dedup.output import rebuild_problems as replay_dedup

    return replay_dedup(layout, conn)


def rebuild_issues(layout: WorkspaceLayout, conn: sqlite3.Connection) -> int:
    """issues 由评估从各 Issue 目录的 00-issue-record.json 重建，随后叠加评估对问题的改动(assess/issue/files.py)。
    issues 必须排在 problems 之后。在函数内导入，store 不依赖评估。"""
    from tightrein.assess.issue.files import rebuild_issues as from_files

    return from_files(layout, conn)


REBUILDERS: dict[str, Rebuilder] = {"runs": rebuild_runs, "problems": rebuild_problems, "issues": rebuild_issues}
