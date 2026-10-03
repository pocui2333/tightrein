"""8.3 的指标(architecture/08 4.2)：每项一个函数，输入为 MetricContext，输出为 MetricValue 列表。

- 状态与结果读数据库；token 与费用读 budget_usage(执行器按环节、按本机日期累计)；耗时读 runs；
- 比率保留分子与分母，分母为 0 或未知时 value 为空，周报显示「无样本」或「未知」；
- 维度为 all 或 `键=值`；计数类指标的 value 即计数，没有分子分母；
- 某项出错时记入 errors，其余照常计算。
"""

from __future__ import annotations

import json
import sqlite3
import statistics
from collections.abc import Callable, Iterable, Sequence
from datetime import date, datetime, timedelta
from typing import Any

from tightrein.config.project import ProjectConfig
from tightrein.guards import diff_rules
from tightrein.guards.diff_rules import ChangedFile
from tightrein.guards.policy import GuardSettings
from tightrein.domain.clock import local_date, parse_iso
from tightrein.domain.enums import (
    Disposition,
    IssueEvent,
    Probe,
    ProblemStatus,
    RunStage,
    RunStatus,
    SignalAggregateState,
    Stage,
    TriageOutcome,
    VerifyPhase,
)
from tightrein.domain.run import Run
from tightrein.pipeline.learn.steps import fix_metrics
from tightrein.pipeline.learn.steps.metric_base import MetricContext, MetricValue, dim, fixed_closes
from tightrein.pipeline.learn.steps.weeks import workdays
from tightrein.sources.api_fuzz.spec import exclude_regex, selected_operations
from tightrein.store.files import handoff_files
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.repos import handoffs, issues, metric_snapshots, pulls, runs, triage
from tightrein.store.repos.handoffs import HandoffRecord
from tightrein.store.repos.metric_snapshots import OVERALL, MetricSnapshot

SECONDS_PER_HOUR = 3600
SECONDS_PER_MINUTE = 60
UNTRIAGED = "untriaged"
GET = "GET"
WRITE = "write"
PASSED = "passed"
SEGMENTS = ("seen-to-issue", "issue-to-approve", "approve-to-pr", "pr-to-merge", "merge-to-fixed")


def _counts(metric: str, keys: Iterable[str]) -> list[MetricValue]:
    tally: dict[str, int] = {}
    for key in keys:
        tally[key] = tally.get(key, 0) + 1
    return [MetricValue.count(metric, key, count) for key, count in sorted(tally.items())]


def _runs_in(ctx: MetricContext, probe: Probe) -> list[Run]:
    return [run for run in runs.find(ctx.conn, stage=RunStage.COLLECT, probe=probe, status=RunStatus.OK)
            if ctx.window.contains(run.started_at)]


def _document(ctx: MetricContext, record: HandoffRecord) -> dict[str, Any]:
    return handoff_files.read(ctx.layout.root / record.path)


def _handoffs_in(ctx: MetricContext, stage: RunStage, phase: VerifyPhase | None = None) -> list[HandoffRecord]:
    found = handoffs.TABLE.find(ctx.conn, stage=stage, phase=phase)
    return [record for record in found if ctx.window.contains(record.created_at)]


def _median(values: Sequence[float]) -> float | None:
    return statistics.median(values) if values else None


# 发现

def new_problems(ctx: MetricContext) -> list[MetricValue]:
    rows = ctx.conn.execute(
        "SELECT DISTINCT e.problem_id, p.probe FROM problem_events e JOIN problems p ON p.id = e.problem_id "
        "WHERE e.to_status = ? AND e.at >= ? AND e.at < ?", (ProblemStatus.NEW.value, *ctx.between())).fetchall()
    severities = []
    for row in rows:
        latest = triage.latest(ctx.conn, row["problem_id"])
        severity = latest.result.severity if latest is not None else None
        severities.append(dim("severity", severity.value if severity is not None else UNTRIAGED))
    metric = "new-problems"
    return [MetricValue.count(metric, OVERALL, len(rows)),
            *_counts(metric, (dim("probe", row["probe"]) for row in rows)), *_counts(metric, severities)]


def regressed_problems(ctx: MetricContext) -> list[MetricValue]:
    rows = ctx.conn.execute("SELECT DISTINCT problem_id FROM problem_events WHERE to_status = ? AND at >= ? AND at < ?",
                            (ProblemStatus.REGRESSED.value, *ctx.between())).fetchall()
    return [MetricValue.count("regressed-problems", OVERALL, len(rows))]


def spec_operations(layout: WorkspaceLayout, config: ProjectConfig, commit: str | None) -> list[tuple[str, str]] | None:
    """某 commit 缓存的接口描述中、扣除 sources.api-fuzz.exclude 后的操作；没有缓存时为空。"""
    if commit is None or not layout.openapi(commit).is_file():
        return None
    document = json.loads(layout.openapi(commit).read_text(encoding="utf-8"))
    exclude = config.data.get("sources", {}).get("api-fuzz", {}).get("exclude", ())
    return selected_operations(document, exclude_regex(exclude))


