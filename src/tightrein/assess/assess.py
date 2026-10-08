"""评估的流程：选题 → 分情况 → 查重 → 判断 → 复核 → 评级 → 去向 → 落库 → 写成 Issue。

- 取证前把只读 worktree 切到 origin/主干的最新 commit(修复要提交到主干)，切换失败就整次停下，不在旧代码上取证；
  评估期间去掉只读 worktree 的写权限(protocol/git/worktrees 的只读锁定)，结束后恢复；
- 逐个问题隔离：每个问题先拿对象锁，锁被占用就跳过；额度到了留余量的门槛就不再开始新的问题；单个问题出现程序
  异常只影响它自己(交接为 failed，带出错位置)，其余照常；同一问题连续出错到对象熔断即转人工；
- 互不相关的问题并行评估(位置或文件相同的同组按顺序，见 select.related_groups)，并行数取
  resources.concurrency.modelCalls；每个并行的问题用自己的数据库连接；
- 用户改判(override)沿用上一次的取证 commit，不调用模型；把「不成立」改判为成立时，上一次结论的实际结果回填为
  「误判为不成立」；重新评估(retriage)时用户补充的信息跨次累积。
"""

from __future__ import annotations

import time
import traceback
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tightrein.assess import (
    cases,
    claims,
    dedup,
    disposition,
    evidence,
    main_diff,
    notes,
    persist,
    rating,
    refute,
    select,
    tradeoff,
)
from tightrein.assess.attribution import Attribution, attribute
from tightrein.assess.cases import Case
from tightrein.assess.checks import Snapshot
from tightrein.assess.disposition import Decision, Destination
from tightrein.assess.evidence import Evidence
from tightrein.assess.issue import create, github
from tightrein.assess.prompts.claim_verifier import REFUTE, TRIAGE, Inputs
from tightrein.assess.prompts.common import Tally
from tightrein.assess.rating import Rating
from tightrein.protocol import boundaries
from tightrein.protocol.documents import pending
from tightrein.protocol.git import Git, GitError
from tightrein.protocol.git.worktrees import (
    ReadonlyLocked,
    create_readonly,
    lock_readonly,
    restore_readonly,
    sync_readonly,
)
from tightrein.protocol.handoff import Handoff, Status, check_facts, load_schema, write
from tightrein.protocol.naming import FileName, format_iso
from tightrein.store.db import connect, transaction
from tightrein.store.files.json import read_json, write_json
from tightrein.store.locks import Busy, FileLock
from tightrein.store.tables import occurrences, problems
from tightrein.store.tables.problems import Problem

if TYPE_CHECKING:
    from tightrein.protocol.runtime import Runtime

POINT = TRIAGE
HANDOFF = FileName(POINT, "handoff", "json")
EVIDENCE = FileName(POINT, "evidence", "json")
READONLY = "readonly"
MARKER = "readonly-assess.json"
HANDOFF_SCHEMA = Path(__file__).with_name("handoff.schema.json")
LOCKED = "正被其他运行处理"
RESERVE = "额度已到给用户留的余量，留到下一次"
new_issue = create.new_issue  # 44c 的评估入口之一(tightrein new)：用户自己提的需求直接写成待修的 Issue


class AssessBlocked(Exception):
    """只读 worktree 无法切到取证 commit：整次停下。"""


@dataclass
class AssessOutcome:
    problem: str
    verdict: str
    severity: str | None
    disposition: str  # fix_now、fix_later、watch、wont_fix
    issue: str | None
    status: Status
    destination: str | None = None  # disposition.Destination 的取值
    merged_into: str | None = None
    issues: list[str] = field(default_factory=list)
    reason: str | None = None


