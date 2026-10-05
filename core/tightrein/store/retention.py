"""按保留期清理(design 2.14、10.4、10.5)。数据库中的删除只由本模块执行，按保留期批量处理。

- 信号：发生时间早于保留期的删除，问题记录与出现次数保留；问题与信号的对应随信号删除。
- 问题：已解决超过同一保留期且没有关联 Issue 的问题，连同别名、关联、事件、信号对应、分诊结论与并入它的问题一并删除。
  解决时间取最近一条转为 resolved 的事件，没有事件时取最近出现时间。
- 运行目录中的原始输出 raw/ 与会话记录 transcripts/：运行开始时间(取自运行编号)早于保留期的删除，交接文档与信号原件保留。
- 事件日志 events-<日期>.jsonl：日期早于保留期的删除。
- 修复目录 data/fixes/<Issue 编号>/：Issue 关闭后保留 thresholds.retention.fixesDays 天；按关闭时间删除还没有实现。
- launchd 日志：单个文件达到 runtime.store.launchdLogMaxBytes 时轮转为 `<文件名>.1`，最多保留
  runtime.store.launchdLogBackups 份。
各项的缺省值取 config/defaults.yaml 的核心缺省值；组装根按工作区配置传入。
"""

from __future__ import annotations

import re
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from tightrein.config import layers
from tightrein.domain.clock import Clock, format_iso
from tightrein.domain.enums import ProblemStatus
from tightrein.domain.ids import parse_sequence
from tightrein.store.db import transaction
from tightrein.store.files.layout import WorkspaceLayout, rotated_log


_RUN_TIME = re.compile(r"^R-(\d{8})-(\d{6})-")
_EVENTS_LOG = re.compile(r"^events-(\d{4}-\d{2}-\d{2})\.jsonl$")


def _days(key: str) -> int:
    return int(layers.core_value(f"thresholds.retention.{key}")["value"])


@dataclass(frozen=True)
class RetentionPolicy:
    """保留天数(thresholds.retention.*)；缺省为核心缺省值。"""

    signals_days: int = field(default_factory=lambda: _days("signalsDays"))
    raw_days: int = field(default_factory=lambda: _days("rawDays"))
    logs_days: int = field(default_factory=lambda: _days("logsDays"))
    fixes_days: int = field(default_factory=lambda: _days("fixesDays"))


@dataclass(frozen=True)
class RetentionReport:
    signals: int
    problems: tuple[str, ...]
    run_files: tuple[Path, ...]
    logs: tuple[Path, ...]


def _expired_problems(conn: sqlite3.Connection, cutoff: str) -> tuple[list[str], list[str]]:
    """到期的问题(根)与连同删除的全部问题(根加上并入它们的问题)。"""
    condition = (
        "status = ? AND issue_id IS NULL AND COALESCE("
        "(SELECT MAX(at) FROM problem_events WHERE problem_id = problems.id AND to_status = ?), last_seen_at) < ?"
    )
    params = (ProblemStatus.RESOLVED.value, ProblemStatus.RESOLVED.value, cutoff)
    roots = [row[0] for row in conn.execute(f"SELECT id FROM problems WHERE {condition}", params)]
    doomed = [
        row[0] for row in conn.execute(
            f"WITH RECURSIVE doomed(id) AS (SELECT id FROM problems WHERE {condition} "
            "UNION SELECT problems.id FROM problems JOIN doomed ON problems.merged_into = doomed.id) "
            "SELECT id FROM doomed",
            params,
        )
    ]
    return roots, sorted(doomed, key=parse_sequence)


