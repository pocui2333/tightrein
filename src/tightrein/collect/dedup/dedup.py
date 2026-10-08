"""去重的流程(44c `dedup(runtime, results) -> DedupOutcome`)：规范化 → 抑制 → 归并 → 复现确认 → 状态更新 → 输出。

- 信号按 (发生时间, 编号) 排序后处理，结果与各来源交出的顺序无关，可重复；
- 各步只改内存中的变更集(ChangeSet)，最后在一个事务里写入：问题、出现、各来源的读取位置(SourceResult.state)、
  问题序列、关联 Issue 的重新打开、要落盘的文件；任一处失败整体回滚，读取位置不前进，下次重读；
- 要用的问题按指纹、位置、状态各一次批量读入，不在循环里逐条查库；commit 先后关系汇总后一次查完；
- 状态转换不在表中时抛 TransitionRejected，本次什么都不写；
- 不调用模型。
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from tightrein.assess.issue import github
from tightrein.assess.issue.transitions import Effect, apply_event, effects_for, regression_event
from tightrein.collect.common.signals import Signal, deployments, latest_release
from tightrein.collect.common.source import SourceResult, SourceStatus
from tightrein.collect.dedup import group, normalize, output, reproduce, status, suppress
from tightrein.collect.dedup.changes import ChangeSet, load_by_fingerprints, load_where
from tightrein.collect.dedup.status import Coverage, ProblemStatus, latest_commit
from tightrein.protocol.handoff import Metrics
from tightrein.protocol.naming import parse_duration
from tightrein.store.db import transaction
from tightrein.store.tables import issues, occurrences, problems, sequences, state
from tightrein.store.tables.issues import Issue
from tightrein.store.tables.problems import Problem

if TYPE_CHECKING:
    from tightrein.protocol.runtime import Runtime

POINT = output.POINT
ON_COMMIT = "on_commit"  # schedule.every 中按提交触发的来源：覆盖范围对应仓库的 HEAD，其余对应最近一次成功部署
COVERING = frozenset({SourceStatus.DONE, SourceStatus.PARTIAL})
MILLISECONDS_PER_SECOND = 1000


@dataclass
class DedupOutcome:
    new: list[str]
    regressed: list[str]
    merged: int
    muted: int  # 命中抑制规则的信号数
    resolved: int
    watching: int
    metrics: Metrics
    reopened: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Params:
    normalize: tuple[normalize.Rule, ...]
    suppress: list[Mapping[str, Any]]
    group: group.Params
    reproduce: reproduce.Params
    status: status.Params

    @classmethod
    def from_settings(cls, runtime: Runtime) -> Params:
        section = runtime.settings.section(POINT)
        watch_occurrences = int(section["watchOccurrences"])
        return cls(
            normalize=normalize.rules_from(section.get("normalize") or []),
            suppress=list(section.get("suppress") or []),
            group=group.Params(title_length=int(section["titleLength"]), nearby_lines=int(section["nearbyLines"])),
            reproduce=reproduce.Params(
                replay_checks={source: frozenset(checks) for source, checks in section["replayChecks"].items()},
                attempts=int(section["replayAttempts"]), watch_occurrences=watch_occurrences,
            ),
            status=status.Params(
                covered_runs=dict(section["coveredRuns"]),
                watch_window=timedelta(seconds=parse_duration(section["watchWindow"])),
                watch_occurrences=watch_occurrences,
                false_positive_clean_runs=int(section["falsePositiveCleanRuns"]),
            ),
        )


def dedup(runtime: Runtime, results: list[SourceResult], *,
          monotonic: Callable[[], float] = time.monotonic) -> DedupOutcome:
    started = monotonic()
    conn, clock = runtime.conn, runtime.clock
    recover(runtime)
    params = Params.from_settings(runtime)
    now = clock.now()
    signals = sorted((signal for result in results for signal in result.signals),
                     key=lambda signal: (signal.occurred_at, signal.id))
    messages = {signal.id: normalize.message(signal.message, params.normalize) for signal in signals}
    prints = {signal.id: None if group.targets_of(signal) else group.fingerprint(signal, messages[signal.id])
              for signal in signals}
    rules = suppress.load(conn, params.suppress)
    kept, suppressed = suppress.apply(signals, rules, now, lambda signal: prints[signal.id])

    changeset = ChangeSet.start(conn, runtime.run, now)
    coverages = _coverages(runtime, results)
    pending = load_where(conn, "status", [ProblemStatus.PENDING.value])
    changeset.load(load_by_fingerprints(conn, _wanted_fingerprints(kept, prints)))
    changeset.load(load_where(conn, "location", group.code_paths(kept)))
    changeset.load(_candidates(conn, coverages))
    changeset.load(pending)

    group.apply(changeset, kept, messages, params.group)
    unseen = [problem.id for problem in pending if problem.id not in changeset.occurred]
    latest = {problem_id: records[0] for problem_id, records in output.recent_records(conn, unseen).items()}
    reproduce.apply(changeset, pending, latest, _replayers(runtime), params.reproduce)
    work = status.plan(changeset, coverages)
    facts = status.query_facts(work.pairs, runtime.git.is_ancestor)
    status.apply(changeset, conn, work, facts, params.status)
    refuted = status.refuted_problems(conn)
    changeset.load(refuted)
    confirmed = status.false_positive_outcomes(changeset, refuted, (value for value in prints.values() if value),
                                               coverages, params.status)
    changeset.notes += [f"{problem_id}：判为不成立后多次覆盖未再出现，评估结论回填为判对" for problem_id in confirmed]

    sent = [problem.id for problem, _ in output.for_assess(changeset)]
    stored = output.recent_records(conn, sent)
    summary = _write(runtime, changeset, results, suppressed, len(signals), stored, started, monotonic)
    return DedupOutcome(
        new=summary.new, regressed=summary.regressed, merged=summary.merged, muted=suppressed,
        resolved=len(summary.resolved), watching=len(summary.watching),
        metrics=_metrics(summary, started, monotonic), reopened=summary.reopened, notes=summary.notes,
    )


def recover(runtime: Runtime) -> str | None:
    """上次去重的事务已提交、文件没写完就中断时补写；返回补写的运行编号。"""
    found = output.flush(runtime.workspace, runtime.conn)
    if found is not None:
        runtime.events.emit(run=runtime.run, subject=found, point=POINT, kind="action",
                            summary=f"补写上次中断的去重运行 {found} 的交接文档")
    return found


def running_commit(runtime: Runtime, source: str) -> str | None:
    """一次来源运行所对应的版本：按提交触发的来源为仓库 HEAD，其余为最近一次成功部署的 commit。"""
    if runtime.settings.get("schedule.every").get(source) == ON_COMMIT:
        return runtime.git.head().commit
    return latest_release(deployments(runtime.conn))


def _write(runtime: Runtime, changeset: ChangeSet, results: Sequence[SourceResult], suppressed: int, signals: int,
           stored: Mapping[str, list[dict[str, Any]]], started: float,
           monotonic: Callable[[], float]) -> output.Summary:
    conn, clock = runtime.conn, runtime.clock
    now = clock.now()
    with transaction(conn):
        sequences.ensure_at_least(conn, sequences.PROBLEM, changeset.next_number - 1)
        for problem_id in sorted(changeset.touched):
            problems.TABLE.save(conn, changeset.known[problem_id], now)
        for occurrence in changeset.occurrences:
            occurrences.TABLE.insert(conn, occurrence, now)
        for result in results:
            for key, value in result.state.items():
                state.put(conn, key, value, clock)
        reopened = _reopen(runtime, changeset)
        summary = output.summarize(changeset, signals, suppressed, [after.id for _, after in reopened])
        written = output.files(runtime.workspace, changeset, summary, _metrics(summary, started, monotonic),
                               stored, clock)
        output.remember(conn, changeset.run, written, clock)
    output.flush(runtime.workspace, conn)
    _sync_github(runtime, reopened)
    for _, after in reopened:
        runtime.events.emit(run=runtime.run, subject=after.id, point=POINT, kind="effect",
                            summary=f"关联问题回归，重新打开 Issue {after.id}")
    return summary


def _reopen(runtime: Runtime, changeset: ChangeSet) -> list[tuple[Issue, Issue]]:
    """关联了 Issue 的问题回归时，按评估的 Issue 状态机重新打开(历史、正文、记录文件与索引由 apply_event 写，
    嵌在去重的事务里)；Issue 还在修(状态机给不出事件)时只记说明。返回 (转换前, 转换后)，GitHub 在事务提交后同步。"""
    reopened = []
    for issue_id, problem_id in changeset.reopened:
        before = issues.get(runtime.conn, issue_id)
        if before is None:
            changeset.notes.append(f"{problem_id} 关联的 Issue {issue_id} 不存在，没有重新打开")
            continue
        event = regression_event(before)
        if event is None:
            changeset.notes.append(f"{problem_id} 回归：Issue {issue_id} 当前为 {before.status}，还在处理中")
            continue
        commit = latest_commit(changeset, problem_id)
        signals = "、".join(signal.id for signal in changeset.occurred.get(problem_id, [])) or "无"
        after = apply_event(runtime, issue_id, event, actor=POINT, sync_github=False,
                            note=f"关联问题 {problem_id} 回归(commit {commit or '未知'}，信号 {signals})")
        reopened.append((before, after))
    return reopened


def _sync_github(runtime: Runtime, reopened: Sequence[tuple[Issue, Issue]]) -> None:
    for before, after in reopened:
        event = regression_event(before)
        if event is not None and Effect.GITHUB in effects_for(before, event, None):
            github.sync(runtime, after)


def _coverages(runtime: Runtime, results: Iterable[SourceResult]) -> dict[str, Coverage]:
    return {result.source: Coverage.from_items(result.coverage, running_commit(runtime, result.source))
            for result in results if result.status in COVERING and result.coverage}


def _wanted_fingerprints(signals: Iterable[Signal], prints: Mapping[str, str | None]) -> set[str]:
    wanted: set[str] = set()
    for signal in signals:
        found = prints[signal.id]
        wanted.update([found] if found is not None else group.targets_of(signal))
    return wanted


def _candidates(conn: sqlite3.Connection, coverages: Mapping[str, Coverage]) -> list[Problem]:
    """没出现也要判定的问题：本次有覆盖范围的来源下待评估与仍出现的(覆盖后解决)，与全部被忽略的(按日期到期)。"""
    sources = sorted(coverages)
    statuses = sorted(item.value for item in status.RESOLVABLE)
    rows = conn.execute(
        f"SELECT * FROM problems WHERE status = ? OR (status IN ({', '.join('?' for _ in statuses)}) "
        f"AND source IN ({', '.join('?' for _ in sources) or 'NULL'})) ORDER BY id",
        [ProblemStatus.MUTED.value, *statuses, *sources],
    ).fetchall()
    return [problems.TABLE.from_row(row) for row in rows]


def _replayers(runtime: Runtime) -> Callable[[str], reproduce.Replayer | None]:
    cache: dict[str, reproduce.Replayer | None] = {}

    def bind(replay: Callable[..., str]) -> reproduce.Replayer:
        return lambda evidence: replay(runtime, evidence)

    def find(source: str) -> reproduce.Replayer | None:
        if source not in cache:
            cache[source] = reproduce.replayer_for(source, bind)
        return cache[source]

    return find


def _metrics(summary: output.Summary, started: float, monotonic: Callable[[], float]) -> Metrics:
    return Metrics(
        duration_ms=round((monotonic() - started) * MILLISECONDS_PER_SECOND),
        produced={"signals": summary.signals, "suppressed": summary.suppressed, "created": len(summary.created),
                  "accumulated": summary.accumulated, "merged": summary.merged, "new": len(summary.new),
                  "regressed": len(summary.regressed), "resolved": len(summary.resolved),
                  "watching": len(summary.watching)},
    )