@dataclass
class Work:
    """一个问题在评估各步之间传递的状态。"""

    problem: Problem
    found: list[Any]
    case: Case
    claim: claims.Claim
    inputs: Inputs
    files: tuple[str, ...]
    knowledge: list[Any]
    p0: bool
    retriage: bool
    tally: Tally = field(default_factory=Tally)
    evidence: Evidence | None = None
    refuter: Evidence | None = None
    verdict: str = refute.INSUFFICIENT
    refuter_verdict: str | None = None
    needs_manual: bool = False
    rating: Rating | None = None
    attribution: Attribution = field(default_factory=Attribution)
    tradeoff_hit: str | None = None
    merged_into: str | None = None
    decision: Decision | None = None
    notes: list[str] = field(default_factory=list)


def assess(runtime: Runtime, problem: str, *, retriage: bool = False) -> AssessOutcome:
    """评估一个问题(命令行指定、重新评估时用)。"""
    selection = select.choose(runtime.conn, 1, [problem], retriage=retriage)
    if selection.rejected:
        return AssessOutcome(problem, refute.INSUFFICIENT, None, "watch", None, Status.FAILED,
                             reason=selection.rejected[0][1])
    with readonly(runtime) as (git, commit):
        return _one(runtime, selection.chosen[0], git, commit)


def assess_pending(runtime: Runtime, limit: int | None = None) -> list[AssessOutcome]:
    """评估全部待评估的问题(调度调用)：互不相关的并行。"""
    section = runtime.settings.section("assess")
    chosen = select.choose(runtime.conn, limit or int(section["perRun"])).chosen
    if not chosen:
        return []
    groups = select.related_groups(chosen)
    with readonly(runtime) as (git, commit):
        workers = min(len(groups), int(runtime.settings.get("resources.concurrency.modelCalls")))
        if workers <= 1:
            return [outcome for group in groups for outcome in _group(runtime, group, git, commit)]
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_in_thread, runtime, group, git, commit) for group in groups]
            return [outcome for future in futures for outcome in future.result()]


def retriage(runtime: Runtime, problem: str, note: str | None = None) -> AssessOutcome:
    """用户请求重新评估(可带补充信息，跨次累积)：不复用采集时的取证。"""
    found = problems.get(runtime.conn, problem)
    if found is None:
        raise LookupError(f"没有问题 {problem}")
    record = select.assessed(found)
    user_notes = [*record.get("userNotes", []), *([note] if note else [])]
    with persist.restoring(runtime, [problem]), transaction(runtime.conn):
        persist.update(runtime, found, assess={"retriage": True, "manual": False, "userNotes": user_notes})
    return assess(runtime, problem, retriage=True)


def override(runtime: Runtime, problem: str, verdict: str, reason: str, *, severity: str | None = None,
             destination: str | None = None) -> AssessOutcome:
    """用户改判：不调用模型，沿用上一次的取证 commit。"""
    found = problems.get(runtime.conn, problem)
    if found is None or found.extra.get(select.MERGED_INTO):
        raise ValueError(f"{problem} 不存在" if found is None else f"{problem} 已并入其他问题，改判请针对并入目标")
    record = select.assessed(found)
    commit = record.get("commit") or found.last_commit
    if commit is None:
        raise ValueError(f"{problem} 没有评估记录，也不知道发现时的版本，先评估一次")
    chosen = Destination(destination) if destination else _override_destination(verdict)
    previous = dict(record)
    misjudged = None
    if previous.get("verdict") == refute.REFUTED and verdict in refute.CONFIRMING and not previous.get("outcome"):
        previous["outcome"] = persist.FALSE_REFUTE
        misjudged = {"kind": persist.FALSE_REFUTE, "point": POINT,
                     "detail": f"第 {previous.get('attempt')} 次评估判为不成立，用户改判为 {verdict}：{reason}"}
    level = severity or record.get("severity")
    decision = Decision(chosen, "fix_later" if chosen is Destination.ISSUE else None)
    stored = _previous_evidence(runtime, problem) or {}
    made = create.Created()
    try:
        _override_commit(runtime, found, record, previous, verdict, level, chosen, decision, reason, commit,
                         stored, made)
    except BaseException:
        create.undo(made)
        raise
    if made.issues:
        _after_issue(runtime, made)
    outcome = AssessOutcome(problem, verdict, level, decision.disposition, made.issues[0] if made.issues else None,
                            Status.PASSED, chosen.value, issues=made.issues, reason=reason)
    _write_handoff(runtime, problem, outcome, {"override": True, "misjudged": misjudged}, Tally(), 0, None)
    return outcome