def api_coverage(ctx: MetricContext) -> list[MetricValue]:
    metric = "api-coverage"
    found = _runs_in(ctx, Probe.API_FUZZ)
    if not found:
        return [MetricValue.ratio(metric, OVERALL, 0, 0)]
    tested = frozenset().union(*(run.coverage.tested_endpoints() for run in found))
    last = found[-1]
    values = [MetricValue.ratio(metric, OVERALL, len(tested), last.coverage.endpoints_total, len(found))]
    operations = spec_operations(ctx.layout, ctx.config, last.target_commit)
    if operations is not None:
        for name, wanted in ((GET, lambda method: method == GET), (WRITE, lambda method: method != GET)):
            total = [item for item in operations if wanted(item[0])]
            covered = [item for item in tested if wanted(item[0])]
            values.append(MetricValue.ratio(metric, dim("method", name), len(covered), len(total), len(found)))
    return values


# 噪声

def noise(ctx: MetricContext) -> list[MetricValue]:
    rows = ctx.conn.execute(
        "SELECT s.id, s.probe, s.suppressed, s.aggregate_state, p.status, p.intermittent FROM signals s "
        "LEFT JOIN problem_signals ps ON ps.signal_id = s.id LEFT JOIN problems p ON p.id = ps.problem_id "
        "WHERE s.occurred_at >= ? AND s.occurred_at < ?", ctx.between()).fetchall()
    kinds: dict[str, set[str]] = {"suppressed": set(), "voided": set(), "intermittent": set()}
    probes: dict[str, set[str]] = {}
    for row in rows:
        probes.setdefault(row["probe"], set()).add(row["id"])
        if row["suppressed"]:
            kinds["suppressed"].add(row["id"])
        if row["aggregate_state"] == SignalAggregateState.VOIDED.value:
            kinds["voided"].add(row["id"])
        if row["status"] == ProblemStatus.PENDING.value and row["intermittent"]:
            kinds["intermittent"].add(row["id"])
    noisy = set().union(*kinds.values())
    total = {row["id"] for row in rows}
    values = [MetricValue.ratio("noise-ratio", OVERALL, len(noisy), len(total))]
    values += [MetricValue.ratio("noise-ratio", dim("probe", probe), len(ids & noisy), len(ids))
               for probe, ids in sorted(probes.items())]
    values += [MetricValue.count("noise-count", dim("kind", kind), len(ids)) for kind, ids in kinds.items()]
    return values


# 分诊

def triage_accuracy(ctx: MetricContext) -> list[MetricValue]:
    rows = ctx.conn.execute(
        "SELECT t.verdict, t.outcome, p.probe FROM triage_results t LEFT JOIN problems p ON p.id = t.problem_id "
        "WHERE t.outcome IS NOT NULL AND t.outcome_at >= ? AND t.outcome_at < ?", ctx.between()).fetchall()
    correct = TriageOutcome.CORRECT.value

    def ratio(dimension: str, items: list[sqlite3.Row]) -> MetricValue:
        return MetricValue.ratio("triage-accuracy", dimension, sum(row["outcome"] == correct for row in items),
                                 len(items))

    groups: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        groups.setdefault(dim("verdict", row["verdict"]), []).append(row)
        groups.setdefault(dim("probe", row["probe"]), []).append(row)
    false_refutes = sum(row["outcome"] == TriageOutcome.FALSE_REFUTE.value for row in rows)
    return [ratio(OVERALL, list(rows)), *(ratio(key, items) for key, items in sorted(groups.items())),
            MetricValue.count("false-refutes", OVERALL, false_refutes)]


