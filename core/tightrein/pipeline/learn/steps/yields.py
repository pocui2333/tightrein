"""环节效益的有效产出回填与汇总(design 14.2)。

- 回填只处理 outcome 为 pending 的记录：执行器没有返回结果的直接判为无效；其余按 RULES 中(环节、角色前缀)对应的
  判定函数查询数据库与交接文档，结果仍未确定的保持 pending(不计入分子也不计入分母)；没有规则的角色保持 pending；
- 写入去重(knowledge-curator)的判断在登记时已确定，不经回填；
- 汇总按(环节、角色组)：角色组去掉角色名末尾的序号与缺陷模式编号，例如 claim-verifier-2 归入 claim-verifier。
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from tightrein.config.project import ProjectConfig
from tightrein.domain.enums import (
    CheckResult,
    IssueStatus,
    Probe,
    ProblemEvent,
    RunnerStatus,
    RunStage,
    Stage,
    SuggestionKind,
    SuggestionStatus,
    TriageOutcome,
    Verdict,
    VerifyPhase,
    YieldOutcome,
)
from tightrein.store import idempotency
from tightrein.store.files import handoff_files
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.repos import handoffs, issues, knowledge, pulls, stage_yield, suggestions, triage
from tightrein.store.repos.stage_yield import StageYieldRecord
from tightrein.store.repos.triage import TriageRecord

ROLE_SUFFIX = re.compile(r"-(\d+|[a-z]{2}-\d+)$")
CONFIRMED = (Verdict.CONFIRMED, Verdict.CONDITIONAL)
SCREENSHOT = "screenshot"
LESSON_KEY = "lesson:"
RULE_KEY = "rule:"


@dataclass(frozen=True)
class Judgement:
    outcome: YieldOutcome
    reason: str


PENDING = Judgement(YieldOutcome.PENDING, "")


def useful(reason: str) -> Judgement:
    return Judgement(YieldOutcome.USEFUL, reason)


def no_yield(reason: str) -> Judgement:
    return Judgement(YieldOutcome.NO_YIELD, reason)


@dataclass(frozen=True)
class YieldContext:
    conn: sqlite3.Connection
    layout: WorkspaceLayout
    config: ProjectConfig
    now: datetime

    def outputs(self, stage: RunStage, subject_id: str, phase: VerifyPhase | None = None) -> list[dict[str, Any]]:
        """该对象该环节全部交接文档的 outputs，按写入时间升序。"""
        records = [record for record in handoffs.for_subject(self.conn, subject_id)
                   if record.stage is stage and record.phase is phase]
        return [handoff_files.read(self.layout.root / record.path)["outputs"] for record in records]


def role_group(role: str) -> str:
    return ROLE_SUFFIX.sub("", role)


def _run_record(ctx: YieldContext, record: StageYieldRecord) -> TriageRecord | None:
    found = [item for item in triage.for_problem(ctx.conn, record.subject_id) if item.run_id == record.run_id]
    return found[-1] if found else None


# collect

def static_claims(ctx: YieldContext, record: StageYieldRecord) -> Judgement:
    """静态巡检的初筛、基线审查、全量扫描与取证：本次运行的静态信号关联的问题中有成立且未被推翻的。"""
    rows = ctx.conn.execute("SELECT s.id, ps.problem_id FROM signals s LEFT JOIN problem_signals ps "
                            "ON ps.signal_id = s.id WHERE s.run_id = ? AND s.probe = ?",
                            (record.run_id, Probe.STATIC.value)).fetchall()
    if not rows:
        return no_yield("没有产出成立的主张")
    if any(row["problem_id"] is None for row in rows):
        return PENDING
    waiting = False
    for problem_id in {row["problem_id"] for row in rows}:
        latest = triage.latest(ctx.conn, problem_id)
        if latest is None:
            waiting = True
            continue
        overridden = ctx.conn.execute("SELECT 1 FROM problem_events WHERE problem_id = ? AND event = ?",
                                      (problem_id, ProblemEvent.USER_FALSE_POSITIVE.value)).fetchone()
        if (latest.result.verdict in CONFIRMED and latest.result.outcome is not TriageOutcome.FALSE_CONFIRM
                and overridden is None):
            return useful(f"主张经取证成立：{problem_id}")
    return PENDING if waiting else no_yield("主张都没有成立或被判为误判")


# triage

def evidence(ctx: YieldContext, record: StageYieldRecord) -> Judgement:
    found = _run_record(ctx, record)
    if found is None:
        return no_yield("没有形成分诊结论")
    outcome = found.result.outcome
    if outcome is None:
        return PENDING
    if outcome is TriageOutcome.CORRECT:
        return useful("分诊结论的实际结果为判对")
    return no_yield(f"分诊结论的实际结果为{outcome.label}")


def refuter(ctx: YieldContext, record: StageYieldRecord) -> Judgement:
    found = _run_record(ctx, record)
    if found is None or found.result.refuter_verdict in (None, Verdict.REFUTED):
        return no_yield("复核没有改变判定")
    latest = triage.latest(ctx.conn, record.subject_id)
    if latest is not None and latest.result.attempt > found.result.attempt:
        if latest.result.verdict is Verdict.REFUTED:
            return no_yield("用户改判为不成立")
        return useful("复核改变了判定，之后的结论认可问题成立")
    outcome = found.result.outcome
    if outcome is None:
        return PENDING
    real = outcome is TriageOutcome.FALSE_REFUTE or (outcome is TriageOutcome.CORRECT
                                                      and found.result.verdict is not Verdict.REFUTED)
    return useful("复核改变了判定，实际结果认可问题成立") if real else no_yield(f"实际结果为{outcome.label}")


def merged(ctx: YieldContext, record: StageYieldRecord) -> Judgement:
    """查重看该问题在本次运行中是否有「并入其他问题」事件。"""
    sql = "SELECT 1 FROM problem_events WHERE event = ? AND run_id = ? AND problem_id = ?"
    if ctx.conn.execute(sql, (ProblemEvent.MERGED.value, record.run_id, record.subject_id)).fetchone() is not None:
        return useful("执行了合并")
    return no_yield("没有合并")


# fix、verify

def _fix_result(ctx: YieldContext, issue_id: str) -> Judgement:
    pull = pulls.get(ctx.conn, issue_id)
    if pull is not None and pull.merged_at is not None:
        return useful("PR 已合并")
    if pull is not None and pull.closed_at is not None:
        return no_yield("PR 关闭而未合并")
    found = issues.get(ctx.conn, issue_id)
    reason = found.issue.close_reason if found is not None else None
    if found is not None and found.issue.status is IssueStatus.CANCELLED:
        return no_yield(f"Issue 以{reason.label if reason is not None else '其他原因'}关闭")
    return PENDING


def fix_work(ctx: YieldContext, record: StageYieldRecord) -> Judgement:
    return _fix_result(ctx, record.subject_id)


def _review_failed(ctx: YieldContext, issue_id: str, mode: str) -> bool | None:
    """该模式的评审是否出现过不通过；还没有这一模式的评审记录时为 None。"""
    reviews = [review for outputs in ctx.outputs(RunStage.FIX, issue_id)
               for item in outputs.get("rounds", []) for review in item.get("reviews", []) if review["mode"] == mode]
    if not reviews:
        return None
    return any(review["passed"] is False for review in reviews)


def fix_review(ctx: YieldContext, record: StageYieldRecord) -> Judgement:
    mode = record.role.rsplit("-", 1)[-1]
    failed = _review_failed(ctx, record.subject_id, mode)
    if failed is None:
        return PENDING
    if not failed:
        return no_yield("评审没有提出不通过项")
    result = _fix_result(ctx, record.subject_id)
    if result.outcome is not YieldOutcome.USEFUL:
        return result
    releases = ctx.outputs(RunStage.RELEASE, record.subject_id)
    if releases and releases[-1].get("acceptedFindings"):
        return no_yield("发布时接受了未通过项")
    return useful("不通过项被修正，PR 已合并")


def screenshot_review(ctx: YieldContext, record: StageYieldRecord) -> Judgement:
    documents = ctx.outputs(RunStage.VERIFY, record.subject_id, VerifyPhase.LOCAL)
    shots = [item for outputs in documents for item in outputs["items"] if item["category"] == SCREENSHOT]
    if not shots:
        return PENDING
    if not any(item["result"] == CheckResult.FAIL.value for item in shots):
        return no_yield("截图没有发现问题")
    return useful("指出的问题已修正") if documents[-1]["conclusion"] == "passed" else PENDING


# learn

def lesson(ctx: YieldContext, record: StageYieldRecord) -> Judgement:
    rows = ctx.conn.execute("SELECT key, result, completed_at FROM idempotency_keys WHERE key LIKE ? AND status = ?",
                            (f"{LESSON_KEY}%:{record.subject_id}:%", idempotency.DONE)).fetchall()
    if not rows:
        return PENDING
    written = [json.loads(row["result"]).get("knowledgeId") for row in rows if row["result"]]
    entries = [knowledge.get(ctx.conn, entry_id) for entry_id in written if entry_id]
    entries = [entry for entry in entries if entry is not None]
    if not entries:
        return no_yield("没有写入经验条目")
    if any(entry.hits > 0 for entry in entries):
        return useful("写入的经验条目之后被命中")
    unused = timedelta(days=ctx.config.whole_threshold("retrieval.unusedDays"))
    return no_yield("写入的经验条目一直没有被命中") if ctx.now - record.created_at > unused else PENDING


def _suggestion_result(found: Iterable[Any]) -> Judgement:
    statuses = [item.status for item in found]
    if SuggestionStatus.ACCEPTED in statuses:
        return useful("产出的建议被接受")
    if SuggestionStatus.PENDING in statuses:
        return PENDING
    return no_yield("没有被接受的建议" if statuses else "没有产出建议")


def rule(ctx: YieldContext, record: StageYieldRecord) -> Judgement:
    found = idempotency.get(ctx.conn, f"{RULE_KEY}{record.subject_id}")
    if found is None or found.status != idempotency.DONE or not found.result:
        return PENDING
    return useful("规则收入规则库") if found.result.get("accepted") else no_yield("规则没有收入规则库")


def improvement(ctx: YieldContext, record: StageYieldRecord) -> Judgement:
    return _suggestion_result(item for item in suggestions.find(ctx.conn, kind=SuggestionKind.IMPROVEMENT)
                              if item.evidence.get("runId") == record.run_id)


def comparison(ctx: YieldContext, record: StageYieldRecord) -> Judgement:
    return _suggestion_result(item for item in suggestions.find(ctx.conn, kind=SuggestionKind.KNOWLEDGE_REVIEW)
                              if item.evidence.get("runId") == record.run_id and item.evidence.get("compared"))


Rule = Callable[[YieldContext, StageYieldRecord], Judgement]

# (环节、角色前缀、判定)；同一环节中较长的前缀写在前面
RULES: tuple[tuple[Stage, str, Rule], ...] = (
    (Stage.COLLECT, "static-review", static_claims),
    (Stage.COLLECT, "baseline-review", static_claims),
    (Stage.COLLECT, "variant-scan", static_claims),
    (Stage.COLLECT, "claim-verifier", static_claims),
    (Stage.TRIAGE, "claim-verifier", evidence),
    (Stage.TRIAGE, "refuter", refuter),
    (Stage.TRIAGE, "triage-dedup", merged),
    (Stage.FIX, "fix-scout", fix_work),
    (Stage.FIX, "fix-planner", fix_work),
    (Stage.FIX, "frontend-designer", fix_work),
    (Stage.FIX, "fix-executor", fix_work),
    (Stage.FIX, "repro-writer", fix_work),
    (Stage.FIX, "fix-session", fix_work),
    (Stage.FIX, "fix-reviewer-", fix_review),
    (Stage.VERIFY, "fix-reviewer-screenshot", screenshot_review),
    (Stage.LEARN, "lesson-writer-compare", comparison),
    (Stage.LEARN, "lesson-writer", lesson),
    (Stage.LEARN, "rule-writer", rule),
    (Stage.LEARN, "improvement-writer", improvement),
)


def judge(ctx: YieldContext, record: StageYieldRecord) -> Judgement:
    if record.runner_status is not RunnerStatus.OK:
        return no_yield(f"执行器没有返回结果：{record.runner_status.value}")
    for stage, prefix, rule in RULES:
        if record.stage is stage and record.role.startswith(prefix):
            return rule(ctx, record)
    return PENDING


def backfill(ctx: YieldContext) -> int:
    """返回本次确定了结果的记录数。"""
    decided = 0
    for record in stage_yield.find(ctx.conn, outcome=YieldOutcome.PENDING):
        result = judge(ctx, record)
        if result.outcome is not YieldOutcome.PENDING and record.id is not None:
            stage_yield.decide(ctx.conn, record.id, result.outcome, result.reason, ctx.now)
            decided += 1
    return decided


@dataclass(frozen=True)
class YieldSummary:
    stage: Stage
    role: str
    calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    useful: int
    no_yield: int
    pending: int
    calls_since_useful: int

    @property
    def tokens_per_useful(self) -> float | None:
        return (self.input_tokens + self.output_tokens) / self.useful if self.useful else None

    def to_dict(self) -> dict[str, Any]:
        return {"stage": self.stage.value, "role": self.role, "calls": self.calls, "inputTokens": self.input_tokens,
                "outputTokens": self.output_tokens, "costUsd": round(self.cost_usd, 4), "useful": self.useful,
                "noYield": self.no_yield, "pending": self.pending, "tokensPerUseful": self.tokens_per_useful,
                "callsSinceUseful": self.calls_since_useful}


def summarize(conn: sqlite3.Connection, since: datetime) -> list[YieldSummary]:
    """since 之后登记的调用按(环节、角色组)汇总；「最近一次有效产出之后的调用次数」按全部历史记录计算。"""
    groups: dict[tuple[Stage, str], list[StageYieldRecord]] = {}
    for record in stage_yield.find(conn):
        groups.setdefault((record.stage, role_group(record.role)), []).append(record)
    found = []
    for (stage, role), records in sorted(groups.items(), key=lambda item: (item[0][0].value, item[0][1])):
        recent = [record for record in records if record.created_at >= since]
        if not recent:
            continue
        last_useful = max((index for index, record in enumerate(records) if record.outcome is YieldOutcome.USEFUL),
                          default=-1)

        def count(outcome: YieldOutcome) -> int:
            return sum(record.outcome is outcome for record in recent)

        found.append(YieldSummary(
            stage, role, len(recent), sum(record.input_tokens or 0 for record in recent),
            sum(record.output_tokens or 0 for record in recent), sum(record.cost_usd or 0.0 for record in recent),
            count(YieldOutcome.USEFUL), count(YieldOutcome.NO_YIELD), count(YieldOutcome.PENDING),
            len(records) - last_useful - 1))
    return found