def _override_commit(runtime: Runtime, found: Problem, record: dict[str, Any], previous: dict[str, Any],
                     verdict: str, level: str | None, chosen: Destination, decision: Decision, reason: str,
                     commit: str, stored: dict[str, Any], made: create.Created) -> None:
    with persist.restoring(runtime, [found.id]), transaction(runtime.conn):
        assess_record = {**record, "attempt": int(record.get("attempt", 0)) + 1, "verdict": verdict, "severity": level, "destination": chosen.value,
                         "disposition": decision.disposition, "outcome": persist.OVERRIDDEN, "reason": reason,
                         "at": format_iso(runtime.clock.now()), "run": runtime.run, "commit": commit,
                         "previous": [*record.get("previous", []), {**_brief(record), "outcome": previous.get("outcome")}],
                         "retriage": False, "manual": False}
        found = persist.update(runtime, found, assess=assess_record)
        if chosen is not Destination.ISSUE:
            _apply_destination(runtime, found, decision, reason)
            return
        found_occurrences = occurrences.find(runtime.conn, found.id)
        claim = claims.Claim.from_json(stored["claim"]) if stored.get("claim") else claims.build(
            found, found_occurrences)
        finding = create.Finding(found, cases.latest(found_occurrences), claim,
                                 dict(stored.get("output") or {}) or {"verdict": verdict}, level, record.get("size"),
                                 record.get("taskType"), record.get("impactKind"), decision.treatment or "fix_later",
                                 (), (), commit)
        with readonly(runtime, commit) as (git, _):
            create.from_problem(runtime, finding, Snapshot(Path(git.repo)), made)
        persist.update(runtime, found, status="ongoing", issue=made.issues[0],
                       assess={"issues": list(made.issues)})


@contextmanager
def readonly(runtime: Runtime, commit: str | None = None) -> Iterator[tuple[Git, str]]:
    """只读 worktree：不存在就建，切到 commit(缺省为 origin/主干最新 commit)，去掉写权限；结束后恢复。"""
    path = runtime.workspace.worktree(READONLY)
    marker = runtime.workspace.worktrees_dir / MARKER
    try:
        if not path.exists():
            create_readonly(runtime.git, path, scope=runtime.scope(runtime.run, POINT))
        synced = sync_readonly(runtime.git, path, marker=marker, commit=commit)
    except (GitError, ReadonlyLocked) as error:
        raise AssessBlocked(f"只读 worktree 无法切换到取证 commit：{error}") from error
    lock_readonly(path, marker, runtime.clock)
    try:
        yield runtime.git.at(path), synced.commit
    finally:
        restore_readonly(marker)


def _in_thread(runtime: Runtime, group: Sequence[Problem], git: Git, commit: str) -> list[AssessOutcome]:
    """并行的一组用自己的数据库连接(sqlite 连接不跨线程)。"""
    from tightrein.protocol.limits import Breaker
    from tightrein.protocol.resources import IssueBudget, Quota

    conn = connect(runtime.workspace.database)
    try:
        agents = replace(runtime.agents, conn=conn, breaker=Breaker(conn, runtime.clock, runtime.settings),
                         quota=Quota(conn, runtime.clock, runtime.settings),
                         budget=IssueBudget(conn, runtime.clock, runtime.settings))
        local = replace(runtime, conn=conn, agents=agents)
        return _group(local, group, git, commit)
    finally:
        conn.close()


