"""学习建议(architecture/08 6.2、6.3)：生成、存储、接受、拒绝与过期。建议只在用户接受后生效，learn 不改任何配置与提示。

- 生成：采集配置(同一检查误报或作废集中)、覆盖缺口(接口描述中连续多周没有被覆盖的)；复核类建议由 curate 生成，
  控制措施由 controls 生成，改进建议由 improve 生成；
- 存储：同一类型、同一对象已有待处理的不再生成；被拒绝过的，只有出现拒绝时没有的证据对象才再生成；
- 接受：knowledge-review 按 renew、merge、archive 处理知识条目；其余类型只记录用户的决定，配置与提示由用户按建议修改；
- 拒绝必须写原因；待处理超过 thresholds.learn.suggestionExpiryWeeks 周的标为过期。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from tightrein.domain.clock import Clock
from tightrein.domain.enums import (
    Disposition,
    DocumentStatus,
    KnowledgeStatus,
    Probe,
    ProblemEvent,
    ProblemStatus,
    RunStage,
    RunStatus,
    SignalAggregateState,
    SuggestionKind,
    SuggestionStatus,
)
from tightrein.pipeline.learn.prompts.common import LearnEnv
from tightrein.pipeline.learn.render.decision import SuggestionText
from tightrein.pipeline.learn.steps.metrics import MetricContext, spec_operations
from tightrein.store import sequences
from tightrein.store.files import documents
from tightrein.store.repos import knowledge, problems, runs, signals, suggestions, triage
from tightrein.store.repos.suggestions import SuggestionRecord

RENEW, MERGE, ARCHIVE = "renew", "merge", "archive"
REVIEW_ACTIONS = (RENEW, MERGE, ARCHIVE)
EXAMPLES = 5


class SuggestionError(Exception):
    """建议不存在、不是待处理状态，或接受与拒绝的参数不合要求。"""


@dataclass(frozen=True)
class Draft:
    """document 给出时(控制措施与改进建议)另写一份决定文档，patch 为改进建议的补丁。"""

    kind: SuggestionKind
    subject: str
    evidence: dict[str, Any]
    items: tuple[str, ...]
    document: SuggestionText | None = None
    patch: str | None = None


DocumentWriter = Callable[[str, Draft], Path]


# 生成

def noisy_checks(ctx: MetricContext) -> list[Draft]:
    """同一探针同一检查本周被判误报、被抑制、被作废或所属问题不稳定的信号，占该检查信号的比例过高。"""
    rows = ctx.conn.execute(
        "SELECT s.id, s.probe, s.\"check\", s.location, s.suppressed, s.aggregate_state, ps.problem_id FROM signals s "
        "LEFT JOIN problem_signals ps ON ps.signal_id = s.id WHERE s.occurred_at >= ? AND s.occurred_at < ?",
        ctx.between()).fetchall()
    groups: dict[tuple[str, str], list[tuple[sqlite3.Row, bool]]] = {}
    for row in rows:
        groups.setdefault((row["probe"], row["check"]), []).append((row, _noisy(ctx.conn, row)))
    ratio, minimum = ctx.config.threshold("learn.noisyCheckRatio"), ctx.config.whole_threshold("learn.minSamples")
    found = []
    for (probe, check), items in sorted(groups.items()):
        noisy = [row for row, flag in items if flag]
        if len(items) < minimum or len(noisy) / len(items) <= ratio:
            continue
        examples = sorted({row["location"] for row in noisy})[:EXAMPLES]
        found.append(Draft(SuggestionKind.PROBE_CONFIG, f"{probe}:{check}", {
            "probe": probe, "check": check, "noisy": len(noisy), "total": len(items),
            "ratio": round(len(noisy) / len(items), 3), "examples": examples,
            "action": f"检查 {probe} 的 {check} 是否需要在 project.yaml 的 sources 段增加排除范围或调整阈值"},
            tuple(row["id"] for row in noisy)))
    return found


def _noisy(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    if row["suppressed"] or row["aggregate_state"] == SignalAggregateState.VOIDED.value:
        return True
    if row["problem_id"] is None:
        return False
    problem = problems.get(conn, row["problem_id"])
    latest = triage.latest(conn, row["problem_id"])
    user_false = conn.execute("SELECT 1 FROM problem_events WHERE problem_id = ? AND event = ?",
                              (row["problem_id"], ProblemEvent.USER_FALSE_POSITIVE.value)).fetchone()
    intermittent = problem is not None and problem.status is ProblemStatus.PENDING and problem.intermittent
    return (intermittent or user_false is not None
            or (latest is not None and latest.result.disposition is Disposition.FALSE_POSITIVE))


def coverage_gaps(ctx: MetricContext) -> list[Draft]:
    weeks = ctx.config.whole_threshold("learn.coverageGapWeeks")
    since = ctx.now - timedelta(weeks=weeks)
    found = []
    api = [run for run in runs.find(ctx.conn, stage=RunStage.COLLECT, probe=Probe.API_FUZZ, status=RunStatus.OK)
           if run.started_at >= since]
    operations = spec_operations(ctx.layout, ctx.config, api[-1].target_commit) if api else None
    if operations is not None:
        tested = frozenset().union(*(run.coverage.tested_endpoints() for run in api))
        missing = [f"{method} {route}" for method, route in operations if (method, route) not in tested]
        if missing:
            found.append(_gap(Probe.API_FUZZ, missing, weeks, "在 project.yaml 的 sources 段调整模糊测试范围"))
    return found


def _gap(probe: Probe, missing: Sequence[str], weeks: int, action: str) -> Draft:
    return Draft(SuggestionKind.COVERAGE_GAP, probe.value,
                 {"probe": probe.value, "weeks": weeks, "missing": list(missing), "action": action}, tuple(missing))


# 存储与处理

def store(conn: sqlite3.Connection, clock: Clock, drafts: Iterable[Draft],
          document: DocumentWriter | None = None) -> list[SuggestionRecord]:
    """document 为写决定文档的函数(参数为建议编号与草稿，返回相对工作区的路径)。"""
    created = []
    for draft in drafts:
        same = [item for item in suggestions.find(conn, kind=draft.kind) if item.subject == draft.subject]
        if any(item.status is SuggestionStatus.PENDING for item in same):
            continue
        rejected = {entry for item in same if item.status is SuggestionStatus.REJECTED
                    for entry in item.evidence.get("items", [])}
        if rejected and set(draft.items) <= rejected:
            continue
        suggestion_id = sequences.next_suggestion_id(conn)
        path = document(suggestion_id, draft) if document is not None and draft.document is not None else None
        record = SuggestionRecord(suggestion_id, draft.kind, draft.subject, SuggestionStatus.PENDING, clock.now(),
                                  {**draft.evidence, "items": list(draft.items)},
                                  target_path=None if path is None else str(path), diff=draft.patch)
        suggestions.save(conn, record)
        created.append(record)
    return created


def expire(conn: sqlite3.Connection, now: datetime, weeks: int) -> list[str]:
    expired = []
    for record in suggestions.find(conn, status=SuggestionStatus.PENDING):
        if record.created_at < now - timedelta(weeks=weeks):
            suggestions.save(conn, replace(record, status=SuggestionStatus.EXPIRED, decided_at=now))
            expired.append(record.id)
    return expired


def _pending(conn: sqlite3.Connection, suggestion_id: str) -> SuggestionRecord:
    record = suggestions.get(conn, suggestion_id)
    if record is None:
        raise SuggestionError(f"建议 {suggestion_id} 不存在")
    if record.status is not SuggestionStatus.PENDING:
        raise SuggestionError(f"建议 {suggestion_id} 已是「{record.status.label}」，只能处理待处理的建议")
    return record


def _review(env: LearnEnv, record: SuggestionRecord, action: str | None) -> None:
    if action not in REVIEW_ACTIONS:
        raise SuggestionError(f"复核建议须用 --action 指定 {'、'.join(REVIEW_ACTIONS)} 之一")
    ids = list(record.evidence["ids"])
    records = [knowledge.get(env.conn, entry_id) for entry_id in ids]
    missing = [entry_id for entry_id, item in zip(ids, records, strict=True) if item is None]
    if missing:
        # 先查再改：建议生成后条目可能已被删除(kb sync 删掉记录)，不能改到一半才报错
        raise SuggestionError(f"条目 {'、'.join(missing)} 已不存在，这条建议无法执行，请拒绝它")
    if action == RENEW:
        review_by = env.today() + timedelta(days=env.config.whole_threshold("learn.lessonReviewDays"))
        for entry_id in ids:
            env.knowledge.set_status(entry_id, KnowledgeStatus.ACTIVE, review_by=review_by)
    elif action == ARCHIVE:
        for entry_id in ids:
            env.knowledge.set_status(entry_id, KnowledgeStatus.ARCHIVED)
    else:
        if len(ids) < 2:
            raise SuggestionError("合并至少需要两个条目，这条建议只涉及一个条目，请用 renew 或 archive")
        primary = max((item for item in records if item is not None), key=lambda item: item.hits).id
        for entry_id in ids:
            if entry_id != primary:
                env.knowledge.set_status(entry_id, KnowledgeStatus.SUPERSEDED, superseded_by=primary)


def _close_document(root: Path, record: SuggestionRecord, at: datetime, event: str) -> None:
    """决定文档(控制措施与改进建议)的历史追加用户的决定，状态改为完成。"""
    if record.target_path is not None and (root / record.target_path).is_file():
        documents.append_history(root / record.target_path, at, event, DocumentStatus.DONE)


def accept(env: LearnEnv, suggestion_id: str, action: str | None = None) -> SuggestionRecord:
    if not env.writable:
        raise SuggestionError("--output 模式下不接受建议")
    record = _pending(env.conn, suggestion_id)
    if record.kind is SuggestionKind.KNOWLEDGE_REVIEW:
        _review(env, record, action)
    updated = replace(record, status=SuggestionStatus.ACCEPTED, decided_at=env.clock.now(), reason=action)
    suggestions.save(env.conn, updated)
    _close_document(env.layout.root, updated, env.clock.now(), "用户批准")
    return updated


def reject(conn: sqlite3.Connection, clock: Clock, suggestion_id: str, reason: str,
           root: Path | None = None) -> SuggestionRecord:
    """root 为工作区根目录，给出时在决定文档中记下拒绝。"""
    if not reason.strip():
        raise SuggestionError("拒绝建议必须写明原因")
    record = _pending(conn, suggestion_id)
    updated = replace(record, status=SuggestionStatus.REJECTED, decided_at=clock.now(), reason=reason.strip())
    suggestions.save(conn, updated)
    if root is not None:
        _close_document(root, updated, clock.now(), f"用户拒绝：{reason.strip()}")
    return updated
