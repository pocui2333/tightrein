"""按通道与模型的修复指标(redesign/08-learn.md 第 2 节)：一次通过率、每个修复的费用、被撤销的比例、用户纠正次数。

- 一次通过率：每个 Issue 在本周第一份带轮次的修复交接文档中，第一轮的复现测试与项目检查全部通过(rounds[0].checksPassed)；
  模型取同一运行中写代码的调用(fix-executor、fix-session)在 stage_yield 中记下的模型，没有记录时为 unknown；
- 每个修复的费用：本周以已修复关闭的 Issue，该 Issue 在 fix、verify、release 环节全部调用的费用之和，取平均；
- 被撤销的比例：learn.regressionWindowDays 天内以已修复关闭的 Issue 中，撤销 PR 已确认或已执行的比例；
- 用户纠正次数：本周驳回修复计划、关闭 Issue、确认撤销 PR、改判分诊结论的次数。
通道取该 Issue 最近一份修复交接文档的 lane，没有时为 unknown。
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import timedelta
from typing import Any

from tightrein.domain.enums import IssueEvent, OperationKind, OperationStatus, RunStage, Stage, TriageOutcome
from tightrein.pipeline.learn.steps.metric_base import MetricContext, MetricValue, dim, fixed_closes
from tightrein.store.files import handoff_files
from tightrein.store.repos import handoffs, pending_operations
from tightrein.store.repos.handoffs import HandoffRecord
from tightrein.store.repos.metric_snapshots import OVERALL

UNKNOWN = "unknown"
WRITERS = ("fix-executor", "fix-session")
COST_STAGES = (Stage.FIX.value, Stage.VERIFY.value, Stage.RELEASE.value)
DONE_OPERATIONS = (OperationStatus.CONFIRMED, OperationStatus.EXECUTED)
CORRECTIONS = ("plan-rejected", "issue-closed", "reverted", "triage-overridden")


def _outputs(ctx: MetricContext, record: HandoffRecord) -> dict[str, Any]:
    return handoff_files.read(ctx.layout.root / record.path)["outputs"]


def _fix_records(ctx: MetricContext, issue_id: str) -> list[HandoffRecord]:
    return [record for record in handoffs.for_subject(ctx.conn, issue_id) if record.stage is RunStage.FIX]


def lane_of(ctx: MetricContext, issue_id: str) -> str:
    for record in reversed(_fix_records(ctx, issue_id)):
        lane = _outputs(ctx, record).get("lane")
        if lane:
            return str(lane)
    return UNKNOWN


def _writer_model(ctx: MetricContext, run_id: str, issue_id: str) -> str:
    marks = ", ".join("?" for _ in WRITERS)
    row = ctx.conn.execute(f"SELECT model FROM stage_yield WHERE run_id = ? AND subject_id = ? AND role IN ({marks}) "
                           "AND model IS NOT NULL ORDER BY id LIMIT 1", (run_id, issue_id, *WRITERS)).fetchone()
    return row["model"] if row is not None else UNKNOWN


def _ratios(metric: str, items: Iterable[tuple[dict[str, str], bool]]) -> list[MetricValue]:
    """items 为(各维度的取值、是否计入分子)；按 all 与每个维度取值分别计算比率。"""
    groups: dict[str, list[bool]] = {OVERALL: []}
    for dimensions, hit in items:
        groups[OVERALL].append(hit)
        for key, value in dimensions.items():
            groups.setdefault(dim(key, value), []).append(hit)
    return [MetricValue.ratio(metric, name, sum(values), len(values)) for name, values in sorted(groups.items())]


def first_pass(ctx: MetricContext) -> list[MetricValue]:
    firsts: dict[str, tuple[HandoffRecord, dict[str, Any]]] = {}
    for record in handoffs.TABLE.find(ctx.conn, stage=RunStage.FIX):
        if record.subject_id in firsts:
            continue
        outputs = _outputs(ctx, record)
        if outputs.get("rounds"):
            firsts[record.subject_id] = (record, outputs)
    items = []
    for issue_id, (record, outputs) in firsts.items():
        if not ctx.window.contains(record.created_at):
            continue
        dimensions = {"lane": str(outputs.get("lane") or UNKNOWN), "model": _writer_model(ctx, record.run_id, issue_id)}
        items.append((dimensions, bool(outputs["rounds"][0]["checksPassed"])))
    return _ratios("first-pass", items)


def fix_cost(ctx: MetricContext) -> list[MetricValue]:
    marks = ", ".join("?" for _ in COST_STAGES)
    groups: dict[str, list[float]] = {OVERALL: []}
    for issue_id in fixed_closes(ctx.conn, ctx.window.start, ctx.window.end):
        row = ctx.conn.execute(f"SELECT SUM(cost_usd) FROM stage_yield WHERE subject_id = ? AND stage IN ({marks})",
                               (issue_id, *COST_STAGES)).fetchone()
        spent = float(row[0] or 0.0)
        groups[OVERALL].append(spent)
        groups.setdefault(dim("lane", lane_of(ctx, issue_id)), []).append(spent)
    return [MetricValue.ratio("fix-cost-usd", name, round(sum(values), 4), len(values))
            for name, values in sorted(groups.items())]


def _reverted(ctx: MetricContext) -> set[str]:
    return {record.subject_id for record in pending_operations.find(ctx.conn)
            if record.kind is OperationKind.REVERT_PULL_REQUEST and record.status in DONE_OPERATIONS}


def revert_rate(ctx: MetricContext) -> list[MetricValue]:
    days = ctx.config.whole_threshold("learn.regressionWindowDays")
    closed = fixed_closes(ctx.conn, ctx.window.end - timedelta(days=days), ctx.window.end)
    reverted = _reverted(ctx)
    return _ratios("revert-rate", (({"lane": lane_of(ctx, issue_id)}, issue_id in reverted) for issue_id in closed))


def corrections(ctx: MetricContext) -> dict[str, list[str]]:
    """本周各类用户纠正涉及的对象(同一对象可出现多次)。"""
    found: dict[str, list[str]] = {kind: [] for kind in CORRECTIONS}
    for record in pending_operations.find(ctx.conn):
        if record.kind is OperationKind.FIX_PLAN and record.status is OperationStatus.REJECTED \
                and ctx.window.contains(record.decided_at):
            found["plan-rejected"].append(record.subject_id)
        if record.kind is OperationKind.REVERT_PULL_REQUEST and record.status in DONE_OPERATIONS \
                and ctx.window.contains(record.decided_at):
            found["reverted"].append(record.subject_id)
    found["issue-closed"] = [row["issue_id"] for row in ctx.conn.execute(
        "SELECT issue_id FROM issue_events WHERE event = ? AND at >= ? AND at < ? ORDER BY at",
        (IssueEvent.USER_CLOSED.value, *ctx.between()))]
    found["triage-overridden"] = [row["problem_id"] for row in ctx.conn.execute(
        "SELECT problem_id FROM triage_results WHERE outcome = ? AND outcome_at >= ? AND outcome_at < ?",
        (TriageOutcome.OVERRIDDEN.value, *ctx.between()))]
    return found


def user_corrections(ctx: MetricContext) -> list[MetricValue]:
    found = corrections(ctx)
    return [MetricValue.count("user-corrections", OVERALL, sum(len(items) for items in found.values())),
            *(MetricValue.count("user-corrections", dim("kind", kind), len(items)) for kind, items in found.items())]