def _group(runtime: Runtime, group: Sequence[Problem], git: Git, commit: str) -> list[AssessOutcome]:
    outcomes = []
    for problem in group:
        if runtime.agents.quota.reserve_reached():
            outcomes.append(_skipped(problem, RESERVE))
            continue
        fresh = problems.get(runtime.conn, problem.id)  # 同组前一个问题可能已把它并入
        if fresh is None or not select.eligible(fresh):
            continue
        outcomes.append(_one(runtime, fresh, git, commit))
    return outcomes


def _one(runtime: Runtime, problem: Problem, git: Git, commit: str) -> AssessOutcome:
    lock = FileLock(runtime.workspace.object_lock(problem.id), runtime.clock,
                    stale_s=runtime.settings.duration("limits.lock.stale"))
    try:
        lock.acquire()
    except Busy:
        return _skipped(problem, LOCKED)
    started = time.monotonic()
    try:
        outcome, work = _examine(runtime, problem, git, commit)
        runtime.agents.breaker.object_progressed(problem.id)
        _write_handoff(runtime, problem.id, outcome, _facts(work, outcome, commit), work.tally,
                       _elapsed(started), work)
        return outcome
    except create.IssueNotCreated as error:
        return _failed(runtime, problem, str(error), started)
    except Exception as error:  # noqa: BLE001 单个问题的程序异常只影响它自己(交接为 failed，带出错位置)
        return _failed(runtime, problem, _describe(error), started)
    finally:
        lock.release()


def _examine(runtime: Runtime, problem: Problem, git: Git, commit: str) -> tuple[AssessOutcome, Work]:
    section = runtime.settings.section("assess")
    record = select.assessed(problem)
    retriage_ = bool(record.get("retriage"))
    found = occurrences.find(runtime.conn, problem.id)
    snapshot = Snapshot(Path(git.repo))
    case = cases.classify(found, retriage=retriage_)
    claim = claims.build(problem, found, record.get("userNotes", []), int(section["claimSamples"]))
    files = main_diff.candidate_files(claim, found)
    claim = claim.with_fact(main_diff.LABEL, main_diff.collect(git, problem.last_commit, commit, files).value())
    knowledge = tradeoff.entries_for(runtime, tradeoff.locations_of(problem, found, files))
    existing = notes.load(runtime.workspace, problem.id)
    issue_section = runtime.settings.section("assess.issue")
    inputs = Inputs(case.value, claim, existing.render() if existing else "无", tradeoff.render_for(runtime, knowledge),
                    section.get("severityGuide"), int(issue_section["titleMaxLength"]))
    latest = cases.latest(found)
    work = Work(problem, found, case, claim, inputs, files, knowledge,
                claims.severity_hint(latest) == "P0", retriage_)
    if problem.issue is None and not retriage_:
        params = dedup.Params.from_settings(section, int(runtime.settings.control("assess.dedup", "rounds")))
        found_dedup = dedup.decide(runtime, problem, claim, files, snapshot, params)
        for asked in found_dedup.asked:
            work.tally.add(asked)
        if found_dedup.note:
            work.notes.append(found_dedup.note)
        if found_dedup.target is not None and found_dedup.target != problem.id:
            work.merged_into = found_dedup.target
            return _finish_merge(runtime, work), work
    _judge(runtime, work, snapshot, commit, git)
    _decide(runtime, work)
    return _commit(runtime, work, snapshot, commit), work


