"""写入去重(architecture/03 1.6.7，design 16.7)：检索相似条目，经执行器由 knowledge-curator 判断，校验判断结果。

- 相似条目：以草稿的标题、摘要与标签拼成查询串，过滤同类型、active，取前 5 条；没有相似条目时直接新增，不调用执行器；
- 判断任务：说明取自 skills/learn/references/knowledge-curator.md，任务说明附草稿全文与候选条目全文；输出按
  runner/roles/knowledge-curator.schema.json；只读；工作目录为 knowledge/ 目录(只读锁定工作区根目录会连带锁住 data/，
  执行器写不了会话记录)；
- 语义校验：add 的 supersedes、update 与 merge 的 targetIds 都必须在候选中，update 恰好 1 个，noop 至少 1 个；
  不通过时把逐条原因附在任务说明末尾交回执行器重试一次(attempt 为 2)，仍不通过抛出 WriteDecisionInvalid；
  执行器没有返回结构化结果(无法启动、超出上限、schema 重试后仍不合格)时同样抛出；
- apply 按判断改写文件(见 writer)，noop 不写文件；
- 给出数据库连接时每次判断调用之后登记环节效益(design 14.2)：判断合格且为 update、merge、noop 时有效(避免了重复条目)，
  add、不合格或没有结果时无效，登记时即确定，不需要回填。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tightrein.config import layers
from tightrein.domain.clock import Clock
from tightrein.domain.enums import Access, KnowledgeStatus, KnowledgeWriteDecision, RunnerStatus, Stage, YieldOutcome
from tightrein.retrieval import search
from tightrein.retrieval.errors import InvalidQuery, WriteDecisionInvalid
from tightrein.retrieval.models import EntryDocument, SearchFilters
from tightrein.retrieval.ranking import CandidateSource
from tightrein.retrieval.writer import Content, KnowledgeDraft, KnowledgeWriter
from tightrein.runner import stage_yield
from tightrein.runner.result import RunnerResult
from tightrein.runner.service import Runner
from tightrein.runner.task import Instructions, RunnerTask, SkillRef, Subject
from tightrein.store.files.layout import WorkspaceLayout

ROLE = "knowledge-curator"
OUTPUT_SCHEMA = "runner/roles/knowledge-curator.schema.json"
SKILL = "learn"
REFERENCE = "references/knowledge-curator.md"
PROMPT = "判断下面的知识草稿与已有条目的关系：新增、更新已有条目、合并，还是不必写入。判断规则见参考资料。"


@dataclass(frozen=True)
class WriteOrigin:
    """发起写入的运行：判断任务挂在这次运行与对象下，执行器与模型可以由命令行参数指定。"""

    run_id: str
    stage: Stage
    subject: Subject
    runner_override: str | None = None
    model_override: str | None = None


@dataclass(frozen=True)
class Decision:
    decision: KnowledgeWriteDecision
    target_ids: tuple[str, ...]
    supersedes: tuple[str, ...]
    result: Content | None
    reason: str

    @classmethod
    def from_output(cls, output: Mapping[str, Any]) -> Decision:
        result = output.get("result")
        return cls(
            KnowledgeWriteDecision(output["decision"]), tuple(output["targetIds"]), tuple(output["supersedes"]),
            None if result is None else Content(result["title"], result["summary"], tuple(result["tags"]),
                                                result["body"]),
            output["reason"],
        )


def similar_query(draft: KnowledgeDraft) -> str:
    return " ".join([draft.title, draft.summary, *draft.tags])


def similar(conn: sqlite3.Connection, layout: WorkspaceLayout, sources: Sequence[CandidateSource],
            draft: KnowledgeDraft, *, limit: int | None = None, candidate_factor: int | None = None,
            rrf_k: int | None = None) -> list[EntryDocument]:
    """相似条目最多 limit 个(runtime.retrieval.dedupSimilarLimit，缺省取核心缺省值)；其余参数传给 search.search。"""
    count = int(layers.core_value("runtime.retrieval.dedupSimilarLimit")) if limit is None else limit
    filters = SearchFilters((draft.type.value,), (), KnowledgeStatus.ACTIVE, count)
    try:
        hits = search.search(conn, sources, similar_query(draft), filters, candidate_factor=candidate_factor,
                             rrf_k=rrf_k)
    except InvalidQuery:
        return []
    return [search.read_entry(layout, search.require(conn, hit.id)) for hit in hits]


def instructions(draft: KnowledgeDraft, candidates: Sequence[EntryDocument], feedback: Sequence[str] = ()) -> str:
    parts = [PROMPT, "## 草稿", draft.render(), "## 候选条目"]
    for document in candidates:
        parts += [f"### {document.id}({document.path})", document.body.strip()]
    if feedback:
        parts += ["## 上一次判断的问题", *(f"- {reason}" for reason in feedback)]
    return "\n\n".join(parts) + "\n"


def curator_task(layout: WorkspaceLayout, origin: WriteOrigin, draft: KnowledgeDraft,
                 candidates: Sequence[EntryDocument], attempt: int = 1, feedback: Sequence[str] = ()) -> RunnerTask:
    return RunnerTask(
        run_id=origin.run_id, stage=origin.stage, role=ROLE, subject=origin.subject, attempt=attempt,
        instructions=Instructions(instructions(draft, candidates, feedback), (SkillRef(SKILL, (REFERENCE,)),)),
        workdir=layout.knowledge_dir(), output_schema=OUTPUT_SCHEMA, access=Access.READ_ONLY,
    )


def check_decision(decision: Decision, candidate_ids: Sequence[str]) -> list[str]:
    known = set(candidate_ids)
    reasons = []
    outside = [entry_id for entry_id in (*decision.target_ids, *decision.supersedes) if entry_id not in known]
    if outside:
        reasons.append(f"{'、'.join(outside)} 不在候选条目中，只能引用 {'、'.join(candidate_ids)}")
    if decision.decision is KnowledgeWriteDecision.ADD and decision.target_ids:
        reasons.append("add 不针对已有条目，targetIds 须为空；被推翻的旧条目写在 supersedes 中")
    if decision.decision is not KnowledgeWriteDecision.ADD and decision.supersedes:
        reasons.append("只有 add 可以填写 supersedes")
    if decision.decision is KnowledgeWriteDecision.UPDATE and len(decision.target_ids) != 1:
        reasons.append("update 的 targetIds 须恰好 1 个")
    if decision.decision in (KnowledgeWriteDecision.MERGE, KnowledgeWriteDecision.NOOP) and not decision.target_ids:
        reasons.append(f"{decision.decision.value} 的 targetIds 至少 1 个")
    return reasons


@dataclass(frozen=True)
class WriteOutcome:
    decision: KnowledgeWriteDecision
    written_id: str | None
    superseded_ids: list[str]
    reason: str
    paths: list[Path]


def apply(writer: KnowledgeWriter, draft: KnowledgeDraft, decision: Decision) -> WriteOutcome:
    """按判断改写文件；noop 不写文件。"""
    kind = decision.decision
    if kind is KnowledgeWriteDecision.ADD:
        written, paths = writer.add(draft, decision.supersedes)
        return WriteOutcome(kind, written, list(decision.supersedes), decision.reason, paths)
    if kind is KnowledgeWriteDecision.UPDATE and decision.result is not None:
        paths = writer.update(decision.target_ids[0], decision.result)
        return WriteOutcome(kind, decision.target_ids[0], [], decision.reason, paths)
    if kind is KnowledgeWriteDecision.MERGE and decision.result is not None:
        written, paths = writer.merge(draft, decision.target_ids, decision.result)
        return WriteOutcome(kind, written, list(decision.target_ids), decision.reason, paths)
    return WriteOutcome(KnowledgeWriteDecision.NOOP, None, [], decision.reason, [])


AVOIDED_DUPLICATE = (KnowledgeWriteDecision.UPDATE, KnowledgeWriteDecision.MERGE, KnowledgeWriteDecision.NOOP)


def _record(conn: sqlite3.Connection | None, task: RunnerTask, result: RunnerResult, clock: Clock,
            decision: Decision | None, feedback: Sequence[str]) -> None:
    if conn is None:
        return
    if decision is not None and not feedback and decision.decision in AVOIDED_DUPLICATE:
        outcome, reason = YieldOutcome.USEFUL, f"判断为 {decision.decision.value}，避免了重复条目"
    elif decision is not None and not feedback:
        outcome, reason = YieldOutcome.NO_YIELD, "判断为 add"
    else:
        outcome, reason = YieldOutcome.NO_YIELD, "没有给出合格的判断"
    stage_yield.record(conn, task, result, clock.now(), outcome=outcome, reason=reason)


def decide(runner: Runner, clock: Clock, layout: WorkspaceLayout, origin: WriteOrigin, draft: KnowledgeDraft,
           candidates: Sequence[EntryDocument], conn: sqlite3.Connection | None = None) -> Decision:
    candidate_ids = [document.id for document in candidates]
    feedback: list[str] = []
    for attempt in (1, 2):
        task = curator_task(layout, origin, draft, candidates, attempt, feedback)
        result = runner.run(task, clock=clock, runner_override=origin.runner_override,
                            model_override=origin.model_override)
        if result.status is not RunnerStatus.OK or result.output is None:
            _record(conn, task, result, clock, None, ())
            raise WriteDecisionInvalid((f"执行器没有给出判断：{result.status.value} {result.error_type or ''}".strip(),))
        decision = Decision.from_output(result.output)
        feedback = check_decision(decision, candidate_ids)
        _record(conn, task, result, clock, decision, feedback)
        if not feedback:
            return decision
    raise WriteDecisionInvalid(tuple(feedback))
