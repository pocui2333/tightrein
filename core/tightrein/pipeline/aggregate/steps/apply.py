"""把 ChangeSet 写入数据库(architecture/05 3.4、3.9、3.10)：一个事务内写运行、信号、问题、对应、合并、关联与事件。

side_effects 为真(正常模式)时在同一事务中执行文件副作用：关联问题回归的 Issue 按 domain.issue.event_for_regression
重新打开或判为部署后验证失败，Issue 文件「历史」一节追加一行并同步 issues 表与 issue_events；误报生成的抑制规则
追加到 suppressions.yaml。文件写入在事务提交前完成，事务失败时 Issue 文件恢复为原内容。
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

from tightrein.domain.clock import Clock, format_iso
from tightrein.domain.enums import IssueEffect, IssueEvent
from tightrein.domain.ids import parse_sequence
from tightrein.domain.issue import event_for_regression
from tightrein.domain.issue import transition as issue_transition
from tightrein.pipeline.aggregate.changeset import ChangeSet, IssueReopen
from tightrein.pipeline.issue.render import issue as template
from tightrein.store import sequences
from tightrein.store.db import transaction
from tightrein.store.files import issue_files, suppressions
from tightrein.store.files.issue_files import IssueDocument, IssueFileConflict
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.repos import issue_events, issues, problem_events, problems, runs, signals
from tightrein.store.repos.issue_events import IssueEventRecord

ACTOR = "aggregate"
def append_history(body: str, line: str) -> str:
    """在「历史」一节(任一语言的标题)末尾追加一行；没有这一节时在文末新建。"""
    return template.append_history_line(body, line)


def _reopen(conn: sqlite3.Connection, layout: WorkspaceLayout, item: IssueReopen, clock: Clock,
            originals: dict[Path, str]) -> IssueEvent | None:
    record = issues.get(conn, item.issue_id)
    if record is None:
        raise IssueFileConflict(f"问题 {item.problem_id} 关联的 Issue {item.issue_id} 不在索引中，先执行 reindex")
    event = event_for_regression(record.issue)
    if event is None:
        return None
    status, effects = issue_transition(record.issue.status, event)
    now = clock.now()
    reopened = any(effect.kind is IssueEffect.REOPEN for effect in effects)
    issue = replace(record.issue, status=status, updated_at=now,
                    close_reason=None if reopened else record.issue.close_reason)
    path = layout.root / record.path
    current = issue_files.read(path)
    originals.setdefault(path, path.read_text(encoding="utf-8"))
    signal_text = "、".join(item.signal_ids) or "无"
    line = (f"- {format_iso(now)} 关联问题 {item.problem_id} 回归(commit {item.release or '未知'}，信号 {signal_text})，"
            f"{event.label}")
    issue_files.write(conn, layout, IssueDocument(issue, append_history(current.body, line), current.run_id))
    issue_events.append(conn, IssueEventRecord(item.issue_id, now, event.value, ACTOR, record.issue.status, status,
                                               note=f"关联问题 {item.problem_id} 回归"))
    return event


def commit(conn: sqlite3.Connection, changeset: ChangeSet, clock: Clock, layout: WorkspaceLayout, *,
           side_effects: bool) -> list[str]:
    """写入 ChangeSet，返回因回归重新打开的 Issue 编号。"""
    originals: dict[Path, str] = {}
    reopened: list[str] = []
    try:
        with transaction(conn):
            sequences.ensure_at_least(conn, sequences.PROBLEM, changeset.next_number - 1)
            for run in changeset.runs.values():
                runs.save(conn, run)
            for signal in changeset.signals.values():
                signals.save(conn, signal)
            for problem in sorted(changeset.problems.values(), key=lambda item: parse_sequence(item.id)):
                problems.save(conn, problem)
            for problem_id, signal_id in changeset.problem_signals:
                problems.add_signals(conn, problem_id, [signal_id])
            for target, source in changeset.merges:
                problems.merge(conn, target, source, clock)
            for event in changeset.events:
                problem_events.append(conn, event)
            if side_effects:
                for item in changeset.reopened:
                    if _reopen(conn, layout, item, clock, originals) is IssueEvent.PROBLEM_REGRESSED:
                        reopened.append(item.issue_id)
                for rule in changeset.suppressions:
                    suppressions.add_rule(layout, rule, clock)
    except BaseException:
        for path, text in originals.items():
            path.write_text(text, encoding="utf-8")
        raise
    return reopened
