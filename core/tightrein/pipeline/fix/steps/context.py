"""修复的上下文(architecture/07 4.2)：Issue 文件是唯一来源(用户可能在审阅时改过正文)。

读取 Issue 的各节与 frontmatter(用户需求的 Issue 取除「历史」外的全文作为需求)、分诊交接文档中的复杂度、三类标记、影响类别与预估改动文件、上一次合并前验证的
交接文档(验证失败退回时)，以及按根因文件预取的知识。复杂度只决定预算：低复杂度取 stages.fix.budget.low，
中、高复杂度取 stages.fix.budget.high；评审深度由通道与风险判定决定。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from tightrein.config.project import MissingSetting, ProjectConfig
from tightrein.domain import issue_sections
from tightrein.domain.issue_sections import (
    ACCEPTANCE,
    CAUSE,
    DIRECTION,
    EVIDENCE,
    EXPECTED,
    HISTORY,
    IMPACT,
    NOTES,
    PROBLEM,
    REFERENCES,
    REPRODUCE,
    SCOPE,
)
from tightrein.domain.enums import Complexity, ContextKind, ImpactKind, RunStage, VerifyPhase
from tightrein.domain.issue import Issue
from tightrein.pipeline.issue.steps import edit
from tightrein.pipeline.issue.steps import select as issue_select
from tightrein.retrieval.context import EMPTY, ContextBundle, ContextRequest
from tightrein.store.files import handoff_files, issue_files
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.repos import handoffs, issues, problems
from tightrein.store.repos.issues import IssueRecord

# 给修复角色的 Issue 正文：问题、验收标准与注意事项在前(关键约束放在开头)，旧版式另有的期望与实际、完整证据在后
ISSUE_SECTIONS = (PROBLEM, ACCEPTANCE, NOTES, SCOPE, CAUSE, REPRODUCE, IMPACT, DIRECTION, EXPECTED, EVIDENCE,
                  REFERENCES)
MANUAL_NOTE = "本 Issue 是用户直接提出的需求，没有关联问题与复现检查；以下正文即需求，勘察与计划以它为准。"
CHECKBOX = ("[ ] ", "[x] ", "[X] ")
FLAG_NAMES = ("design", "dataStructure", "publicContract")

ContextFor = Callable[[ContextRequest], ContextBundle]


class IssueMissing(LookupError):
    """没有这个 Issue。"""


@dataclass
class FixContext:
    record: IssueRecord
    sections: dict[str, str]
    triage: Mapping[str, Any]
    fingerprints: tuple[str, ...]
    complexity: Complexity = Complexity.MEDIUM
    flags: dict[str, bool] = field(default_factory=dict)
    impact_kind: ImpactKind | None = None
    verify_report: Mapping[str, Any] | None = None
    knowledge: str = EMPTY
    # 用户的决定与补充(steps/decisions.py 渲染)，由 FixService 在出计划与实施前填入
    decisions: str = ""
    # 分层代码摘要(steps/brief.py 渲染)：勘察后生成，出计划、写测试、写代码与评审共用
    brief: str = ""

    @property
    def issue(self) -> Issue:
        return self.record.issue

    @property
    def issue_id(self) -> str:
        return self.issue.id

    def section(self, key: str) -> str | None:
        """按小节键取 Issue 的一节(标题为任一语言或旧标题)。"""
        return issue_sections.find(self.sections, key)

    def keyed(self, keys: tuple[str, ...]) -> list[str]:
        """按键列出各节，标题沿用文件中的实际标题。"""
        return [f"## {title}\n\n{text.strip()}" for key in keys for title, text in self.sections.items()
                if issue_sections.key_of(title) == key]

    @property
    def acceptance(self) -> list[str]:
        items = []
        for line in (self.section(ACCEPTANCE) or "").splitlines():
            if not line.startswith("- "):
                continue
            item = line[2:].strip()
            item = item[len(next((box for box in CHECKBOX if item.startswith(box)), "")):].strip()
            if item:
                items.append(item)
        return items

    @property
    def root_files(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(location.rsplit(":", 1)[0] for location in self.issue.root_cause))

    def issue_text(self) -> str:
        parts = [f"# Issue {self.issue_id}：{self.issue.title}"]
        if self.issue.is_manual:
            parts.append(MANUAL_NOTE)
            parts += [f"## {name}\n\n{text.strip()}" for name, text in self.sections.items()
                      if issue_sections.key_of(name) != HISTORY]
            return "\n\n".join(parts)
        parts += self.keyed(ISSUE_SECTIONS)
        if self.issue.findings:
            parts.append(f"发现报告：{self.issue.findings}")
        return "\n\n".join(parts)


def _flags(outputs: Mapping[str, Any]) -> dict[str, bool]:
    flags = outputs.get("flags") or {}
    return {name: bool((flags.get(name) or {}).get("flagged")) for name in FLAG_NAMES}


def _impact(outputs: Mapping[str, Any]) -> ImpactKind | None:
    kind = ((outputs.get("evidence") or {}).get("impact") or {}).get("kind")
    return ImpactKind(kind) if kind else None


def latest_verify(conn: sqlite3.Connection, layout: WorkspaceLayout, issue_id: str) -> Mapping[str, Any] | None:
    record = handoffs.get(conn, RunStage.VERIFY, issue_id, phase=VerifyPhase.LOCAL)
    if record is None:
        return None
    return handoff_files.read(layout.root / record.path)["outputs"]


def load(conn: sqlite3.Connection, layout: WorkspaceLayout, issue_id: str,
         context_for: ContextFor | None = None) -> FixContext:
    record = issues.get(conn, issue_id)
    if record is None:
        raise IssueMissing(f"没有 Issue {issue_id}")
    document = issue_files.read(layout.root / record.path)
    record = IssueRecord(document.issue, record.path, record.file_sha256)
    outputs: Mapping[str, Any] = {}
    if document.issue.problems:
        try:
            outputs = issue_select.triage_outputs(layout, conn, document.issue.problems[0])
        except LookupError:
            outputs = {}
    fingerprints = tuple(found.fingerprint for problem_id in document.issue.problems
                         if (found := problems.get(conn, problem_id)) is not None)
    complexity = Complexity(outputs["complexity"]) if outputs.get("complexity") else Complexity.MEDIUM
    context = FixContext(record, edit.sections(document.body), outputs, fingerprints, complexity, _flags(outputs),
                         _impact(outputs), latest_verify(conn, layout, issue_id))
    if context_for is not None:
        request = ContextRequest(ContextKind.FIX, paths=context.root_files, keywords=document.issue.title,
                                 problem_id=document.issue.problems[0] if document.issue.problems else None)
        context.knowledge = context_for(request).render()
    return context


def budget(config: ProjectConfig, complexity: Complexity) -> float | None:
    key = "low" if complexity is Complexity.LOW else "high"
    try:
        return float(config.get(f"stages.fix.budget.{key}"))
    except MissingSetting:
        return None