def manual_queue_items(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """每个问题最新一次分诊为人工队列、且问题未解决未忽略的记录。"""
    return conn.execute(
        "SELECT t.problem_id, t.attempt, t.created_at FROM triage_results t JOIN problems p ON p.id = t.problem_id "
        "WHERE t.attempt = (SELECT MAX(attempt) FROM triage_results WHERE problem_id = t.problem_id) "
        "AND t.disposition = ? AND p.status NOT IN (?, ?) ORDER BY t.created_at",
        (Disposition.MANUAL_QUEUE.value, ProblemStatus.RESOLVED.value, ProblemStatus.IGNORED.value)).fetchall()


def manual_queue(ctx: MetricContext) -> list[MetricValue]:
    items = manual_queue_items(ctx.conn)
    waits = [workdays(parse_iso(row["created_at"]), ctx.now, ctx.config, ctx.zone) for row in items]
    return [MetricValue.count("manual-queue", OVERALL, len(items)),
            MetricValue.count("manual-queue-max-wait", OVERALL, max(waits, default=0), len(items))]


# 修复

def _first_event(conn: sqlite3.Connection, issue_id: str, event: IssueEvent) -> datetime | None:
    row = conn.execute("SELECT MIN(at) FROM issue_events WHERE issue_id = ? AND event = ?",
                       (issue_id, event.value)).fetchone()
    return parse_iso(row[0]) if row[0] else None


def lead_times(ctx: MetricContext) -> list[MetricValue]:
    durations: dict[str, list[float]] = {name: [] for name in SEGMENTS}
    for issue_id, closed_at in fixed_closes(ctx.conn, ctx.window.start, ctx.window.end).items():
        record = issues.get(ctx.conn, issue_id)
        if record is None:
            continue
        seen = [ctx.conn.execute("SELECT first_seen_at FROM problems WHERE id = ?", (problem_id,)).fetchone()
                for problem_id in record.issue.problems]
        first_seen = min((parse_iso(row[0]) for row in seen if row is not None), default=None)
        pull = pulls.get(ctx.conn, issue_id)
        points = [first_seen, record.issue.created_at, _first_event(ctx.conn, issue_id, IssueEvent.APPROVE),
                  pull.created_at if pull is not None else None, pull.merged_at if pull is not None else None,
                  closed_at]
        for name, start, end in zip(SEGMENTS, points, points[1:]):
            if start is not None and end is not None:
                durations[name].append((end - start).total_seconds() / SECONDS_PER_HOUR)
    return [MetricValue("lead-time-hours", dim("segment", name), _median(values), None, None, len(values))
            for name, values in durations.items()]


def verify_first_pass(ctx: MetricContext) -> list[MetricValue]:
    firsts = [record for record in _handoffs_in(ctx, RunStage.VERIFY, VerifyPhase.LOCAL) if record.attempt == 1]
    passed = sum(_document(ctx, record)["outputs"].get("conclusion") == PASSED for record in firsts)
    return [MetricValue.ratio("verify-first-pass", OVERALL, passed, len(firsts))]


def change_size(ctx: MetricContext) -> list[MetricValue]:
    latest: dict[str, HandoffRecord] = {}
    for record in _handoffs_in(ctx, RunStage.FIX):
        current = latest.get(record.subject_id)
        if current is None or record.attempt > current.attempt:
            latest[record.subject_id] = record
    sizes = []
    over = 0
    settings = GuardSettings.from_config(ctx.config)
    for record in latest.values():
        changed = _document(ctx, record)["outputs"].get("changedFiles")
        if changed is not None:
            sizes.append((len(changed), sum(item["added"] + item["removed"] for item in changed)))
            changes = [ChangedFile(item["path"], lines_added=item["added"], lines_removed=item["removed"])
                       for item in changed]
            over += bool(diff_rules.size_violations(changes, settings))
    files, lines = [size[0] for size in sizes], [size[1] for size in sizes]
    values = []
    for metric, items in (("change-files", files), ("change-lines", lines)):
        values.append(MetricValue(metric, dim("stat", "median"), _median(items), None, None, len(items)))
        values.append(MetricValue(metric, dim("stat", "max"), max(items, default=None), None, None, len(items)))
    return [*values, MetricValue.count("change-over-cap", OVERALL, over, len(sizes))]


# 结果

def fixed_issues(ctx: MetricContext) -> list[MetricValue]:
    return [MetricValue.count("fixed-issues", OVERALL, len(fixed_closes(ctx.conn, ctx.window.start, ctx.window.end)))]


def regression_rate(ctx: MetricContext) -> list[MetricValue]:
    days = ctx.config.whole_threshold("learn.regressionWindowDays")
    closed = fixed_closes(ctx.conn, ctx.window.end - timedelta(days=days), ctx.window.end)
    regressed = 0
    for issue_id, closed_at in closed.items():
        events = ctx.conn.execute("SELECT at FROM issue_events WHERE issue_id = ? AND event = ?",
                                  (issue_id, IssueEvent.PROBLEM_REGRESSED.value)).fetchall()
        regressed += any(parse_iso(row["at"]) > closed_at for row in events)
    return [MetricValue.ratio("regression-rate", OVERALL, regressed, len(closed))]


def pr_rejection(ctx: MetricContext) -> list[MetricValue]:
    decided = [pull for pull in pulls.find(ctx.conn) if ctx.window.contains(pull.merged_at)
               or (pull.merged_at is None and ctx.window.contains(pull.closed_at))]
    rejected = sum(pull.merged_at is None for pull in decided)
    return [MetricValue.ratio("pr-rejection-rate", OVERALL, rejected, len(decided))]


# 成本

def _local_days(ctx: MetricContext) -> tuple[date, date]:
    return local_date(ctx.window.start, ctx.zone), local_date(ctx.window.end, ctx.zone)


def cost(ctx: MetricContext) -> list[MetricValue]:
    first, end = _local_days(ctx)
    rows = ctx.conn.execute("SELECT stage, cost_usd, input_tokens, output_tokens, estimated FROM budget_usage "
                            "WHERE date >= ? AND date < ? ORDER BY stage", (first.isoformat(), end.isoformat()))
    tokens: dict[str, int] = {}
    spent: dict[str, float] = {}
    estimated: dict[str, float] = {}
    for row in rows:
        stage = row["stage"]
        tokens[stage] = tokens.get(stage, 0) + row["input_tokens"] + row["output_tokens"]
        spent[stage] = spent.get(stage, 0.0) + row["cost_usd"]
        if row["estimated"]:
            estimated[stage] = estimated.get(stage, 0.0) + row["cost_usd"]
    values = [MetricValue.count("tokens", OVERALL, sum(tokens.values())),
              MetricValue.count("cost-usd", OVERALL, round(sum(spent.values()), 4), len(spent))]
    values += [MetricValue.count("tokens", dim("stage", stage), total) for stage, total in tokens.items()]
    values += [MetricValue.count("cost-usd", dim("stage", stage), round(total, 4), 1) for stage, total in spent.items()]
    values += [MetricValue.count("cost-usd-estimated", dim("stage", stage), round(total, 4), 1)
               for stage, total in estimated.items()]
    durations: dict[str, list[float]] = {}
    for run in runs.find(ctx.conn):
        if ctx.window.contains(run.started_at) and run.ended_at is not None:
            minutes = (run.ended_at - run.started_at).total_seconds() / SECONDS_PER_MINUTE
            durations.setdefault(run.stage.value, []).append(minutes)
    for stage, items in sorted(durations.items()):
        values.append(MetricValue.count("run-minutes", dim("stage", stage), round(sum(items), 1), len(items)))
        values.append(MetricValue("run-minutes-median", dim("stage", stage), _median(items), None, None, len(items)))
    return values


MetricFunction = Callable[[MetricContext], list[MetricValue]]

# 每项指标所属的环节(learn metrics --stage 按它筛选)；成本按维度中的环节筛选
METRICS: tuple[tuple[str, Stage | None, MetricFunction], ...] = (
    ("new-problems", Stage.COLLECT, new_problems),
    ("regressed-problems", Stage.AGGREGATE, regressed_problems),
    ("api-coverage", Stage.COLLECT, api_coverage),
    ("noise", Stage.AGGREGATE, noise),
    ("triage-accuracy", Stage.TRIAGE, triage_accuracy),
    ("manual-queue", Stage.TRIAGE, manual_queue),
    ("lead-times", Stage.FIX, lead_times),
    ("verify-first-pass", Stage.VERIFY, verify_first_pass),
    ("change-size", Stage.FIX, change_size),
    ("fixed-issues", Stage.RELEASE, fixed_issues),
    ("regression-rate", Stage.RELEASE, regression_rate),
    ("pr-rejection", Stage.RELEASE, pr_rejection),
    ("first-pass", Stage.FIX, fix_metrics.first_pass),
    ("fix-cost", Stage.FIX, fix_metrics.fix_cost),
    ("revert-rate", Stage.RELEASE, fix_metrics.revert_rate),
    ("user-corrections", None, fix_metrics.user_corrections),
    ("cost", None, cost),
)
ERRORS = (sqlite3.Error, OSError, ValueError, KeyError, TypeError)


def compute(ctx: MetricContext, stage: Stage | None = None) -> tuple[list[MetricValue], list[dict[str, str]]]:
    values: list[MetricValue] = []
    errors: list[dict[str, str]] = []
    for name, owner, function in METRICS:
        if stage is not None and owner is not None and owner is not stage:
            continue
        try:
            found = function(ctx)
        except ERRORS as error:
            errors.append({"item": name, "reason": f"{type(error).__name__}: {error}"})
            continue
        if stage is not None and owner is None:
            found = [value for value in found if value.dimension == dim("stage", stage.value)]
        values += found
    return values, errors


def save_snapshots(conn: sqlite3.Connection, week: date, values: Iterable[MetricValue], at: datetime) -> None:
    """按(周、指标、维度)覆盖。"""
    for value in values:
        metric_snapshots.save(conn, MetricSnapshot(week, value.metric, value.dimension, value.sample_size, at,
                                                   value.value, value.numerator, value.denominator))


def trend(conn: sqlite3.Connection, metric: str, dimension: str, week: date, weeks: int) -> list[MetricSnapshot]:
    """截至 week(含)最近 weeks 周的快照，按周升序。"""
    since = week - timedelta(days=7 * (weeks - 1))
    return [item for item in metric_snapshots.series(conn, metric, dimension, since) if item.week <= week]