def _judge(runtime: Runtime, work: Work, snapshot: Snapshot, commit: str, git: Git) -> None:
    section = runtime.settings.section("assess")
    retries = int(runtime.settings.control(TRIAGE, "rounds"))
    words = list(section["vagueWords"])
    prepared = cases.prepared(cases.latest(work.found)) if work.case is Case.VERIFIED else None
    case = Case.FULL if work.case is Case.VERIFIED else work.case
    work.evidence = evidence.gather(runtime, TRIAGE, work.problem.id, work.inputs, snapshot, case=case,
                                    retries=retries, vague_words=words, prepared=prepared)
    _count(work, work.evidence)
    sizes = section["sizes"]
    work.rating = rating.rate(work.evidence.output, p0=work.p0, size_limits=sizes)
    if not work.evidence.passed:
        work.verdict, work.needs_manual = refute.INSUFFICIENT, True
        work.notes.append(f"证据检查重做后仍未通过：{work.evidence.reason}")
        return
    work.verdict = str(work.evidence.verdict)
    rule = refute.RefuteRule.from_settings(section)
    # 能复现与轻量推测不做证伪复核；判不成立的 P0 例外(去向要求它经复核)
    if (work.case in (Case.FULL, Case.VERIFIED) or work.verdict == refute.REFUTED) and refute.needed(
            rule, work.verdict, p0=work.p0, severity=work.rating.severity, task_type=work.rating.task_type,
            impact_kind=work.rating.impact_kind):
        work.refuter = evidence.gather(runtime, REFUTE, work.problem.id, work.inputs, snapshot, case=Case.FULL,
                                       retries=retries, vague_words=words)
        _count(work, work.refuter)
        second = work.refuter.verdict if work.refuter.passed else None
        work.refuter_verdict = second
        final, work.needs_manual = refute.combine(work.verdict, second)
        if final != work.verdict and second in refute.CONFIRMING:
            work.evidence = work.refuter
            work.rating = rating.rate(work.refuter.output, p0=work.p0, size_limits=sizes)
        if work.needs_manual:
            work.notes.append(f"证伪复核判为 {second or '未通过证据检查'}，与取证的 {work.verdict} 不一致")
        work.verdict = final
    output = work.evidence.output or {}
    if work.verdict in refute.CONFIRMING and not work.needs_manual:
        github_ = runtime.github
        work.attribution = attribute(git, github_, commit, list(output.get("rootCauses") or []))
        work.notes += list(work.attribution.notes)
        hit = output.get("tradeoffHit")
        if tradeoff.tradeoff_valid(hit, work.knowledge):
            work.tradeoff_hit = hit
        elif hit:
            work.notes.append(f"取证给出的取舍 {hit} 不在给出的知识中或不是有效的取舍，未采纳")
    _save_notes(runtime, work, output, commit, snapshot.root)


def _decide(runtime: Runtime, work: Work) -> None:
    section = runtime.settings.section("assess")
    record = select.assessed(work.problem)
    output = (work.evidence.output if work.evidence else None) or {}
    assessed = output.get("assessment") or {}
    current = work.rating or Rating(None, None, None, None, ())
    protected = boundaries.high_risk(current.files, runtime.settings) + boundaries.forbidden(current.files,
                                                                                             runtime.settings)
    insufficient = int(record.get("insufficient", 0)) + 1 if work.verdict == refute.INSUFFICIENT else 0
    facts = disposition.Facts(
        verdict=work.verdict, severity=current.severity, size=current.size, worth=assessed.get("worth"),
        fixed_on_main=bool(output.get("fixedOnMain")) and work.verdict in refute.CONFIRMING,
        tradeoff_hit=work.tradeoff_hit is not None, needs_manual=work.needs_manual,
        refuter_verdict=work.refuter_verdict, insufficient_count=insufficient,
        insufficient_limit=int(section["insufficientToManual"]),
        observed_before=record.get("treatment") == "watch", touches_protected=bool(protected))
    work.decision = disposition.decide(facts, disposition.tree(section["treatment"]))


