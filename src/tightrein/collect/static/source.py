"""静态巡检的采集流程：范围 → 确定性工具 → 审查 → 筛主张 → 取证 → 信号。

- 只读 worktree(游离 HEAD)切到 origin/<主分支>：切不过去、或 HEAD 不在目标 commit 时直接失败并提示，不在错的版本上
  审查；
- 增量起点取上一次巡检的目标 commit(state 表 collect.static)；没有新提交时跳过；第一次按基线；
- 确定性工具：Semgrep(配置的规则集)、项目与技术栈的工具(tools/extension.py)、规则库(工作区 rules/ 下已验证的
  Semgrep 规则，命中直接产出信号，不经审查与取证)；某个工具失败时丢弃它的结果，巡检记为 partial；
- 审查与取证期间只读 worktree 去掉写权限(chmod，先写标记再改，中途崩溃也能恢复)，前后各比一次 git 状态与工作树
  哈希，有变化就整次作废；调用报告环境级越界同样整次作废，其余越界只作废那一次的产出；
- 审过的文件按内容哈希记在 state 表(collect.static.reviewed)，下次不再审；待取证清单(collect.static.pending)留着
  低级与超出上限的主张；
- 模型审查失败时增量起点不前进(下次重审时已审过的文件按内容哈希跳过)；只是工具失败时照常前进。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tightrein.agents.call import call
from tightrein.agents.result import CallResult
from tightrein.collect.common.signals import SignalFactory, SignalLimits
from tightrein.collect.common.source import (
    SourceMisconfigured,
    SourceResult,
    SourceStatus,
    SourceUnavailable,
    skipped,
)
from tightrein.collect.dedup import suppress
from tightrein.collect.static import baseline, claims, context, mapping, review, scope, verify
from tightrein.collect.static.claims import Claim, ToolFinding
from tightrein.collect.static.review import Caller, Review
from tightrein.collect.static.scope import Level, Scope
from tightrein.collect.static.tools import extension, semgrep
from tightrein.protocol.git import Git, GitError, worktrees
from tightrein.protocol.handoff import Metrics, Tokens
from tightrein.protocol.naming import format_iso
from tightrein.protocol.raw import RawDir, raw_dir
from tightrein.protocol.runtime import Runtime
from tightrein.protocol.security import SnapshotFailed, TreeSnapshot, snapshot
from tightrein.store.tables import state

SOURCE = "collect.static"
REVIEW_KEY = "collect.static.review"
STATE_KEY = SOURCE
REVIEWED_KEY = "collect.static.reviewed"
PENDING_KEY = "collect.static.pending"
WORKTREE = "readonly"
MARKER = "readonly-{name}.json"
RULES_DIR = "rules"
LIBRARY_RAW = "rule-library"
GUARD_FAILURE = "只读 worktree 在审查期间被改动或越过了只读边界，整次巡检作废"
REVIEW_SKIPPED = "改动只涉及文档、测试、配置或锁文件，或内容都已审过，跳过模型审查，只跑确定性工具"


@dataclass
class _Reviewed:
    claims: list[Claim] = field(default_factory=list)
    files: dict[str, str] = field(default_factory=dict)  # 审完的文件 → 内容哈希
    calls: list[CallResult] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    excluded: int = 0
    truncated: int = 0
    violated: bool = False
    review_failed: bool = False

    def add(self, done: Review, hashes: Mapping[str, str]) -> None:
        self.calls.append(done.result)
        if done.environment_violated:
            self.violated = True
            return
        problem = done.problem()
        if problem is not None:
            self.problems.append(problem)
            self.review_failed = True
            return
        self.claims += done.claims
        self.excluded += len(done.excluded)
        self.files.update(hashes)


@dataclass
class _Tools:
    findings: list[ToolFinding] = field(default_factory=list)
    library: list[ToolFinding] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def collect(runtime: Runtime, *, level: Level = Level.INCREMENTAL, caller: Caller = call) -> SourceResult:
    section = runtime.settings.section(SOURCE)
    worktree, marker = _worktree(runtime)
    git = runtime.git.at(worktree)
    previous = state.get(runtime.conn, STATE_KEY) or {}
    found = scope.resolve(git, previous.get("commit"), level)
    if found is None:
        return skipped(SOURCE, scope.NO_NEW_COMMITS)
    raw = RawDir(raw_dir(runtime.workspace, runtime.run, SOURCE))
    tools = _tools(runtime, section, found, worktree, raw)
    before = _snapshot(runtime, worktree)
    try:
        worktrees.lock_readonly(worktree, marker, runtime.clock)
    except (OSError, worktrees.ReadonlyLocked) as error:
        raise SourceUnavailable(f"只读 worktree 锁定失败：{error}") from error
    try:
        reviewed = _review(runtime, section, found, git, worktree, tools.findings, caller)
        if reviewed.violated:
            raise SourceUnavailable(GUARD_FAILURE)
        screened = claims.screen(reviewed.claims, worktree=worktree, known=claims.open_problems(runtime.conn),
                                 rules=_suppression(runtime), now=runtime.clock.now(),
                                 nearby_lines=int(section["duplicateLines"]), run=runtime.run)
        queued, kept = claims.queued_claims(state.get(runtime.conn, PENDING_KEY) or [])
        limit = (baseline.BaselineSettings.from_section(section).max_claims if found.level is Level.BASELINE
                 else int(section["maxVerify"]))
        selection = claims.select(queued, screened.kept, limit)
        verified = verify.run(selection.verify, verify.verifier(runtime, workdir=worktree, caller=caller),
                              workers=int(runtime.settings.get("resources.concurrency.modelCalls")))
    finally:
        worktrees.restore_readonly(marker)
    if verified.violated or _snapshot(runtime, worktree) != before:
        raise SourceUnavailable(GUARD_FAILURE)
    return _result(runtime, section, found, git, raw, tools, reviewed, screened, selection, kept, verified, previous)


def _worktree(runtime: Runtime) -> tuple[Path, Path]:
    """建好并切到 origin/<主分支> 的只读 worktree 与它的锁定标记。"""
    path = runtime.workspace.worktree(WORKTREE)
    marker = runtime.workspace.worktrees_dir / MARKER.format(name=WORKTREE)
    try:
        if not path.exists():
            worktrees.create_readonly(runtime.git, path, scope=runtime.scope(runtime.run, SOURCE))
        synced = worktrees.sync_readonly(runtime.git, path, marker=marker)
        head = runtime.git.at(path).head().commit
    except (GitError, worktrees.ReadonlyLocked) as error:
        raise SourceUnavailable(f"只读 worktree 切换失败：{error}") from error
    if head is None or head != synced.commit:
        raise SourceUnavailable(f"只读 worktree 不在 {synced.commit}，先同步只读 worktree 再巡检")
    return path, marker


def _snapshot(runtime: Runtime, worktree: Path) -> TreeSnapshot:
    try:
        return snapshot(worktree, runtime.runner)
    except SnapshotFailed as error:
        raise SourceUnavailable(f"读不了只读 worktree 的 git 状态，不能放行审查：{error}") from error


def _tools(runtime: Runtime, section: Mapping[str, Any], found: Scope, worktree: Path, raw: RawDir) -> _Tools:
    result = _Tools()
    command = semgrep.resolve_command(runtime.settings.get("tools.semgrep.path"), runtime.tool.root)
    timeout_s = runtime.settings.duration("limits.timeouts.semgrep")
    rules = semgrep.run(runtime.runner, section["semgrep"]["configs"], found, worktree, raw.root, runtime.environ,
                        timeout_s, command)
    result.findings += rules.findings
    (result.problems if rules.status == semgrep.FAILED else result.notes).extend(rules.notes)
    library_dir = runtime.workspace.root / RULES_DIR
    configs = [str(path) for path in sorted(library_dir.glob("*.yaml"))] if library_dir.is_dir() else []
    if configs:
        library = semgrep.run(runtime.runner, configs, found, worktree, raw.path(LIBRARY_RAW), runtime.environ,
                              timeout_s, command)
        result.library += library.findings
        if library.status == semgrep.FAILED:
            result.problems += [f"规则库：{note}" for note in library.notes]
    given = extension.Runner(runtime.runner, runtime.workspace.root, runtime.environ, runtime.secrets,
                             runtime.redactor, raw)
    project = extension.run(section["extension"], found, worktree, given)
    result.findings += project.findings
    (result.problems if project.degraded else result.notes).extend(project.notes)
    return result


def _review(runtime: Runtime, section: Mapping[str, Any], found: Scope, git: Git, worktree: Path,
            findings: Sequence[ToolFinding], caller: Caller) -> _Reviewed:
    result = _Reviewed()
    limits = context.Limits.from_section(section)
    reviewed = state.get(runtime.conn, REVIEWED_KEY) or {}
    if found.level is Level.BASELINE:
        # 内容已审过的不再审：预算用尽中断的基线下次从没审到的文件接着审
        hashes = scope.content_hashes(worktree, found.files)
        todo = scope.unreviewed(hashes, reviewed)
        batches = baseline.run(runtime, workdir=worktree, files=todo, findings=findings,
                               settings=baseline.BaselineSettings.from_section(section),
                               knowledge=context.knowledge(runtime.workspace, todo, limits), caller=caller)
        for done in batches.reviews:
            result.add(done, {})
        result.files.update({path: hashes[path] for path in batches.reviewed})
        result.notes += batches.notes
    elif found.base is not None and found.changed:
        _incremental(runtime, section, found, git, worktree, findings, reviewed, limits, result, caller)
    if found.whole_repo and not result.violated:
        _variants(runtime, worktree, result, caller)
    return result


def _incremental(runtime: Runtime, section: Mapping[str, Any], found: Scope, git: Git, worktree: Path,
                 findings: Sequence[ToolFinding], reviewed: Mapping[str, str], limits: context.Limits,
                 result: _Reviewed, caller: Caller) -> None:
    non_code = [*section["nonCode"], *(runtime.settings.project.test_patterns if runtime.settings.project else ())]
    candidates, skipped_files = scope.reviewable(found.changed, non_code)
    hashes = scope.content_hashes(worktree, candidates)
    todo = scope.unreviewed(hashes, reviewed)
    if not todo:
        result.notes.append(REVIEW_SKIPPED)
        return
    if skipped_files or len(todo) < len(candidates):
        result.notes.append(f"模型审查 {len(todo)} 个文件(非代码改动 {skipped_files} 个、内容已审过 "
                            f"{len(candidates) - len(todo)} 个不审)")
    targets = frozenset(todo)
    max_claims = int(runtime.settings.section(REVIEW_KEY)["maxClaims"])
    done = review.review(
        runtime, workdir=worktree, range_text=f"{found.base}..{found.head}",
        changes=context.changes_text(git, found.base or "", found.head, todo, worktree, limits),
        findings=[item for item in findings if item.file in targets or item.vulnerability],
        knowledge=context.knowledge(runtime.workspace, todo, limits), max_claims=max_claims, caller=caller)
    kept, truncated = claims.top(done.claims, max_claims)
    result.add(Review(done.what, done.result, tuple(kept), done.excluded), {path: hashes[path] for path in todo})
    result.truncated += truncated


def _variants(runtime: Runtime, worktree: Path, result: _Reviewed, caller: Caller) -> None:
    patterns = context.defect_patterns(runtime.workspace)
    if not patterns:
        return
    tradeoffs = context.tradeoffs(runtime.workspace)
    for number, entry in enumerate(patterns, start=1):
        done = review.variant(runtime, workdir=worktree, pattern_id=entry.id,
                              pattern=f"{entry.title}\n\n{entry.body.strip()}", tradeoffs=tradeoffs, number=number,
                              caller=caller)
        result.add(done, {})
        if result.violated:
            return
        if done.exhausted:
            result.notes.append(f"额度或用量已到上限，其余 {len(patterns) - number} 个缺陷模式的变体扫描未运行")
            return


def _suppression(runtime: Runtime) -> tuple[suppress.SuppressionRule, ...]:
    try:
        return suppress.load(runtime.conn, runtime.settings.section("collect.dedup").get("suppress") or [])
    except suppress.SuppressionInvalid as error:
        raise SourceMisconfigured(str(error)) from error


def _pending(section: Mapping[str, Any], kept: list[dict[str, Any]], selection: claims.Selection,
             verified: verify.VerifyRun) -> list[dict[str, Any]]:
    """合并待取证清单：同一处只留一条，按严重度保留 pendingLimit 条。"""
    items = [*kept, *selection.pending,
             *(claims.pending_item(claim, claims.OVER_LIMIT) for claim in verified.not_started)]
    unique: dict[tuple[str, int, str], dict[str, Any]] = {}
    for item in items:
        unique.setdefault(Claim.from_json(item["claim"]).key, item)
    ordered = sorted(unique.values(), key=lambda item: Claim.from_json(item["claim"]).severity_rank)
    return ordered[:int(section["pendingLimit"])]


def _result(runtime: Runtime, section: Mapping[str, Any], found: Scope, git: Git, raw: RawDir, tools: _Tools,
            reviewed: _Reviewed, screened: claims.Screened, selection: claims.Selection,
            kept: list[dict[str, Any]], verified: verify.VerifyRun, previous: Mapping[str, Any]) -> SourceResult:
    factory = SignalFactory(run=runtime.run, source=SOURCE, clock=runtime.clock, redactor=runtime.redactor, raw=raw,
                            limits=SignalLimits.from_settings(runtime.settings, SOURCE))
    signals = mapping.to_signals(verified.done, tools.findings, factory, head=found.head, git=git)
    signals += mapping.rule_signals(tools.library, factory, head=found.head, now=runtime.clock.now())
    pending = _pending(section, kept, selection, verified)
    verdicts = [item.verdict for item in verified.done]
    notes = [*tools.notes, *reviewed.notes, *verified.notes]
    if found.first_run:
        notes.append("第一次巡检，检查范围为全部文件，按基线审查")
    if reviewed.truncated:
        notes.append(f"审查的主张超过上限，按严重度截掉 {reviewed.truncated} 条")
    if screened.dropped:
        notes.append("取证前筛掉：" + "、".join(f"{reason} {count} 条" for reason, count in screened.dropped.items()))
    problems = [*tools.problems, *reviewed.problems]
    if verified.degraded:
        problems.append("部分主张的取证未完成")
    calls = [*reviewed.calls, *verified.calls]
    tokens = Tokens()
    for item in calls:
        tokens.add(item.tokens)
    commit = previous.get("commit") if reviewed.review_failed else found.head
    stored_hashes = {path: digest for path, digest in {**(state.get(runtime.conn, REVIEWED_KEY) or {}),
                                                        **reviewed.files}.items()
                     if (git.repo / path).is_file()}
    saved: dict[str, Any] = {
        PENDING_KEY: pending,
        REVIEWED_KEY: stored_hashes,
    }
    if commit is not None:
        saved[STATE_KEY] = {"commit": commit, "level": found.level.value, "at": format_iso(runtime.clock.now())}
    metrics = Metrics(calls=len(calls), tokens=tokens,
                      cost_usd=sum(item.cost_usd or 0.0 for item in calls) if calls else None,
                      produced={"signals": len(signals), "toolFindings": len(tools.findings),
                                "claims": len(reviewed.claims), "excludedClaims": reviewed.excluded,
                                "verifiedClaims": len(verified.done), "refutedClaims": verdicts.count(verify.REFUTED),
                                "insufficientClaims": verdicts.count(verify.INSUFFICIENT),
                                "ruleHits": len(tools.library), "pendingClaims": len(pending)})
    status = SourceStatus.PARTIAL if problems else SourceStatus.DONE
    return SourceResult(SOURCE, status, signals, len(found.files), None, "；".join(problems) or None, saved, metrics,
                        coverage=list(found.files), notes=notes)
