"""learn 各子命令背后的函数(architecture/08 第 3 节，redesign/08-learn.md)。

- report：回填分诊结论与有效产出 → 计算指标并写快照 → 经验自动清理 → 学习建议(采集配置、覆盖缺口、知识复核、
  控制措施) → 过期建议 → 需要处理的事项 → 健康检查 → 第三方 skill 核实 → JSON 交接文档 → 周报(result 类型的交接文档
  data/reports/weekly-<周一日期>.md) → 本机通知；
- metrics：只计算并返回指标，不写快照、不写交接文档；
- health：链路健康检查，交接文档中标出需要立即通知的项，由编排合并进本次运行的通知；
- lessons：回填分诊结论与有效产出，出问题时写经验，缺陷变规则(编排在每次运行结束时执行)；
- curate：单独运行经验清理与知识库复核；suggestions、accept、reject：列出与处理学习建议(接受只记录决定)。
--output 模式不写数据库与知识，交接文档与周报写到输出目录。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any

from tightrein.config.project import ProjectConfig
from tightrein.domain.clock import Clock, local_date
from tightrein.domain.enums import HandoffStatus, RunStage, Stage, SuggestionKind, SuggestionStatus
from tightrein.observability.events import EventLog
from tightrein.observability.notify import Notifier, NotifyResult
from tightrein.pipeline.common import stage_runs
from tightrein.pipeline.learn.prompts.common import LearnCalls, LearnEnv, LearnPrompt
from tightrein.pipeline.learn.render import decision, weekly
from tightrein.pipeline.learn.steps import (
    attention,
    controls,
    curate,
    health,
    lessons,
    metrics,
    outcomes,
    rules,
    suggestions,
    third_party,
    weeks,
    yields,
)
from tightrein.pipeline.learn.steps.rules import RuleEnv
from tightrein.pipeline.learn.steps.suggestions import Draft
from tightrein.pipeline.learn.steps.health import HealthContext
from tightrein.pipeline.learn.steps.metrics import MetricContext, MetricValue
from tightrein.pipeline.learn.steps.third_party import RepoQuery
from tightrein.pipeline.learn.steps.weeks import Window
from tightrein.pipeline.learn.steps.yields import YieldContext
from tightrein.runner.roles import Overrides
from tightrein.pipeline.common.stage_runs import StageRun
from tightrein.retrieval.service import KnowledgeService
from tightrein.runner.service import Runner
from tightrein.store import locks
from tightrein.store.files import documents
from tightrein.store.files.layout import ToolLayout, WorkspaceLayout
from tightrein.store.repos import suggestions as suggestion_repo
from tightrein.store.repos.suggestions import SuggestionRecord

STAGE = RunStage.LEARN
WEEK_SUBJECT = "week"
RUN_SUBJECT = "run"
NOTIFY_EVENT = "learn-weekly"


@dataclass
class LearnDeps:
    layout: WorkspaceLayout
    tool: ToolLayout
    config: ProjectConfig
    conn: sqlite3.Connection
    clock: Clock
    events: EventLog
    runner: Runner
    knowledge: KnowledgeService
    notifier: Notifier | None = None
    repo_query: RepoQuery | None = None
    pid_alive: Callable[[int], bool] = locks.process_alive
    zone: tzinfo | None = None
    overrides: Overrides = field(default_factory=Overrides)
    rules: RuleEnv | None = None


@dataclass(frozen=True)
class LearnResult:
    run_id: str
    handoff: Path
    outputs: Mapping[str, Any]
    report: Path | None = None
    notification: NotifyResult | None = None


class LearnService:
    def __init__(self, deps: LearnDeps) -> None:
        self.deps = deps

    @property
    def writable(self) -> bool:
        return self.deps.layout.output_dir is None

    def _env(self, run_id: str) -> LearnEnv:
        deps = self.deps
        prompt = LearnPrompt(deps.tool, deps.config, run_id, deps.layout.knowledge_dir())
        calls = LearnCalls(deps.runner, deps.clock, prompt, deps.overrides, deps.conn if self.writable else None)
        return LearnEnv(deps.conn, deps.layout, deps.config, deps.clock, deps.zone, calls, deps.knowledge)

    def _metric_context(self, window: Window) -> MetricContext:
        deps = self.deps
        return MetricContext(deps.conn, deps.layout, deps.config, window, deps.clock.now(), deps.zone)

    def _begin(self) -> StageRun:
        deps = self.deps
        return stage_runs.begin(STAGE, deps.layout, deps.conn, deps.clock, deps.events)

    def _backfill(self) -> list[dict[str, Any]]:
        """回填分诊结论(判为误报且之后不再出现)与环节效益的有效产出，返回回填的分诊结论。"""
        deps = self.deps
        if not self.writable:
            return []
        filled = outcomes.backfill(deps.conn, deps.config, deps.clock.now())
        yields.backfill(YieldContext(deps.conn, deps.layout, deps.config, deps.clock.now()))
        return [{"problemId": problem_id, "attempt": attempt, "outcome": "correct"} for problem_id, attempt in filled]

    def _finish(self, run: StageRun, subject: str, subject_type: str, outputs: Mapping[str, Any],
                next_action: str) -> Path:
        path = run.handoff(STAGE, subject, HandoffStatus.OK, outputs, next_action, subject_type=subject_type)
        run.end(HandoffStatus.OK)
        return path

    # 指标

    def metrics(self, start: datetime | None = None, end: datetime | None = None,
                stage: Stage | None = None) -> tuple[list[MetricValue], list[dict[str, str]]]:
        """start、end 为空时取本周；只计算，不写快照。"""
        deps = self.deps
        current = weeks.week_of(local_date(deps.clock.now(), deps.zone), deps.zone)
        window = Window(start or current.start, end or current.end)
        return metrics.compute(self._metric_context(window), stage)

    # 周报

    def _trends(self, week: date, values: list[MetricValue]) -> dict[tuple[str, str], list[float | None]]:
        """每项指标此前各周的值(旧到新，缺快照的周为空)，共 thresholds.learn.trendWeeks - 1 周。"""
        count = self.deps.config.whole_threshold("learn.trendWeeks") - 1
        found: dict[tuple[str, str], list[float | None]] = {}
        for value in values:
            history = {item.week: item.value for item in metrics.trend(
                self.deps.conn, value.metric, value.dimension, week - timedelta(weeks=1), count)}
            found[(value.metric, value.dimension)] = [
                history.get(week - timedelta(weeks=offset)) for offset in range(count, 0, -1)]
        return found

    def _suggestion_drafts(self, env: LearnEnv, ctx: MetricContext, monday: date,
                           values: list[MetricValue]) -> tuple[list[Draft], list[dict[str, str]]]:
        review, review_errors = curate.review_drafts(env)
        drafts = [*suggestions.noisy_checks(ctx), *suggestions.coverage_gaps(ctx), *review,
                  *controls.drafts(ctx, monday, values)]
        return drafts, review_errors

    def _document(self, suggestion_id: str, draft: Draft) -> Path:
        deps = self.deps
        if draft.document is None:
            raise ValueError(f"建议 {suggestion_id} 没有决定文档的内容")
        path = decision.write(deps.layout, suggestion_id, draft.subject, draft.document, deps.clock.now(),
                              deps.config.language, deps.zone)
        return Path(deps.layout.relative(path))

    @staticmethod
    def pending_items(found: list[SuggestionRecord]) -> list[dict[str, Any]]:
        return [{"id": item.id, "kind": item.kind.value, "subject": item.subject, "status": item.status.value,
                 "document": item.target_path, **({"advice": item.evidence["advice"]} if "advice" in item.evidence
                                                   else {})} for item in found]

    def report(self, week: date | None = None) -> LearnResult:
        deps = self.deps
        now = deps.clock.now()
        window = weeks.week_of(week or local_date(now, deps.zone), deps.zone)
        monday = weeks.week_start(window, deps.zone)
        run = self._begin()
        env = self._env(run.id)
        ctx = self._metric_context(window)
        filled = self._backfill()
        values, errors = metrics.compute(ctx)
        if self.writable:
            metrics.save_snapshots(deps.conn, monday, values, now)
        cleaned = curate.cleanup(env)
        drafts, draft_errors = self._suggestion_drafts(env, ctx, monday, values)
        if self.writable:
            suggestions.store(deps.conn, deps.clock, drafts, self._document)
            suggestions.expire(deps.conn, now, deps.config.whole_threshold("learn.suggestionExpiryWeeks"))
        pending = suggestion_repo.find(deps.conn, status=SuggestionStatus.PENDING)
        items = attention.collect(ctx)
        checks = health.check(HealthContext(deps.conn, deps.layout, deps.config, now, deps.zone, run.id,
                                            deps.pid_alive))
        third = [] if deps.repo_query is None else third_party.check(
            deps.conn, deps.clock, deps.config, deps.tool.third_party_lock(), deps.repo_query, deps.zone,
            record=self.writable)
        outputs = {
            "metrics": [value.to_dict() for value in values],
            "attention": [item.to_dict() for item in items],
            "suggestions": self.pending_items(pending),
            "health": [item.to_dict() for item in checks],
            "yields": [item.to_dict() for item in yields.summarize(deps.conn, window.start)],
            "thirdParty": [item.to_dict() for item in third],
            "rules": rules.recent(deps.conn, window.start, window.end),
            "cleanup": cleaned,
            "outcomes": filled,
            "errors": [*errors, *draft_errors],
        }
        path = deps.layout.weekly_report(monday) if self.writable else \
            deps.layout.output_path(f"weekly-{monday.isoformat()}.md")
        handoff = self._finish(run, monday.isoformat(), WEEK_SUBJECT, outputs,
                               f"阅读周报 {path.name}，处理需要决定的建议与需要处理的事项")
        trends = self._trends(monday, values)
        base = deps.layout.root if self.writable else deps.layout.output_path()
        document = weekly.build(outputs, monday, window, deps.zone, trends, now, handoff.relative_to(base).as_posix())
        documents.write(path, document, deps.config.language, deps.zone)
        notification = None
        if self.writable and deps.notifier is not None:
            previous = {key: history[-1] if history else None for key, history in trends.items()}
            notification = deps.notifier.notify(NOTIFY_EVENT, monday.isoformat(), weekly.summary(outputs, previous))
        return LearnResult(run.id, handoff, outputs, path, notification)

    # 每次运行结束

    def health(self) -> LearnResult:
        deps = self.deps
        run = self._begin()
        checks = health.check(HealthContext(deps.conn, deps.layout, deps.config, deps.clock.now(), deps.zone, run.id,
                                            deps.pid_alive))
        outputs = {"health": [item.to_dict() for item in checks]}
        urgent = [item for item in checks if item.notify]
        next_action = "把标为立即通知的项合并进本次运行的通知" if urgent else "无需处理"
        return LearnResult(run.id, self._finish(run, run.id, RUN_SUBJECT, outputs, next_action), outputs)

    def lessons(self) -> LearnResult:
        deps = self.deps
        run = self._begin()
        env = self._env(run.id)
        filled = self._backfill()
        report = lessons.write_lessons(env)
        made = rules.generate(env, deps.rules)
        outputs = {"lessons": [item.to_dict() for item in report.results],
                   "attention": [item.to_dict() for item in report.attention], "rules": made.results,
                   "outcomes": filled, "errors": [*report.errors, *made.errors]}
        return LearnResult(run.id, self._finish(run, run.id, RUN_SUBJECT, outputs, "无需处理"), outputs)

    def curate(self) -> LearnResult:
        deps = self.deps
        run = self._begin()
        env = self._env(run.id)
        cleaned = curate.cleanup(env)
        drafts, errors = curate.review_drafts(env)
        created = suggestions.store(deps.conn, deps.clock, drafts) if self.writable else []
        outputs = {"suggestions": self.pending_items(created), "cleanup": cleaned, "errors": errors}
        next_action = "在收件箱或 learn suggestions 中处理复核建议"
        return LearnResult(run.id, self._finish(run, run.id, RUN_SUBJECT, outputs, next_action), outputs)

    # 建议

    def suggestions(self, status: SuggestionStatus | None = None,
                    kind: SuggestionKind | None = None) -> list[SuggestionRecord]:
        return suggestion_repo.find(self.deps.conn, status=status, kind=kind)

    def accept(self, suggestion_id: str, action: str | None = None) -> SuggestionRecord:
        """只记录用户的决定；知识复核按 action 改条目状态。控制措施与改进建议由用户按决定文档自己修改。"""
        run = self._begin()
        try:
            return suggestions.accept(self._env(run.id), suggestion_id, action)
        finally:
            run.end(HandoffStatus.OK)

    def reject(self, suggestion_id: str, reason: str) -> SuggestionRecord:
        root = self.deps.layout.root if self.writable else None
        return suggestions.reject(self.deps.conn, self.deps.clock, suggestion_id, reason, root)
