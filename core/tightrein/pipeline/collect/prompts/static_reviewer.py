"""StaticReviewer：probes/static 的 Reviewer 协议的实现(architecture/04 5.4)。

每次运行由 collect 以运行编号构造一个实例。缺陷模式与已接受的取舍取知识库中 active 的条目；增量审查的相关知识经
注入的 context(KnowledgeService.context_for)预取，没有注入时写明没有相关条目。执行器的状态、原因与会话记录原样
放进 ReviewResult、Verification，由探针决定整次巡检的结果；审查结果另带耗时、费用与当天预算是否用尽。
增量审查与基线审查的相关知识按改动文件或本批文件预取。
全量扫描的种子是知识库中 active 的缺陷模式：规则库(工作区 rules/)能表达的同类问题由 Semgrep 确定性地检测，
写成缺陷模式条目的是规则表达不了或验证没有通过的部分(learn 的缺陷变规则)。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from pathlib import Path

from tightrein.config.project import ProjectConfig
from tightrein.domain.clock import Clock
from tightrein.domain.enums import ContextKind, KnowledgeStatus, KnowledgeType, RunnerStatus
from tightrein.pipeline.collect.prompts import tasks
from tightrein.pipeline.collect.prompts.tasks import TaskContext
from tightrein.runner.roles import Overrides, run
from tightrein.sources.static.baseline import Batch
from tightrein.sources.static.reviewer import Claim, ReviewResult, ToolFinding, Verification
from tightrein.sources.static.scope import Scope
from tightrein.retrieval import search
from tightrein.retrieval.context import EMPTY, ContextBundle, ContextRequest
from tightrein.runner.result import DAILY_BUDGET, RunnerResult
from tightrein.runner.service import Runner
from tightrein.store.files.layout import ToolLayout, WorkspaceLayout
from tightrein.store.repos import knowledge

ContextFor = Callable[[ContextRequest], ContextBundle]


def _detail(result: RunnerResult) -> str | None:
    return None if result.status is RunnerStatus.OK else (result.error_type or result.status.label)


def _kinds(result: RunnerResult) -> tuple[str, ...]:
    """边界检查违规的种类(去重，保持顺序)。"""
    return tuple(dict.fromkeys(item.kind.value for item in result.violations))


class StaticReviewer:
    def __init__(self, *, runner: Runner, tool: ToolLayout, layout: WorkspaceLayout, config: ProjectConfig,
                 conn: sqlite3.Connection, clock: Clock, run_id: str, workdir: Path,
                 context: ContextFor | None = None, overrides: Overrides = Overrides()) -> None:
        self.runner = runner
        self.layout = layout
        self.conn = conn
        self.clock = clock
        self.context = context
        self.overrides = overrides
        self.ctx = TaskContext(tool, config, run_id, workdir)
        self.verified = 0
        self._yield_conn = None if layout.output_dir is not None else conn

    def _active(self, kind: KnowledgeType) -> list[knowledge.KnowledgeRecord]:
        return knowledge.find(self.conn, types=(kind.value,), status=KnowledgeStatus.ACTIVE)

    def _review_result(self, result: RunnerResult) -> ReviewResult:
        output = result.output if result.status is RunnerStatus.OK and result.output is not None else {}
        return ReviewResult(result.status, tuple(Claim.from_dict(item) for item in output.get("claims", [])),
                            tuple(output.get("excluded", [])), result.transcript_path, _detail(result),
                            result.duration_ms, result.usage.cost_usd, result.error_type == DAILY_BUDGET,
                            _kinds(result))

    def _known(self, paths: Sequence[str]) -> str:
        if self.context is None:
            return EMPTY
        return self.context(ContextRequest(ContextKind.STATIC_REVIEW, paths=tuple(paths))).render()

    def review(self, scope: Scope, tool_findings: list[ToolFinding]) -> ReviewResult:
        task = tasks.review_task(self.ctx, scope, tool_findings, self._known(scope.changed_files))
        return self._review_result(run(self.runner, task, self.clock, self.overrides, self._yield_conn))

    def review_baseline(self, batch: Batch, tool_findings: list[ToolFinding]) -> ReviewResult:
        task = tasks.baseline_task(self.ctx, batch, tool_findings, self._known(batch.paths))
        return self._review_result(run(self.runner, task, self.clock, self.overrides, self._yield_conn))

    def defect_patterns(self) -> tuple[str, ...]:
        return tuple(record.id for record in self._active(KnowledgeType.DEFECT_PATTERN))

    def scan_variants(self, pattern_id: str, scope: Scope) -> ReviewResult:
        pattern = search.read_entry(self.layout, search.require(self.conn, pattern_id)).body
        tradeoffs = "\n".join(f"- {record.id} {record.summary}" for record in self._active(KnowledgeType.TRADEOFF))
        task = tasks.variant_task(self.ctx, pattern_id, pattern, tradeoffs or "(无)")
        return self._review_result(run(self.runner, task, self.clock, self.overrides, self._yield_conn))

    def verify(self, claim: Claim) -> Verification:
        self.verified += 1
        result = run(self.runner, tasks.verify_task(self.ctx, claim, self.verified), self.clock, self.overrides,
                     self._yield_conn)
        output = result.output if result.status is RunnerStatus.OK else None
        return Verification(result.status, output, result.transcript_path, _detail(result), self.clock.now(),
                            _kinds(result))