def _purge_database(conn: sqlite3.Connection, now: datetime, policy: RetentionPolicy) -> tuple[int, tuple[str, ...]]:
    cutoff = format_iso(now - timedelta(days=policy.signals_days))
    with transaction(conn):
        signals = conn.execute("DELETE FROM signals WHERE occurred_at < ?", (cutoff,)).rowcount
        roots, doomed = _expired_problems(conn, cutoff)
        conn.executemany("DELETE FROM triage_results WHERE problem_id = ?", [(problem_id,) for problem_id in doomed])
        conn.executemany("DELETE FROM problems WHERE id = ?", [(problem_id,) for problem_id in roots])
    return signals, tuple(doomed)


def _run_started(name: str) -> datetime | None:
    match = _RUN_TIME.match(name)
    if match is None:
        return None
    return datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)


def _purge_run_files(layout: WorkspaceLayout, now: datetime, policy: RetentionPolicy) -> tuple[Path, ...]:
    runs_dir = layout.runs_dir()
    if not runs_dir.is_dir():
        return ()
    cutoff = now - timedelta(days=policy.raw_days)
    removed: list[Path] = []
    for run_dir in sorted(runs_dir.iterdir()):
        started = _run_started(run_dir.name)
        if not run_dir.is_dir() or started is None or started >= cutoff:
            continue
        for directory in (layout.raw_dir(run_dir.name), layout.transcripts_dir(run_dir.name)):
            if directory.is_dir():
                shutil.rmtree(directory)
                removed.append(directory)
    return tuple(removed)


def _purge_logs(layout: WorkspaceLayout, now: datetime, policy: RetentionPolicy) -> tuple[Path, ...]:
    logs_dir = layout.logs_dir()
    if not logs_dir.is_dir():
        return ()
    cutoff = now.date() - timedelta(days=policy.logs_days)
    removed: list[Path] = []
    for path in sorted(logs_dir.iterdir()):
        match = _EVENTS_LOG.match(path.name)
        if match is not None and date.fromisoformat(match.group(1)) < cutoff:
            path.unlink()
            removed.append(path)
    return tuple(removed)


def purge(
    conn: sqlite3.Connection, layout: WorkspaceLayout, clock: Clock, policy: RetentionPolicy | None = None
) -> RetentionReport:
    """执行一次清理，返回删除的内容。数据库部分在一个事务中完成。"""
    policy = policy or RetentionPolicy()
    now = clock.now()
    signals, problems = _purge_database(conn, now, policy)
    return RetentionReport(signals, problems, _purge_run_files(layout, now, policy), _purge_logs(layout, now, policy))


def rotate_log(path: Path, max_bytes: int | None = None, backups: int | None = None) -> bool:
    """文件达到 max_bytes 时轮转：删除 `<文件名>.<backups>`，其余备份序号依次加 1，当前文件改名为 `<文件名>.1`。

    用改名而不是截断：launchd 每次启动任务时按路径重新打开日志文件，正在运行的进程继续写入改名后的文件，不丢内容。
    文件不存在或未达到上限时不做任何事，返回 False。
    """
    max_bytes = int(layers.core_value("runtime.store.launchdLogMaxBytes")) if max_bytes is None else max_bytes
    backups = int(layers.core_value("runtime.store.launchdLogBackups")) if backups is None else backups
    if max_bytes < 1 or backups < 1:
        raise ValueError(f"大小上限与保留份数都必须大于 0：{max_bytes}、{backups}")
    if not path.is_file() or path.stat().st_size < max_bytes:
        return False
    rotated_log(path, backups).unlink(missing_ok=True)
    for number in range(backups - 1, 0, -1):
        source = rotated_log(path, number)
        if source.exists():
            source.replace(rotated_log(path, number + 1))
    path.replace(rotated_log(path, 1))
    return True


def rotate_launchd_logs(
    layout: WorkspaceLayout, max_bytes: int | None = None, backups: int | None = None
) -> tuple[Path, ...]:
    """轮转 launchd 的标准输出与标准错误日志，返回被轮转的文件。"""
    paths = (layout.launchd_out_log(), layout.launchd_err_log())
    return tuple(path for path in paths if rotate_log(path, max_bytes, backups))