def _commit(runtime: Runtime, work: Work, snapshot: Snapshot, commit: str) -> AssessOutcome:
    """落库(一个事务)：问题状态与评估记录、抑制规则、写成 Issue；失败全部回滚，问题视为没处理过。"""
    decision = work.decision
    assert decision is not None and work.rating is not None
    output = (work.evidence.output if work.evidence else None) or {}
    problem = work.problem
    record = select.assessed(problem)
    reason = _reason(work, decision)
    insufficient = int(record.get("insufficient", 0)) + 1 if work.verdict == refute.INSUFFICIENT else 0
    assess_record: dict[str, Any] = {
        "attempt": int(record.get("attempt", 0)) + 1, "at": format_iso(runtime.clock.now()), "run": runtime.run,
        "commit": commit, "case": work.case.value, "verdict": work.verdict, "refuterVerdict": work.refuter_verdict,
        "severity": work.rating.severity, "size": work.rating.size, "taskType": work.rating.task_type,
        "impactKind": work.rating.impact_kind, "treatment": decision.treatment,
        "destination": decision.destination.value, "disposition": decision.disposition, "reason": reason,
        "rootCauses": list(output.get("rootCauses") or []), "insufficient": insufficient,
        "manual": decision.destination is Destination.MANUAL, "retriage": False, "outcome": None,
        "previous": [*record.get("previous", []), *([_brief(record)] if record.get("verdict") else [])],
        "userNotes": record.get("userNotes", [])}
    _save_evidence(runtime, work)
    if decision.destination is not Destination.ISSUE:
        with persist.restoring(runtime, [problem.id]), transaction(runtime.conn):
            problem = persist.update(runtime, problem, assess=assess_record)
            _apply_destination(runtime, problem, decision, reason)
        return AssessOutcome(problem.id, work.verdict, work.rating.severity, decision.disposition, None,
                             Status.PASSED, decision.destination.value, reason=reason)
    finding = create.Finding(
        problem, cases.latest(work.found), work.claim, output, work.rating.severity, work.rating.size,
        work.rating.task_type, work.rating.impact_kind, decision.treatment or "fix_later", decision.labels,
        tuple(item.to_json() for item in work.attribution.introduced_by), commit)
    made = create.Created()
    try:
        with persist.restoring(runtime, [problem.id]), transaction(runtime.conn):
            create.from_problem(runtime, finding, snapshot, made)
            persist.update(runtime, problem, status="ongoing", assess={**assess_record, "issues": made.issues},
                           issue=made.issues[0])
    except create.IssueNotCreated as error:
        create.undo(made)
        create.write_handoff(runtime, finding, None, str(error))
        raise
    except BaseException:
        create.undo(made)
        raise
    create.write_handoff(runtime, finding, made, None)
    _after_issue(runtime, made)
    return AssessOutcome(problem.id, work.verdict, work.rating.severity, decision.disposition, made.issues[0],
                         Status.FAILED if made.failures else Status.PASSED, decision.destination.value,
                         issues=made.issues, reason=reason)


def _apply_destination(runtime: Runtime, problem: Problem, decision: Decision, reason: str) -> None:
    destination = decision.destination
    if destination is Destination.WATCH_EVIDENCE:
        persist.update(runtime, problem, status="watching")
    elif destination is Destination.FALSE_POSITIVE:
        persist.suppress(runtime, problem, reason)
        persist.update(runtime, problem, status="closed")
    elif destination in (Destination.TRADEOFF, Destination.WONT_FIX):
        persist.update(runtime, problem, status="closed")
    elif destination is Destination.WATCH:
        occurrences_ = int(runtime.settings.section("assess")["watchReopenOccurrences"])
        persist.mute(runtime, problem, occurrences=occurrences_, new_release=False)
    elif destination is Destination.AWAITING_DEPLOY:
        persist.update(runtime, problem, status="ongoing")
    elif destination is Destination.MANUAL:
        _pending(runtime, problem, reason)


def _finish_merge(runtime: Runtime, work: Work) -> AssessOutcome:
    target = str(work.merged_into)
    reason = "；".join(work.notes) or f"与 {target} 同一根因"
    with persist.restoring(runtime, [work.problem.id, target]), transaction(runtime.conn):
        persist.merge(runtime, work.problem, target, reason)
    return AssessOutcome(work.problem.id, "merged", None, "wont_fix", None, Status.PASSED,
                         Destination.MERGED.value, merged_into=target, reason=reason)


