"""出问题时写经验(redesign/08-learn.md 第 1 节，architecture/08 6.1)。

1. 来源取 troubles.collect：分诊误判、评审驳回、修复失败、被撤销、用户驳回计划、用户关闭 Issue；幂等键已完成的跳过；
2. 经执行器运行 lesson-writer，核心预取同类型的已有条目摘要；分诊误判附发现报告，其余附修复报告；
3. 草稿经 KnowledgeService.write 写入(带写入去重)；正文末尾由程序追加「来源」一节(类型、对象、运行)，
   reviewBy 为写入日加 thresholds.learn.lessonReviewDays 天，过期且长期未命中的由 curate 自动归档；
4. 成功或草稿为空时标记幂等键，结果中记下写入的条目；失败时不标记，下次重试；同一对象的 lesson-writer 调用累计达到
   thresholds.learn.lessonRetries 次仍未成功时标记为放弃，列入周报「需要处理的事项」，由用户手写。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from tightrein.domain.enums import KnowledgeStatus, KnowledgeType, RunnerStatus
from tightrein.pipeline.learn.prompts import lesson as lesson_prompt
from tightrein.pipeline.learn.prompts.common import LESSON_WRITER, STAGE, LearnEnv
from tightrein.pipeline.learn.steps import troubles
from tightrein.pipeline.learn.steps.attention import Attention
from tightrein.pipeline.learn.steps.troubles import Trouble
from tightrein.retrieval.errors import InvalidQuery, KnowledgeError
from tightrein.retrieval.models import SearchFilters
from tightrein.retrieval.writer import KnowledgeDraft
from tightrein.runner.task import RunnerTask
from tightrein.store import idempotency

KNOWLEDGE_ID = "knowledgeId"
SOURCE_TITLE = "## 来源"


def _done(conn: sqlite3.Connection, key: str) -> bool:
    record = idempotency.get(conn, key)
    return record is not None and record.status == idempotency.DONE


@dataclass(frozen=True)
class LessonResult:
    source: str
    knowledge_id: str | None
    decision: str | None

    def to_dict(self) -> dict[str, Any]:
        return {"source": self.source, "knowledgeId": self.knowledge_id, "decision": self.decision}


@dataclass
class LessonReport:
    results: list[LessonResult] = field(default_factory=list)
    attention: list[Attention] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)


def existing(env: LearnEnv, kind: KnowledgeType, query: str, context: str) -> str:
    """同类型已有条目的摘要，数量取 thresholds.retrieval.contextLimits.<context>。"""
    limit = env.config.whole_threshold(f"retrieval.contextLimits.{context}")
    try:
        hits = env.knowledge.search(query, SearchFilters((kind.value,), (), KnowledgeStatus.ACTIVE, limit)).hits
    except InvalidQuery:
        return ""
    return "\n".join(f"- {hit.id} {hit.summary}" for hit in hits)


def _read(path: Any) -> str:
    return path.read_text(encoding="utf-8") if path is not None and path.is_file() else ""


def _attempts(conn: sqlite3.Connection, subject_id: str) -> int:
    row = conn.execute("SELECT COUNT(*) FROM stage_yield WHERE stage = ? AND role = ? AND subject_id = ?",
                       (STAGE.value, LESSON_WRITER, subject_id)).fetchone()
    return int(row[0])


def _mark(env: LearnEnv, keys: Sequence[str], result: dict[str, Any]) -> None:
    for key in keys:
        idempotency.run_once(env.conn, key, lambda: result, env.clock)


def _draft(env: LearnEnv, data: dict[str, Any], trouble: Trouble) -> KnowledgeDraft:
    review_by = env.today() + timedelta(days=env.config.whole_threshold("learn.lessonReviewDays"))
    source = [f"- 类型：{trouble.label}", f"- 对象：{trouble.subject_type} {trouble.subject_id}",
              f"- 运行：{trouble.run_id or env.run_id}"]
    body = f"{data['body'].rstrip()}\n\n{SOURCE_TITLE}\n\n" + "\n".join(source) + "\n"
    return KnowledgeDraft(KnowledgeType(data["type"]), data["slug"], data["title"], data["summary"],
                          tuple(data["tags"]), body, review_by, tuple(data["related"]), trouble.run_id or env.run_id)


def _handle(env: LearnEnv, report: LessonReport, task: RunnerTask, trouble: Trouble) -> None:
    keys = trouble.keys
    result = env.calls.run(task)
    failure = None
    if result.status is not RunnerStatus.OK or result.output is None:
        failure = f"执行器没有给出草稿：{result.status.value} {result.error_type or ''}".strip()
    elif result.output["draft"] is None:
        _mark(env, keys, {KNOWLEDGE_ID: None, "decision": None})
        report.results += [LessonResult(key, None, None) for key in keys]
        return
    else:
        try:
            written = env.knowledge.write(_draft(env, result.output["draft"], trouble), env.calls.runner,
                                          env.origin(task.subject))
        except KnowledgeError as error:
            failure = f"写入失败：{error}"
        else:
            _mark(env, keys, {KNOWLEDGE_ID: written.written_id, "decision": written.decision.value})
            report.results += [LessonResult(key, written.written_id, written.decision.value) for key in keys]
            return
    report.errors.append({"item": keys[0], "reason": failure})
    attempts = _attempts(env.conn, task.subject_id)
    if attempts >= env.config.whole_threshold("learn.lessonRetries"):
        _mark(env, keys, {KNOWLEDGE_ID: None, "decision": None, "gaveUp": failure})
        directory = env.layout.relative(env.layout.knowledge_type_dir(trouble.knowledge_type))
        report.attention.append(Attention("lesson-failed", keys[0], f"经验撰写已失败 {attempts} 次，需要手写",
                                          f"在 {directory}/ 下手写一条经验"))


def _material(env: LearnEnv, trouble: Trouble) -> str:
    if trouble.subject_type == "problem":
        return _read(env.layout.finding(trouble.subject_id))
    return _read(env.layout.fix_report(trouble.subject_id))


def write_lessons(env: LearnEnv) -> LessonReport:
    report = LessonReport()
    if not env.writable:
        return report
    for trouble in troubles.collect(env.conn, env.layout):
        if all(_done(env.conn, key) for key in trouble.keys):
            continue
        context = "triage" if trouble.knowledge_type is KnowledgeType.TRIAGE_LESSON else "fix"
        task = lesson_prompt.lesson_task(env.calls.prompt, trouble, _material(env, trouble),
                                         existing(env, trouble.knowledge_type, trouble.title or trouble.subject_id,
                                                  context))
        _handle(env, report, task, trouble)
    return report
