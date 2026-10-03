"""StaticProbe(architecture/04 5.2、5.6，redesign/01-collect.md 第 6 节)。

步骤：确认只读 worktree 在目标 commit → 确定扫描范围(没有新提交的 incremental 跳过) → static-tools 与 Semgrep →
规则库 → 记录 git 状态 → 审查 → 取证 → 核对 git 状态未变 → 对成立的主张 git blame → 映射信号。
- 规则库：工作区 rules/ 中由缺陷变规则验证通过的规则单独运行一次 Semgrep，命中直接映射为信号(规则已用原补丁验证，
  不经审查与取证)；失败时巡检为 partial；
- 审查：incremental 与 full 为 reviewer.review(审查 diff)；baseline 档位与没有上次巡检终点的 full 档位为基线审查，
  按目录模块分批(baseline.plan)逐批 review_baseline，当天预算用尽时其余批次不再运行；full 与 baseline 另对每个缺陷
  模式 scan_variants；两种审查都用强档(roleCapabilities.static-review、baseline-review)；
- 取证与待处理清单：低级疑点不在采集时取证，直接进入待处理清单(pending_claims，原因 low)；先取证清单中超出上限
  遗留的疑点(options.queued)，再按严重度取证本次的高、中级疑点，合计不超过取证上限(sources.static.maxClaims，基线为
  baseline.maxClaims)；超出上限与预算用尽后未取证的进入清单(原因 over-limit)，后续运行继续处理。清单中的低级疑点经
  `--select pending:<编号>|low` 选中时取证(这时不做审查，只取证选中的疑点)。取证结论随信号交给分诊直接复用；
- 只读锁定由执行器在每次审查与取证任务前后完成(guards.before、guards.after)。某次调用报告 guard-violation 时按违规
  种类区分：环境级(reviewer.ENVIRONMENT_VIOLATIONS)或前后 git 状态不同，整次巡检为 failed，不产出任何信号；其余只作废
  该次调用的产出，notes 写明，运行为 partial；
- 某个确定性工具失败、static-tools 整体失败、review 或 verify 返回 schema-invalid、limit-reached、failed 时为 partial；
- insufficient 计入 insufficientClaims，refuted 计入 refutedClaims，都不产出信号；
- coverage.files 为本次扫描范围内的文件。
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

from tightrein.config.project import MissingSetting, ProjectConfig
from tightrein.domain.enums import Probe as ProbeKind
from tightrein.domain.enums import ProbeLevel, RunnerStatus, RunStatus, Verdict
from tightrein.domain.run import Coverage
from tightrein.extensions.client import ExtensionClient
from tightrein.sources.base import (PROBE_LEVELS, ProbeOptions, ProbeOutcome, ProbeTarget, failed, resolve_level,
                                     skipped)
from tightrein.sources.common.procs import Launcher
from tightrein.sources.common.raw import RawDir
from tightrein.sources.common.redact import ProbeRedactor
from tightrein.sources.common.signals import RandomBytes, SignalFactory
from tightrein.sources.common.target import head_matches
from tightrein.sources.static import baseline as baselines
from tightrein.sources.static import mapping
from tightrein.sources.static import scope as scoping
from tightrein.sources.static.baseline import Batch, BaselineSettings
from tightrein.sources.static.reviewer import (
    Claim,
    ReviewResult,
    Reviewer,
    ToolFinding,
    VerifiedClaim,
    environment_violated,
)
from tightrein.sources.static.scope import Scope
from tightrein.sources.static.tools import extension, semgrep
from tightrein.store.repos import pending_claims
from tightrein.store.repos.pending_claims import PendingClaim
from tightrein.vcs.git_read import GitReader

GUARD_FAILURE = "只读 worktree 在审查期间被改动或越过了只读边界，整次巡检作废"
LOW = "low"
LIBRARY_DIR = "rule-library"
PENDING_PREFIX = "PC-"
PENDING_DIGITS = 10


@dataclass(frozen=True)
class StaticDependencies:
    config: ProjectConfig
    client: ExtensionClient
    git: GitReader
    launcher: Launcher
    redactor: ProbeRedactor
    environ: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    randomness: RandomBytes = os.urandom
    semgrep: str = semgrep.TOOL
    rules_dir: Path | None = None


def library_configs(directory: Path | None) -> list[str]:
    """规则库中的规则文件；没有时不运行。"""
    return [str(path) for path in sorted(directory.glob("*.yaml"))] if directory is not None and \
        directory.is_dir() else []


def semgrep_configs(config: ProjectConfig) -> list[str]:
    try:
        return list(config.get("sources.static.semgrep.configs"))
    except MissingSetting:
        return []


def max_claims(config: ProjectConfig, options: ProbeOptions, settings: BaselineSettings | None = None) -> int:
    if options.max_claims is not None:
        return options.max_claims
    if settings is not None:
        return settings.max_claims
    return int(config.get("sources.static.maxClaims"))


def effective_level(level: ProbeLevel, base_commit: str | None) -> ProbeLevel:
    """没有上次巡检终点(第一次巡检)的 full 档位按基线审查。"""
    return ProbeLevel.BASELINE if level is ProbeLevel.FULL and base_commit is None else level


def assign_findings(batches: tuple[Batch, ...], findings: list[ToolFinding]) -> list[list[ToolFinding]]:
    """确定性工具的结果分到所在文件的批；文件不在任何一批中的(例如依赖清单)放进第一批。"""
    owner = {path: index for index, batch in enumerate(batches) for path in batch.paths}
    assigned: list[list[ToolFinding]] = [[] for _ in batches]
    for finding in findings:
        assigned[owner.get(finding.file, 0)].append(finding)
    return assigned


def _seconds(milliseconds: int) -> str:
    return f"{milliseconds / 1000:.0f} 秒"


def _cost(value: float | None) -> str:
    return "费用未知" if value is None else f"费用 ${value:.2f}"


@dataclass
class _Review:
    claims: dict[tuple[str, int, str], tuple[Claim, str | None]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    excluded: int = 0
    degraded: bool = False
    violated: bool = False
    stats: dict[str, float] = field(default_factory=dict)

    def add(self, result: ReviewResult, what: str) -> None:
        if result.status is RunnerStatus.GUARD_VIOLATION:
            if environment_violated(result.violations):
                self.violated = True
            else:
                self.degraded = True
                self.notes.append(voided(what, result.violations))
            return
        if result.status is not RunnerStatus.OK:
            self.degraded = True
            self.notes.append(f"{what} 未完成({result.status.value})：{result.detail or '没有说明'}，其主张不产出信号")
            return
        self.excluded += len(result.excluded)
        for claim in result.claims:
            self.claims.setdefault((claim.file, claim.line, claim.rule_or_pattern), (claim, result.transcript))


def voided(what: str, violations: tuple[str, ...]) -> str:
    return f"{what} 的边界检查违规({'、'.join(violations)})，该任务的产出作废"


def claim_id(claim: Claim) -> str:
    """待处理疑点的编号：按文件、行与规则计算，同一疑点再次被审查出来时编号相同。"""
    digest = hashlib.sha1(f"{claim.file}:{claim.line}:{claim.rule_or_pattern}".encode("utf-8")).hexdigest()
    return f"{PENDING_PREFIX}{digest[:PENDING_DIGITS]}"


def to_pending(claim: Claim, reason: str, run_id: str, now: datetime) -> PendingClaim:
    data = {"file": claim.file, "line": claim.line, "ruleOrPattern": claim.rule_or_pattern, "layer": claim.layer,
            "statement": claim.statement, "trigger": claim.trigger, "severity": claim.severity}
    return PendingClaim(claim_id(claim), data, claim.severity, reason, run_id, now)


@dataclass
class _Verified:
    items: list[VerifiedClaim] = field(default_factory=list)
    pending: list[PendingClaim] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    degraded: bool = False
    violated: bool = False


def _baseline(reviewer: Reviewer, repo: Path, found: Scope, findings: list[ToolFinding], settings: BaselineSettings,
              review: _Review) -> None:
    """逐批基线审查；当天预算用尽时其余批次不再运行。批数、耗时与费用写入 stats 与 notes。"""
    planned = baselines.plan(repo, found.files, settings)
    batches = planned.batches
    reviewed = sum(len(batch.files) for batch in batches)
    review.stats.update(baselineBatches=len(batches), baselineFiles=reviewed, baselineExcludedFiles=planned.excluded)
    if not batches:
        review.notes.append(f"基线审查：排除 {planned.excluded} 个文件后没有要审查的源代码文件")
        return
    batch_notes: list[str] = []
    duration = 0
    costs: list[float | None] = []
    for batch, assigned in zip(batches, assign_findings(batches, findings)):
        result = reviewer.review_baseline(batch, assigned)
        review.add(result, f"基线审查{batch.describe()}")
        if review.violated:
            return
        duration += result.duration_ms
        costs.append(result.cost_usd)
        batch_notes.append(f"基线审查{batch.describe()}：耗时 {_seconds(result.duration_ms)}，{_cost(result.cost_usd)}")
        remaining = batch.total - batch.number
        if result.exhausted and remaining:
            batch_notes.append(f"当天预算已用尽，其余 {remaining} 批未审查；改天或调高 stages.collect.budgetPerDay "
                               "后再运行 --level baseline")
            break
    known = [value for value in costs if value is not None]
    total = sum(known) if known else None
    review.stats["baselineDurationMs"] = duration
    if total is not None:
        review.stats["baselineCostUsd"] = round(total, 4)
    partly = "，部分批次没有费用数据" if known and len(known) < len(costs) else ""
    review.notes.append(f"基线审查共 {len(batches)} 批，审查 {reviewed} 个文件、排除 {planned.excluded} 个；"
                        f"已运行 {len(costs)} 批，合计耗时 {_seconds(duration)}，{_cost(total)}{partly}")
    review.notes.extend(batch_notes)


class StaticProbe:
    name = ProbeKind.STATIC
    levels = PROBE_LEVELS[ProbeKind.STATIC]

    def __init__(self, dependencies: StaticDependencies) -> None:
        self.deps = dependencies

    def _state(self, repo: Path) -> tuple[object, ...]:
        status = self.deps.git.status(repo)
        return status.commit, status.entries

    def run(self, target: ProbeTarget, level: ProbeLevel | None, options: ProbeOptions) -> ProbeOutcome:
        resolved = resolve_level(self.name, level)
        repo = target.worktree
        if resolved is None or repo is None:
            return failed("静态巡检需要档位与只读 worktree")
        reviewer = options.reviewer
        if reviewer is None:
            return failed("没有注入审查器(Reviewer)，由 collect 提供")
        head = self.deps.git.head(repo).commit
        if target.release is not None and not head_matches(head, target.release):
            return failed(f"只读 worktree 不在 {target.release}，先执行 tightrein worktree sync")
        if options.pending:
            return self._selected(target, repo, head, reviewer, options)
        found, reason = scoping.resolve(self.deps.git, repo, options.base_commit,
                                        effective_level(resolved, options.base_commit))
        if found is None:
            return skipped(reason or scoping.NO_NEW_COMMITS)
        settings = BaselineSettings.from_config(self.deps.config) if found.level is ProbeLevel.BASELINE else None
        tools = extension.run(self.deps.client, repo, found, target.raw_dir)
        rules = semgrep.run(self.deps.launcher, semgrep_configs(self.deps.config), found, repo, target.raw_dir,
                            self.deps.environ, float(self.deps.config.get("runtime.sources.semgrepTimeoutSeconds")),
                            self.deps.semgrep)
        library = semgrep.run(self.deps.launcher, library_configs(self.deps.rules_dir), found, repo,
                              target.raw_dir / LIBRARY_DIR, self.deps.environ,
                              float(self.deps.config.get("runtime.sources.semgrepTimeoutSeconds")), self.deps.semgrep)
        findings = [*tools.findings, *rules.findings]
        before = self._state(repo)
        review = self._review(reviewer, repo, found, findings, settings)
        limit = max_claims(self.deps.config, options, settings)
        verified = self._verify(reviewer, review, list(options.queued), limit, target)
        if review.violated or verified.violated or self._state(repo) != before:
            return failed(GUARD_FAILURE, extensions=tools.extensions)
        return self._outcome(target, repo, found.head, found, findings, tools, rules, review, verified, library)

    def _selected(self, target: ProbeTarget, repo: Path, head: str, reviewer: Reviewer,
                  options: ProbeOptions) -> ProbeOutcome:
        """只取证选中的待处理疑点，不做审查；覆盖范围为空(不据此判定已解决)。"""
        if not options.queued:
            return skipped("待处理清单中没有选中的疑点")
        before = self._state(repo)
        verified = self._verify(reviewer, _Review(), list(options.queued), len(options.queued), target)
        if verified.violated or self._state(repo) != before:
            return failed(GUARD_FAILURE)
        return self._outcome(target, repo, head, None, [], None, None, _Review(), verified)

    def _review(self, reviewer: Reviewer, repo: Path, found: Scope, findings: list[ToolFinding],
                settings: BaselineSettings | None) -> _Review:
        review = _Review()
        if settings is None:
            review.add(reviewer.review(found, findings), "增量审查")
        else:
            _baseline(reviewer, repo, found, findings, settings, review)
        if found.whole_repo and not review.violated:
            for pattern in reviewer.defect_patterns():
                review.add(reviewer.scan_variants(pattern, found), f"缺陷模式 {pattern} 的全量扫描")
                if review.violated:
                    break
        return review

    def _verify(self, reviewer: Reviewer, review: _Review, queued: list[PendingClaim], limit: int,
                target: ProbeTarget) -> _Verified:
        """先取证清单中遗留的疑点，再按严重度取证本次的高、中级疑点；低级与超出上限的进入待处理清单。"""
        now = target.clock.now()
        result = _Verified()
        fresh = sorted(review.claims.values(), key=lambda item: item[0].severity_rank)
        result.pending += [to_pending(claim, pending_claims.LOW, target.run_id, now)
                           for claim, _ in fresh if claim.severity == LOW]
        candidates: list[tuple[Claim, str | None, PendingClaim | None]] = [
            (Claim.from_dict(item.claim), None, item) for item in queued]
        candidates += [(claim, transcript, None) for claim, transcript in fresh if claim.severity != LOW]
        stopped = False
        for index, (claim, transcript, origin) in enumerate(candidates):
            if stopped or index >= limit:
                if origin is None:
                    result.pending.append(to_pending(claim, pending_claims.OVER_LIMIT, target.run_id, now))
                continue
            verification = reviewer.verify(claim)
            if verification.status is RunnerStatus.GUARD_VIOLATION:
                if environment_violated(verification.violations):
                    result.violated = True
                    return result
                result.degraded = True
                result.notes.append(voided(f"{claim.file}:{claim.line} 的取证", verification.violations))
                continue
            if verification.status is RunnerStatus.LIMIT_REACHED:
                result.degraded = True
                result.notes.append(f"取证达到上限({verification.detail or '预算'})，其余疑点进入待处理清单")
                stopped = True
                if origin is None:
                    result.pending.append(to_pending(claim, pending_claims.OVER_LIMIT, target.run_id, now))
                continue
            if verification.status is not RunnerStatus.OK:
                result.degraded = True
                result.notes.append(f"{claim.file}:{claim.line} 的取证未完成({verification.status.value})")
                continue
            result.items.append(VerifiedClaim(claim, verification, transcript))
            if origin is not None:
                result.pending.append(replace(origin, state=pending_claims.VERIFIED))
        return result

    def _outcome(self, target: ProbeTarget, repo: Path, head: str, found: Scope | None, findings: list[ToolFinding],
                 tools: extension.ExtensionTools | None, rules: semgrep.SemgrepRun | None, review: _Review,
                 verified: _Verified, library: semgrep.SemgrepRun | None = None) -> ProbeOutcome:
        by_location = {(item.file, item.rule): item for item in findings}
        enriched = []
        for item in verified.items:
            finding = by_location.get((item.claim.file, item.claim.rule_or_pattern))
            blamed = None
            if item.verification.verdict in mapping.SIGNAL_VERDICTS:
                blamed = mapping.introduced_by(self.deps.git, repo, item.claim.file, item.claim.line, head)
            enriched.append(VerifiedClaim(item.claim, item.verification, item.review_transcript, finding, blamed))
        factory = SignalFactory(target, self.name, self.deps.redactor, randomness=self.deps.randomness)
        signals = mapping.to_signals(enriched, factory, head, target.clock.now())
        hits = list(library.findings) if library is not None and library.status == "ok" else []
        signals += mapping.rule_signals(hits, factory, head, target.clock.now())
        verdicts = [item.verification.verdict for item in verified.items]
        notes = [*(tools.notes if tools else ()), *(rules.notes if rules else ()),
                 *(f"规则库：{note}" for note in (library.notes if library and library.status == "failed" else ())),
                 *review.notes, *verified.notes]
        if found is not None and found.first_run:
            notes.append("第一次巡检，扫描范围为全部文件" + ("，按基线审查" if found.level is ProbeLevel.BASELINE else ""))
        low = sum(1 for item in verified.pending if item.reason == pending_claims.LOW)
        over = sum(1 for item in verified.pending if item.reason == pending_claims.OVER_LIMIT)
        if low:
            notes.append(f"{low} 条低级疑点进入待处理清单，选中时取证(--select pending:<编号>|low)")
        if over:
            notes.append(f"{over} 条疑点超出本次取证上限，进入待处理清单，后续运行继续取证")
        partial = (bool(tools and tools.degraded) or bool(rules and rules.status == "failed") or review.degraded
                   or verified.degraded or bool(library and library.status == "failed"))
        stats = {"toolFindings": len(findings), "claims": len(review.claims), "excludedClaims": review.excluded,
                 "verifiedClaims": len(verified.items), "ruleHits": len(hits), "signals": len(signals),
                 "pendingLow": low,
                 "pendingOverLimit": over, "insufficientClaims": verdicts.count(Verdict.INSUFFICIENT),
                 "refutedClaims": verdicts.count(Verdict.REFUTED), **review.stats}
        return ProbeOutcome(RunStatus.PARTIAL if partial else RunStatus.OK, tuple(signals),
                            Coverage(files=found.files if found is not None else ()), stats=stats,
                            artifacts=RawDir(target.raw_dir).files(), notes=tuple(notes),
                            extensions=tools.extensions if tools else {}, pending_claims=tuple(verified.pending))