def _after_issue(runtime: Runtime, made: create.Created) -> None:
    """事务提交之后：GitHub 同步与待决定的 Issue 写给人看的待审核文档。"""
    from tightrein.store.tables import issues

    for issue_id in made.issues:
        record = issues.get(runtime.conn, issue_id)
        if record is None:
            continue
        github.sync(runtime, record)
        if record.status == "needs_decision":
            _issue_pending(runtime, record.id, record.title)


def _pending(runtime: Runtime, problem: Problem, reason: str) -> None:
    pending(runtime, problem.id, point=POINT, decision="这个问题的证据不足以自动判定，需要你来定",
            options=["补充信息后重新评估", "直接改判"], recommendation="补充信息后重新评估", reason=reason,
            if_not="问题保持原状态，不再自动评估",
            command=f"tightrein problem retriage {problem.id} --note <补充的信息>")


def _issue_pending(runtime: Runtime, issue: str, title: str) -> None:
    pending(runtime, issue, point=create.POINT, decision=f"是否放行修复：{title}", options=["approve", "reject"],
            recommendation="approve", reason="新建的 Issue 默认等待放行", if_not="Issue 停在待决定，不会进入实施",
            command=f"tightrein approve {issue}")


def _save_notes(runtime: Runtime, work: Work, output: dict[str, Any], commit: str, worktree: Path) -> None:
    """读了代码就留下代码笔记：输出中的笔记位置加根因位置(核心)，原文与签名由程序截取。"""
    findings = list(output.get("notes") or [])
    findings += [{"location": f"{cause['file']}:{cause['line']}", "description": cause.get("symbol") or "根因",
                  "role": notes.CORE} for cause in output.get("rootCauses") or []]
    if not findings:
        return
    current = notes.load(runtime.workspace, work.problem.id) or notes.CodeNotes(work.problem.id, commit)
    current.add(findings, worktree)
    current.trigger = output.get("trigger") or current.trigger
    notes.save(runtime.workspace, current)


def _save_evidence(runtime: Runtime, work: Work) -> None:
    """取证与复核的完整输出(下次重新评估、改判与写 Issue 时读)。"""
    write_json(runtime.workspace.step_file(work.problem.id, EVIDENCE), {
        "claim": work.claim.to_json(), "case": work.case.value,
        "output": work.evidence.output if work.evidence else None,
        "failures": work.evidence.failures if work.evidence else [],
        "refuter": work.refuter.output if work.refuter else None})


def _previous_evidence(runtime: Runtime, problem: str) -> dict[str, Any] | None:
    path = runtime.workspace.step_file(problem, EVIDENCE)
    return read_json(path) if path.is_file() else None


def _write_handoff(runtime: Runtime, subject: str, outcome: AssessOutcome, facts: dict[str, Any], tally: Tally,
                   duration_ms: int, work: Work | None) -> None:
    from tightrein.protocol.records import versions

    facts = {"problem": outcome.problem, "verdict": outcome.verdict, "severity": outcome.severity,
             "disposition": outcome.disposition, "destination": outcome.destination, "issue": outcome.issue,
             "issues": outcome.issues, "mergedInto": outcome.merged_into, "knowledgeSuggestions": [],
             "misjudged": None, **facts}
    check_facts(POINT, facts, load_schema(HANDOFF_SCHEMA))
    model = "；".join(f"{point}={name}" for point, name in tally.models.items()) or None
    write(runtime.workspace.step_file(subject, HANDOFF), Handoff(
        point=POINT, subject=subject, run=runtime.run, status=outcome.status,
        summary=outcome.reason or f"{outcome.verdict}，去向 {outcome.destination}", facts=facts,
        metrics=tally.metrics(duration_ms), notes="\n".join(work.notes) if work and work.notes else None,
        created_at=format_iso(runtime.clock.now()),
        versions=versions(runtime.tool.root, runtime.settings.hash, prompt_hash=tally.prompt_hashes.get(TRIAGE),
                          tool=None, tool_version=None, model=model)))


