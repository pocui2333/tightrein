"""建议沉淀：收集、去重，用户确认后才写入知识库(knowledge/README.md「写入」)。

- 各步骤只在交接的必填事实 `knowledgeSuggestions`(字符串列表)中提建议，不自己写知识库；
- propose：程序收集并按规整后的文字去重，存进待确认清单 `knowledge/proposals.json`；同一条再被提出时只追加对象；
  用户拒绝过的，只有出现拒绝时没有的新对象才重新列为待确认；
- accept(用户确认后)：调用一次模型(调用点 knowledge.curate)对照候选条目判断新增、更新、合并还是不必写入，
  并写出完整条目；判断只能引用给出的候选编号，update 恰好 1 条，merge 与 noop 至少 1 条，不合格时带原因再判一次，
  仍不合格就不写；
- reject：用户拒绝，记下原因。
"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tightrein.agents.call import AgentContext, call, params_for
from tightrein.agents.params import CallParams
from tightrein.agents.result import CallResult
from tightrein.assess import notes as code_notes
from tightrein.knowledge.entries import (
    KINDS,
    SLUG,
    Entry,
    EntryStatus,
    active,
    create,
    find,
    load,
    location_problem,
    save,
    supersede,
)
from tightrein.knowledge.match import classify, render, select
from tightrein.prompts.build import build
from tightrein.protocol.handoff import Handoff, load_schema
from tightrein.protocol.naming import format_iso, local_date
from tightrein.store.files.json import read_json, write_json
from tightrein.store.files.layout import WorkspaceLayout

if TYPE_CHECKING:
    from tightrein.protocol.runtime import Runtime

POINT = "knowledge.curate"
FACT_KEY = "knowledgeSuggestions"
SCHEMA_PATH = Path(__file__).with_name("curate.schema.json")
ATTEMPTS = 2
NO_FEEDBACK = "无"

Invoke = Callable[[CallParams, AgentContext], CallResult]


class ProposalStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class DecisionKind(StrEnum):
    ADD = "add"
    UPDATE = "update"
    MERGE = "merge"
    NOOP = "noop"


class ProposalError(Exception):
    """建议不存在、不是待确认，或模型没有给出合格的判断。"""


@dataclass
class Proposal:
    id: str  # 四位编号
    text: str
    subjects: list[str]
    status: ProposalStatus
    created_at: str
    decided_at: str | None = None
    reason: str | None = None  # 拒绝的原因，或判断的依据
    entry: str | None = None  # 写入或判定已覆盖它的条目
    rejected_subjects: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "text": self.text, "subjects": self.subjects, "status": self.status.value,
                "createdAt": self.created_at, "decidedAt": self.decided_at, "reason": self.reason,
                "entry": self.entry, "rejectedSubjects": self.rejected_subjects}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Proposal:
        return cls(data["id"], data["text"], list(data["subjects"]), ProposalStatus(data["status"]),
                   data["createdAt"], data.get("decidedAt"), data.get("reason"), data.get("entry"),
                   list(data.get("rejectedSubjects") or []))


@dataclass(frozen=True)
class Content:
    kind: str
    slug: str
    title: str
    summary: str
    locations: tuple[str, ...]
    body: str


@dataclass(frozen=True)
class Decision:
    kind: DecisionKind
    target_ids: tuple[str, ...]
    supersedes: tuple[str, ...]
    content: Content | None
    reason: str

    @classmethod
    def from_output(cls, output: dict[str, Any]) -> Decision:
        entry = output.get("entry")
        content = None if entry is None else Content(
            entry["kind"], entry["slug"], entry["title"], entry["summary"], tuple(entry["locations"]), entry["body"])
        return cls(DecisionKind(output["decision"]), tuple(output["targetIds"]), tuple(output["supersedes"]), content,
                   output["reason"])


# 收集


def suggestions_in(handoff: Handoff) -> list[str]:
    """一份交接中的「建议沉淀」。"""
    found = handoff.facts.get(FACT_KEY) or []
    return [str(item).strip() for item in found if str(item).strip()]


def propose(runtime: Runtime, subject: str, suggestions: list[str]) -> None:
    """收集建议、去重，列为待确认；不写知识库。"""
    add(runtime.workspace, subject, suggestions, format_iso(runtime.clock.now()))


def add(layout: WorkspaceLayout, subject: str, suggestions: Sequence[str], now: str) -> list[Proposal]:
    """返回新列为待确认(或重新列为待确认)的建议。"""
    proposals = read(layout)
    by_text = {normalize(item.text): item for item in proposals}
    listed: list[Proposal] = []
    for text in (item.strip() for item in suggestions if item.strip()):
        same = by_text.get(normalize(text))
        if same is None:
            same = Proposal(f"{len(proposals) + 1:04d}", text, [subject], ProposalStatus.PENDING, now)
            proposals.append(same)
            by_text[normalize(text)] = same
            listed.append(same)
        elif subject in same.subjects:
            continue
        elif same.status is ProposalStatus.REJECTED and subject not in same.rejected_subjects:
            same.subjects.append(subject)
            same.status, same.decided_at, same.reason = ProposalStatus.PENDING, None, None
            listed.append(same)
        elif same.status is ProposalStatus.PENDING:
            same.subjects.append(subject)
    write(layout, proposals)
    return listed


def normalize(text: str) -> str:
    """去重用：去掉空白与标点，英文转小写。"""
    folded = unicodedata.normalize("NFKC", text).lower()
    return "".join(char for char in folded if not char.isspace() and not unicodedata.category(char).startswith("P"))


# 清单


def proposals_path(layout: WorkspaceLayout) -> Path:
    return layout.knowledge_proposals


def read(layout: WorkspaceLayout) -> list[Proposal]:
    path = proposals_path(layout)
    return [Proposal.from_json(item) for item in read_json(path)] if path.is_file() else []


def write(layout: WorkspaceLayout, proposals: Sequence[Proposal]) -> None:
    write_json(proposals_path(layout), [item.to_json() for item in proposals])


def pending(layout: WorkspaceLayout) -> list[Proposal]:
    return [item for item in read(layout) if item.status is ProposalStatus.PENDING]


def reject(layout: WorkspaceLayout, proposal_id: str, reason: str, now: str) -> Proposal:
    proposals = read(layout)
    proposal = _pending(proposals, proposal_id)
    proposal.status, proposal.decided_at, proposal.reason = ProposalStatus.REJECTED, now, reason
    proposal.rejected_subjects = sorted({*proposal.rejected_subjects, *proposal.subjects})
    write(layout, proposals)
    return proposal


# 确认后写入


def accept(runtime: Runtime, proposal_id: str, *, invoke: Invoke = call) -> Proposal:
    """用户确认一条建议：由模型对照候选条目判断并写出完整条目，程序校验后改写文件。

    候选的条数与 token 上限取 controls 的 knowledge.curate.candidates、candidateTokens。
    """
    section = runtime.settings.section(POINT)
    candidate_limit, candidate_tokens = int(section["candidates"]), int(section["candidateTokens"])
    layout = runtime.workspace
    proposals = read(layout)
    proposal = _pending(proposals, proposal_id)
    entries = active(load(layout).entries)
    candidates = _candidates(layout, entries, proposal.subjects, candidate_limit, candidate_tokens)
    decision = _decide(runtime, proposal, candidates, candidate_tokens, invoke)
    now = runtime.clock.now()
    today = local_date(now).isoformat()
    commit = runtime.git.head().commit
    written = apply(layout, decision, entries, proposal.subjects, today=today, commit=commit)
    proposal.status, proposal.decided_at = ProposalStatus.ACCEPTED, format_iso(now)
    proposal.reason, proposal.entry = decision.reason, written
    write(layout, proposals)
    return proposal


def check_decision(decision: Decision, candidate_ids: Sequence[str]) -> list[str]:
    """语义校验，返回全部不合格的原因。"""
    known = set(candidate_ids)
    reasons: list[str] = []
    outside = [item for item in (*decision.target_ids, *decision.supersedes) if item not in known]
    if outside:
        allowed = "、".join(candidate_ids) or "(没有候选)"
        reasons.append(f"{'、'.join(outside)} 不在候选条目中，只能引用 {allowed}")
    if decision.kind is DecisionKind.ADD and decision.target_ids:
        reasons.append("add 不针对已有条目，targetIds 须为空；被推翻的旧条目写在 supersedes 中")
    if decision.kind is not DecisionKind.ADD and decision.supersedes:
        reasons.append("只有 add 可以填写 supersedes")
    if decision.kind is DecisionKind.UPDATE and len(decision.target_ids) != 1:
        reasons.append("update 的 targetIds 须恰好 1 个")
    if decision.kind in (DecisionKind.MERGE, DecisionKind.NOOP) and not decision.target_ids:
        reasons.append(f"{decision.kind.value} 的 targetIds 至少 1 个")
    if decision.kind is DecisionKind.NOOP:
        return reasons
    if decision.content is None:
        reasons.append(f"{decision.kind.value} 须写出完整条目 entry")
        return reasons
    content = decision.content
    if content.kind not in KINDS:
        reasons.append(f"entry.kind 只能是 {'、'.join(KINDS)}")
    if not SLUG.match(content.slug):
        reasons.append(f"entry.slug 只能由小写英文、数字与连字符组成：{content.slug!r}")
    reasons += [f"entry.{name} 不能为空" for name in ("title", "summary", "body")
                if not getattr(content, name).strip()]
    for index, location in enumerate(content.locations):
        problem = location_problem(location)
        if problem is not None:
            reasons.append(f"entry.locations[{index}]：{problem}")
    return reasons


def apply(layout: WorkspaceLayout, decision: Decision, entries: Sequence[Entry], subjects: Sequence[str], *,
          today: str, commit: str | None) -> str | None:
    """按判断改写条目文件，返回写入(或已覆盖它)的条目编号；条目从不删除。"""
    targets = [entry for item in decision.target_ids if (entry := find(list(entries), item)) is not None]
    if decision.kind is DecisionKind.NOOP:
        return targets[0].id if targets else None
    content = decision.content
    assert content is not None  # check_decision 已保证
    if decision.kind is DecisionKind.UPDATE:
        target = targets[0]
        updated = replace(target, title=content.title, summary=content.summary, body=content.body,
                          locations=content.locations, updated=today, commit=commit, status=EntryStatus.ACTIVE,
                          sources=tuple(dict.fromkeys([*target.sources, *subjects])))
        return save(updated).id
    created = create(layout, kind=content.kind, slug=content.slug, title=content.title, summary=content.summary,
                     body=content.body, locations=content.locations, today=today, commit=commit,
                     sources=tuple(subjects))
    replaced = decision.supersedes if decision.kind is DecisionKind.ADD else decision.target_ids
    for item in replaced:
        old = find(list(entries), item)
        if old is not None:
            supersede(old, created.id, today)
    return created.id


# 内部


def _pending(proposals: Sequence[Proposal], proposal_id: str) -> Proposal:
    proposal = next((item for item in proposals if item.id == proposal_id), None)
    if proposal is None:
        raise ProposalError(f"建议 {proposal_id} 不存在")
    if proposal.status is not ProposalStatus.PENDING:
        raise ProposalError(f"建议 {proposal_id} 已是 {proposal.status.value}，只能处理待确认的")
    return proposal


def _candidates(layout: WorkspaceLayout, entries: list[Entry], subjects: Sequence[str], limit: int,
                tokens: int) -> list[Entry]:
    """候选：按提出它的对象的代码笔记中的文件匹配；没有笔记时取最近更新的有效条目。"""
    files = [path for subject in subjects if (notes := code_notes.load(layout, subject)) is not None
             for path in notes.files]
    if files:
        return select(entries, classify(files), limit_entries=limit, limit_tokens=tokens)
    return sorted(entries, key=lambda entry: (entry.updated, entry.id), reverse=True)[:limit]


def _decide(runtime: Runtime, proposal: Proposal, candidates: Sequence[Entry], tokens: int,
            invoke: Invoke) -> Decision:
    schema = load_schema(SCHEMA_PATH)
    runtime.workspace.knowledge_dir.mkdir(parents=True, exist_ok=True)
    candidate_ids = [entry.id for entry in candidates]
    feedback: list[str] = []
    for _ in range(ATTEMPTS):
        variables = {
            "suggestion": proposal.text,
            "subjects": "、".join(proposal.subjects),
            "candidates": render(list(candidates), limit_tokens=tokens),
            "feedback": "\n".join(f"- {reason}" for reason in feedback) or NO_FEEDBACK,
        }
        model = runtime.settings.model_for(POINT)
        prompt = build(POINT, variables, language=runtime.language, schema=schema, tool=model.tool)
        params = params_for(POINT, settings=runtime.settings, run=runtime.run, subject=None, prompt=prompt.text,
                            schema=schema, workdir=runtime.workspace.knowledge_dir, prompt_hash=prompt.hash)
        result = invoke(params, runtime.agents)
        if not result.ok or result.output is None:
            raise ProposalError(f"{POINT} 没有给出判断：{result.status.value} {result.error or ''}".strip())
        decision = Decision.from_output(result.output)
        feedback = check_decision(decision, candidate_ids)
        if not feedback:
            return decision
    raise ProposalError(f"{POINT} 两次判断都不合格：" + "；".join(feedback))
