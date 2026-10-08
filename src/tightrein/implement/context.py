"""实施的上下文：Issue、代码笔记、用户的决定与补充、各小步骤最近一次的交接。

各小步骤的入口统一为 `run(runtime, context) -> Handoff`：只读 context，交出这一步的 handoff(由 implement.py 落盘、推进)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from tightrein.assess import notes
from tightrein.assess.issue import files as issue_files
from tightrein.assess.issue.transitions import HISTORY
from tightrein.assess.notes import CodeNotes
from tightrein.knowledge.entries import Entry
from tightrein.knowledge.match import match
from tightrein.protocol import recovery
from tightrein.protocol.git import Git, GitError
from tightrein.protocol.handoff import Handoff
from tightrein.store.tables import issues
from tightrein.store.tables.issues import Issue

if TYPE_CHECKING:
    from tightrein.protocol.runtime import Runtime

STEPS = (
    "implement.prepare",
    "implement.locate",
    "implement.design",
    "implement.approve",
    "implement.code",
    "implement.check",
    "implement.review",
    "implement.deliver",
)


@dataclass(frozen=True)
class Decision:
    """用户在关卡上的决定：approve(可带选项序号)或 reject(必带原因)，及补充说明。"""

    point: str
    verdict: str  # approve、reject
    option: int | None
    note: str | None
    at: str


@dataclass(frozen=True)
class Risk:
    """风险判定：决定是否等用户确认方案、是否深度审查、合并是否人工。"""

    high: bool
    reasons: tuple[str, ...]
    high_risk_paths: tuple[str, ...] = ()


@dataclass
class ImplementContext:
    issue: Issue
    body: str  # 00-issue-body.md 的正文(已去空小节)
    notes: CodeNotes | None
    knowledge: list[Entry]
    decisions: list[Decision]
    worktree: Path | None  # 准备之前为 None
    git: Git | None  # worktree 上的 Git
    base_commit: str | None
    round: int  # 编码、自检、审查这一圈的轮次，从 1 起
    latest: dict[str, Handoff] = field(default_factory=dict)  # 每个小步骤最近一次的交接
    risk: Risk | None = None

    def last(self, point: str) -> Handoff | None:
        return self.latest.get(point)


DECISIONS = "decisions"  # issues.extra 中用户决定的列表(命令行 approve、reject 与 approve/approve.record 写入)
APPROVE = "approve"
STAGE = "implement"
ACTOR = "tightrein"  # 程序自己触发的转换，不是用户的决定
PREPARE = "implement.prepare"
DESIGN = "implement.design"
CODE = "implement.code"


def load(runtime: Runtime, issue: str) -> ImplementContext:
    """从 store 与各步的 handoff.json 读出实施上下文；worktree 与基准取自准备的交接。

    基准取 worktree HEAD 与主干的最近公共祖先(Git.review_base)，只算一次，检查点、自检、审查、交付共用：合并过主干后
    它就是最近一次合并进来的主干版本，主干只改了别的文件时 diff 哈希不变，不必重新审查。
    """
    record = issues.get(runtime.conn, issue)
    if record is None:
        raise LookupError(f"没有 Issue {issue}")
    latest: dict[str, Handoff] = {}
    for checkpoint in recovery.checkpoints(runtime.workspace, issue):
        if checkpoint.handoff.point in STEPS:
            latest[checkpoint.handoff.point] = checkpoint.handoff
    prepared = latest.get(PREPARE)
    worktree = Path(prepared.facts["worktree"]) if prepared is not None and prepared.facts.get("worktree") else None
    if worktree is not None and not worktree.is_dir():
        worktree = None
    git = runtime.git.at(worktree) if worktree is not None else None
    code_notes = notes.load(runtime.workspace, issue)
    design = latest.get(DESIGN)
    coded = latest.get(CODE)
    return ImplementContext(
        issue=record,
        body=issue_files.read_body(runtime.workspace, issue),
        notes=code_notes,
        knowledge=_knowledge(runtime, code_notes, design),
        decisions=_decisions(record, latest),
        worktree=worktree,
        git=git,
        base_commit=_base(git, prepared),
        round=(coded.round or 1) if coded is not None else 1,
        latest=latest,
        risk=_risk(design),
    )


def _decisions(record: Issue, latest: dict[str, Handoff]) -> list[Decision]:
    """用户的决定，按时间先后。

    - 关卡上的决定(issues.extra.decisions)：命令行记下 Issue 当时所在的步骤(`step`)，没有时取 `point`；
    - 停下之后的放行：Issue 已转为待决定，命令行按状态机走 APPROVE(只记进历史)。实施开始之后用户的放行也是一个
      决定(approve，补充说明取历史的 note)，否则续跑时看不到新决定，会在同一处再停一次。
    """
    found = [Decision(item.get("step") or item["point"], item["verdict"], item.get("option"), item.get("note"),
                      item["at"]) for item in record.extra.get(DECISIONS) or []]
    started = min((handoff.created_at or "" for handoff in latest.values()), default=None)
    if started is not None:
        found += [Decision(STAGE, APPROVE, None, entry.get("note"), str(entry["at"]))
                  for entry in record.extra.get(HISTORY) or []
                  if entry.get("event") == APPROVE and entry.get("actor") != ACTOR and str(entry["at"]) > started]
    return sorted(found, key=lambda item: item.at)


def _base(git: Git | None, prepared: Handoff | None) -> str | None:
    if git is None or prepared is None:
        return None
    try:
        return git.review_base(f"origin/{git.main_branch}")
    except GitError:
        found = prepared.facts.get("baseCommit")
        return str(found) if found else None


def _knowledge(runtime: Runtime, code_notes: CodeNotes | None, design: Handoff | None) -> list[Entry]:
    paths = list(code_notes.files) if code_notes is not None else []
    if design is not None:
        paths += [str(item["path"]) for item in design.facts.get("files") or [] if item.get("path") not in paths]
    if not paths:
        return []
    section = runtime.settings.section("implement")
    return match(runtime.workspace, paths, limit_entries=int(section["knowledgeEntries"]),
                 limit_tokens=int(section["knowledgeTokens"]))


def _risk(design: Handoff | None) -> Risk | None:
    found = design.facts.get("risk") if design is not None else None
    if not found:
        return None
    return Risk(bool(found["high"]), tuple(found.get("reasons") or ()), tuple(found.get("highRiskPaths") or ()))