def _facts(work: Work, outcome: AssessOutcome, commit: str) -> dict[str, Any]:
    output = (work.evidence.output if work.evidence else None) or {}
    current = work.rating
    return {
        "case": work.case.value, "commit": commit, "refuterVerdict": work.refuter_verdict,
        "needsManual": work.needs_manual, "size": current.size if current else None,
        "taskType": current.task_type if current else None,
        "treatment": work.decision.treatment if work.decision else None,
        "labels": list(work.decision.labels) if work.decision else [],
        "rootCauses": list(output.get("rootCauses") or []), "missingInfo": list(output.get("missingInfo") or []),
        "incidentalFindings": list(output.get("incidental") or []), "tradeoffHit": work.tradeoff_hit,
        "introducedBy": [item.to_json() for item in work.attribution.introduced_by],
        "checks": list(work.evidence.failures) if work.evidence else [],
        "reused": bool(work.evidence and work.evidence.reused),
        "knowledgeSuggestions": list(output.get("knowledgeSuggestions") or []), "misjudged": None}


def _failed(runtime: Runtime, problem: Problem, reason: str, started: float) -> AssessOutcome:
    if runtime.agents.breaker.object_failed(problem.id):
        with persist.restoring(runtime, [problem.id]), transaction(runtime.conn):
            persist.update(runtime, problems.get(runtime.conn, problem.id) or problem,
                           assess={"manual": True, "retriage": False, "failure": reason})
        reason += "；连续出错已到上限，转人工"
    outcome = AssessOutcome(problem.id, refute.INSUFFICIENT, None, "watch", None, Status.FAILED, reason=reason)
    _write_handoff(runtime, problem.id, outcome, {}, Tally(), _elapsed(started), None)
    return outcome


def _skipped(problem: Problem, reason: str) -> AssessOutcome:
    return AssessOutcome(problem.id, refute.INSUFFICIENT, None, "watch", None, Status.PENDING, reason=reason)


def _reason(work: Work, decision: Decision) -> str:
    output = (work.evidence.output if work.evidence else None) or {}
    parts = [f"判定为 {work.verdict}"]
    if output.get("trigger"):
        parts.append(f"触发条件：{output['trigger']}")
    source = output.get("sourceOfPhenomenon")
    if source and work.verdict == refute.REFUTED:
        parts.append(f"现象来源：{source['explanation']}")
    missing = [item["item"] for item in output.get("missingInfo") or []]
    if missing and work.verdict == refute.INSUFFICIENT:
        parts.append(f"缺少：{'、'.join(missing)}")
    if work.tradeoff_hit:
        parts.append(f"命中已接受的取舍 {work.tradeoff_hit}")
    parts.append(f"去向为 {decision.destination.value}")
    return "；".join([*parts, *work.notes])


def _brief(record: dict[str, Any]) -> dict[str, Any]:
    return {key: record.get(key) for key in ("attempt", "at", "verdict", "severity", "destination", "outcome")}


def _override_destination(verdict: str) -> Destination:
    if verdict == refute.REFUTED:
        return Destination.FALSE_POSITIVE
    if verdict == refute.INSUFFICIENT:
        return Destination.MANUAL
    return Destination.ISSUE


def _count(work: Work, found: Evidence) -> None:
    for asked in found.asked:
        work.tally.add(asked)
    work.tally.rounds += len(found.asked)


def _elapsed(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _describe(error: BaseException) -> str:
    detail = "".join(traceback.format_exception_only(type(error), error)).strip()
    frames = traceback.extract_tb(error.__traceback__)[-3:]
    where = "；".join(f"{frame.filename.rsplit('/', 1)[-1]}:{frame.lineno}" for frame in frames)
    return f"{detail}(位置 {where})" if where else detail

