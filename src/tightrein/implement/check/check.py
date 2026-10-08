"""自检(implement.check)：程序跑项目检查、改动量与规则检查、本机运行检查，不调用模型(截图审查除外)。

流程：
1. 改动没变就不重做：上次已通过且 diff 哈希相同，直接沿用上次结果；
2. 改动量与规则检查(rules.py)；
3. 项目检查(测试、lint、类型检查、构建，命令取 settings.json 的 project.commands)：第一轮全量；修正轮次先只跑受影响的
   (affected.py，快速失败)，都过了再补跑其余的，所以交给审查与交付的「通过」总是全量通过；相互独立的命令并行；
   检查命令执行后工作区被改了算局部问题；
4. 前面都过了才做本机运行检查(runtime/session.py)：启动服务较慢，先让便宜的检查把明显的问题拦下；
5. 交出 handoff：不通过项带性质与(位置, 类型)，供「没有进展就停」比较；没跑起来的检查记为需要用户(停下报告原因，
   不交回编码)；等用户确认迁移或查看截图时为待决定。
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any

from tightrein.implement.check import affected, commands, rules
from tightrein.implement.check.affected import Planned
from tightrein.implement.check.changes import Changes, changed_since, collect, file_states
from tightrein.implement.check.commands import CommandResult, CommandSettings
from tightrein.implement.check.findings import LOCAL, NEEDS_USER, Finding, first_category
from tightrein.implement.check.runtime import session
from tightrein.implement.check.runtime.session import RuntimeOutcome
from tightrein.implement.check.runtime.verdict import Result
from tightrein.implement.context import ImplementContext
from tightrein.protocol import boundaries
from tightrein.protocol.documents import pending
from tightrein.protocol.git import Git
from tightrein.protocol.handoff import Handoff, Metrics, Status
from tightrein.protocol.naming import format_iso, step_sequence
from tightrein.protocol.runtime import Runtime

POINT = "implement.check"
DESIGN = "implement.design"
FULL = "full"
AFFECTED = "affected"
TESTS_SLOT = "tests"
RAW_SUFFIX = "raw"


def run(runtime: Runtime, context: ImplementContext, *, deps: session.SessionDeps | None = None) -> Handoff:
    started = time.monotonic()
    git, base, worktree = _workspace(context)
    diff_hash = git.diff_hash(base)
    previous = context.last(POINT)
    if previous is not None and previous.status is Status.PASSED and previous.facts.get("diffHash") == diff_hash:
        reason = "改动没变，沿用上次结果"
        return replace(previous, run=runtime.run, round=context.round, summary=f"{reason}：{previous.summary}",
                       facts={**previous.facts, "skipped": reason}, created_at=format_iso(runtime.clock.now()))
    changes = collect(git, base)
    states = file_states(worktree, changes.paths)
    this_round = changed_since(previous.facts.get("fileStates") or {}, states) if previous is not None \
        else list(changes.paths)
    design = context.last(DESIGN)
    findings = rules.evaluate(changes, planned=rules.planned_files(design.facts if design else {}), worktree=worktree,
                              settings=runtime.settings, rules=rules.RuleSettings.from_settings(runtime.settings),
                              over_cap_before=_over_cap_before(previous),
                              approved=rules.approved_files(design.facts if design else {}))
    full = previous is None or context.round <= 1
    results, scope = _project_checks(runtime, context, git, worktree, this_round, previous, full, changes.paths)
    findings += _command_findings(results)
    if file_states(worktree, changes.paths) != states or set(git.changed_files(base)) != set(changes.paths):
        findings.append(Finding("project", "worktree_modified", None,
                                "检查命令执行后工作区出现新改动：检查不能改代码(格式化、自动修复等要在编码时做)", LOCAL))
    outcome = RuntimeOutcome()
    if not findings:
        outcome = session.run(runtime, context, changes, raw_dir=_raw_dir(runtime, context, "runtime"), deps=deps)
        findings += [Finding("runtime", item.category, item.id, item.reason or "检查失败", LOCAL)
                     for item in outcome.items if item.result is Result.FAILED]
    return _handoff(runtime, context, started, diff_hash, base, changes, states, this_round, scope, results, findings,
                    outcome)


def _workspace(context: ImplementContext) -> tuple[Git, str, Path]:
    if context.git is None or context.worktree is None or context.base_commit is None:
        raise ValueError("自检之前没有准备好 worktree 与基准 commit")
    return context.git, context.base_commit, context.worktree


def _over_cap_before(previous: Handoff | None) -> bool:
    """同一次实施中之前已因超量交回过一次。"""
    if previous is None:
        return False
    return bool(previous.facts.get("overCapSeen")) or any(
        item.get("kind") == boundaries.OVER_CAP for item in previous.facts.get("blockers") or [])


def _project_checks(runtime: Runtime, context: ImplementContext, git: Git, worktree: Path,
                    this_round: Sequence[str], previous: Handoff | None, full: bool,
                    changed: Sequence[str]) -> tuple[list[CommandResult], str]:
    project = runtime.settings.project
    configured = affected.configured(project.commands if project is not None else {})
    if not configured:
        return [], FULL
    settings = CommandSettings.from_settings(runtime.settings)
    log_dir = _raw_dir(runtime, context, "commands")
    if full:
        return _run_all(runtime, [Planned(command) for command in configured], worktree, log_dir, settings), FULL
    configured = affected.selected(configured, changed, runtime.settings.section(POINT)["when"])
    patterns = project.test_patterns if project is not None else ()
    index = affected.index_tests(git.ls_files(), patterns)
    tests = affected.affected_tests(this_round, index, patterns)
    before = {item["name"]: item["result"] for item in (previous.facts.get("commands") or [])} if previous else {}
    planned = affected.plan(configured, full=False, tests=tests, changed_this_round=this_round, previous=before)
    results = _run_all(runtime, planned, worktree, log_dir, settings)
    if any(not result.passed for result in results):
        return results, AFFECTED
    rest = affected.remaining(configured, planned)
    return _merge(results, _run_all(runtime, rest, worktree, log_dir, settings)), FULL


def _run_all(runtime: Runtime, planned: Sequence[Planned], worktree: Path, log_dir: Path,
             settings: CommandSettings) -> list[CommandResult]:
    if not planned:
        return []

    def one(item: Planned) -> CommandResult:
        with runtime.slots.hold(TESTS_SLOT):
            return commands.run(item, worktree=worktree, log=log_dir / f"{item.command.name}.log",
                                runner=runtime.runner, environ=runtime.environ, settings=settings)

    with ThreadPoolExecutor(max_workers=len(planned)) as pool:
        return list(pool.map(one, planned))


def _merge(first: Sequence[CommandResult], second: Sequence[CommandResult]) -> list[CommandResult]:
    """补跑的全量结果覆盖只跑了一部分的同名结果。"""
    merged = {result.name: result for result in first}
    merged.update({result.name: result for result in second})
    return list(merged.values())


def _command_findings(results: Sequence[CommandResult]) -> list[Finding]:
    findings = []
    for result in results:
        if result.result == commands.NOT_RUN:
            findings.append(Finding("project", "not_run", result.name,
                                    f"`{result.command}` 没有跑起来：{result.reason}；日志 {result.log}", NEEDS_USER))
        elif not result.passed:
            detail = result.reason or (f"退出码 {result.exit_code}" if result.exit_code is not None else "失败")
            findings.append(Finding("project", result.result, result.name,
                                    f"`{result.command}` {detail}；日志 {result.log}", LOCAL))
    return findings


def _handoff(runtime: Runtime, context: ImplementContext, started: float, diff_hash: str, base: str,
             changes: Changes, states: Mapping[str, str], this_round: Sequence[str], scope: str,
             results: Sequence[CommandResult], findings: Sequence[Finding], outcome: RuntimeOutcome) -> Handoff:
    project = runtime.settings.project
    tests = project.test_patterns if project is not None else ()
    hints = rules.hardcode_hints(changes, test_patterns=tests, issue_text=context.body,
                                 min_length=rules.RuleSettings.from_settings(runtime.settings).min_literal_length)
    files, lines = boundaries.counted(changes.files, runtime.settings)
    facts: dict[str, Any] = {
        "base": base,
        "diffHash": diff_hash,
        "scope": scope,
        "changedFiles": list(changes.paths),
        "changedThisRound": list(this_round),
        "fileStates": dict(states),
        "counted": {"files": files, "lines": lines},
        "highRiskPaths": boundaries.high_risk(changes.paths, runtime.settings),
        "commands": [result.to_json() for result in results],
        "runtime": [item.to_json() for item in outcome.items],
        "runtimeNotes": list(outcome.notes),
        "hardcodeHints": [hint.text() for hint in hints],
        "blockers": [finding.to_json() for finding in findings],
        "firstCategory": first_category(findings),
        "overCapSeen": any(finding.kind == boundaries.OVER_CAP for finding in findings),
        "migration": outcome.migration.to_json() if outcome.migration else None,
        "awaitingScreenshots": list(outcome.awaiting_screenshots),
        "skipped": None,
        "knowledgeSuggestions": [],
    }
    status, summary = _conclusion(results, findings, outcome)
    if status is Status.PENDING:
        _pending_document(runtime, context, outcome)
    passed = sum(1 for result in results if result.passed) + sum(
        1 for item in outcome.items if item.result is Result.PASSED)
    metrics = Metrics(duration_ms=int((time.monotonic() - started) * 1000), calls=outcome.calls or None,
                      tokens=outcome.tokens if outcome.calls else None, files_changed=len(changes.files),
                      lines_changed=sum(change.added + change.deleted for change in changes.files),
                      produced={"blockers": len(findings)}, passed=passed, failed=len(findings))
    return Handoff(point=POINT, subject=context.issue.id, run=runtime.run, status=status, summary=summary,
                   facts=facts, metrics=metrics, round=context.round, created_at=format_iso(runtime.clock.now()))


def _conclusion(results: Sequence[CommandResult], findings: Sequence[Finding],
                outcome: RuntimeOutcome) -> tuple[Status, str]:
    if findings:
        category = first_category(findings)
        return Status.FAILED, f"自检有 {len(findings)} 项不通过(最靠前的一类：{category})"
    if outcome.migration is not None:
        return Status.PENDING, "改到了迁移文件，等用户确认后再启动服务检查"
    if outcome.awaiting_screenshots:
        return Status.PENDING, f"有 {len(outcome.awaiting_screenshots)} 张截图审查给不出结论，等用户查看"
    weak = [item for item in outcome.items if item.result in (Result.WEAK, Result.UNVERIFIED)]
    note = f"；{len(weak)} 项为弱证据或未验证，写进报告" if weak else ""
    return Status.PASSED, f"自检通过：{len(results)} 条项目检查{note}"


def _pending_document(runtime: Runtime, context: ImplementContext, outcome: RuntimeOutcome) -> None:
    subject = context.issue.id
    if outcome.migration is not None:
        pending(runtime, subject, point=POINT, decision="是否在测试库上执行这些新增的迁移并启动服务检查",
                options=["执行迁移并检查", "不执行"], recommendation="确认迁移可以在测试库执行后通过",
                reason=outcome.migration.text(), if_not="本机运行检查不做，接口与页面在部署后再确认",
                command=f"tightrein approve {subject}")
        return
    pending(runtime, subject, point=POINT, decision="截图中的页面布局有没有问题",
            options=["没有问题", "有问题(用 reject 写明)"], recommendation="打开截图逐张查看",
            reason="截图审查给不出结论或工具不能读图：" + "、".join(outcome.awaiting_screenshots),
            if_not="改动不能交付", command=f"tightrein approve {subject} 或 tightrein reject {subject} --note \"<问题>\"")


def _raw_dir(runtime: Runtime, context: ImplementContext, name: str) -> Path:
    """本轮自检的原始输出：`36-implement.check.r<轮>-raw/<名>/`(日志与运行产物)。
    store/files/layout 只有文件名的规则，没有步骤原始输出目录，先在这里按同一套命名算。"""
    folder = f"{step_sequence(POINT):02d}-{POINT}.r{context.round}-{RAW_SUFFIX}"
    return runtime.workspace.subject_dir(context.issue.id) / folder / name
