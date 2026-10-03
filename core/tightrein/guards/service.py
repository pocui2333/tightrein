"""运行前后的边界检查、修复分支的 diff 检查与只读锁定的恢复(architecture/02 3.3 到 3.11)。

before：生成 agent 进程的环境；worktree 中有凭证文件或仓库本地配置带凭证、git 状态无法读取、只读锁定失败时抛出
GuardBlocked，不启动 agent；否则记录 git 状态、workdir 与不可写路径的文件快照，只读任务(交互会话除外)锁定 worktree。
同一 worktree 上已有锁定标记时，持有进程仍在运行则不启动(同一只读 worktree 同一时间只允许一个 agent 运行)，
持有进程已不存在则先按标记恢复再锁定，避免把只读状态当作原有权限记下来。
after：先恢复只读锁定，再比较 git 状态与文件快照，按访问级别判定改动范围，检查受保护文件、测试改动与隐藏路径的读取；
交互会话只检查 git 状态与隐藏路径的读取。所有违规一并报告，不抛异常，不回滚 agent 的改动。
check_diff：修复分支相对基准 commit 的全部改动执行 diff 规则与改动量上限；不可写路径的比较以 fix 在修复开始时
经 snapshot_forbidden 记录的快照为基准，没有基准时不做这一项。
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from tightrein.domain.clock import Clock
from tightrein.domain.enums import ViolationKind
from tightrein.guards import diff_rules, file_state, git_state, readonly
from tightrein.guards.credentials import build_env, config_credentials, credential_files
from tightrein.guards.file_state import Changes, Snapshot
from tightrein.guards.git_state import GitSnapshot
from tightrein.guards.policy import GuardSettings, forbidden_paths, hidden_paths, skipped
from tightrein.guards.protected import matching_pattern
from tightrein.guards.report import GuardBlocked, GuardReport, Violation, summary, write_report
from tightrein.guards.readonly import ReadonlyError, ReadonlyRestoreError
from tightrein.observability.tracing import Tracer
from tightrein.runner.task import RunnerTask
from tightrein.store.files.layout import ToolLayout, WorkspaceLayout
from tightrein.store.locks import process_alive
from tightrein.vcs.errors import VcsError
from tightrein.vcs.git_read import GitReader

HEAD = "HEAD"
TOKEN_SEPARATORS = re.compile(r"[\s'\"`=,;|&<>()]+")
DECISION_PASS = "pass"
DECISION_VIOLATION = "violation"
DECISION_RECOVERED = "readonly-recovered"
DECISION_RESTORE_FAILED = "readonly-restore-failed"


class ReadonlyHeld(Exception):
    """同一只读 worktree 正被另一个仍在运行的进程锁定。"""


@dataclass
class GuardContext:
    """before 的结果：agent 进程的环境与运行前的快照，交给 after 比较。"""

    env: dict[str, str]
    removed_env_names: tuple[str, ...]
    report_path: Path
    git_before: GitSnapshot
    files_before: Snapshot
    forbidden_before: dict[str, Snapshot]
    marker_path: Path | None = None


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


# 只提交最终结果、不访问文件的工具；其参数是结论正文，正文中引用的代码(例如 `it.key`)不是路径
OUTPUT_ONLY_TOOLS = frozenset({"StructuredOutput"})


def hidden_reads(tool_calls: Iterable[Mapping[str, Any]], workdir: Path, hidden: Sequence[Path],
                 credential_patterns: Sequence[str]) -> list[Violation]:
    """会话记录中的工具调用(统一事件中 type 为 tool-call 的)读取了隐藏路径或凭证文件的，每个路径报告一次。

    凭证文件按文件名模式匹配，只有解析后的路径确实存在时才算读取：搜索文本或代码片段中形如 `it.key` 的 token
    并不指向文件。只提交结果的工具(OUTPUT_ONLY_TOOLS)不检查。
    """
    roots = [os.path.realpath(path) for path in hidden]
    found: dict[str, Violation] = {}
    for call in tool_calls:
        if call.get("toolName") in OUTPUT_ONLY_TOOLS:
            continue
        for text in _strings(call.get("toolInput")):
            for token in TOKEN_SEPARATORS.split(text):
                if not token or token in found or ("/" not in token and "." not in token):
                    continue
                resolved = os.path.realpath(token if os.path.isabs(token) else workdir / token)
                inside = any(resolved == root or resolved.startswith(root + os.sep) for root in roots)
                pattern = matching_pattern(PurePosixPath(token).as_posix(), credential_patterns)
                if pattern is not None and not os.path.exists(resolved):
                    pattern = None
                if inside or pattern is not None:
                    reason = "隐藏目录" if inside else f"凭证文件(匹配 {pattern})"
                    found[token] = Violation(ViolationKind.HIDDEN_PATH_READ, token,
                                             f"{call.get('toolName') or '工具'} 访问了{reason}")
    return list(found.values())


class Guards:
    def __init__(
        self,
        git: GitReader,
        settings: GuardSettings,
        layout: WorkspaceLayout,
        tool: ToolLayout,
        *,
        tracer: Tracer | None = None,
        chmod: readonly.Chmod | None = None,
        alive: Callable[[int], bool] = process_alive,
    ) -> None:
        self.git = git
        self.settings = settings
        self.layout = layout
        self.tool = tool
        self.tracer = tracer
        self.chmod = chmod or readonly.set_mode
        self.alive = alive

    # 快照

    def snapshot_forbidden(self) -> dict[str, Snapshot]:
        """工作区中 agent 不可写的路径的快照；跳过虚拟环境、缓存与依赖目录。"""
        snapshots: dict[str, Snapshot] = {}
        for root in forbidden_paths(self.layout, self.tool):
            paths = [path for path in file_state.walk(root, lambda name: skipped((name,)))
                     if not skipped(PurePosixPath(path).parts)]
            snapshots[str(root)] = file_state.snapshot_files(root, paths)
        return snapshots

    def _forbidden_changes(self, before: Mapping[str, Snapshot]) -> list[Violation]:
        after = self.snapshot_forbidden()
        violations = []
        for root, snapshot in sorted(before.items()):
            changes = file_state.compare(snapshot, after.get(root, {}))
            for path in changes.paths:
                shown = root if path == "." else str(Path(root) / path)
                violations.append(Violation(ViolationKind.FORBIDDEN_PATH_MODIFIED, shown, "agent 不可写的路径被修改"))
        return violations

    def _workdir_snapshot(self, workdir: Path, previous: Snapshot | None = None) -> Snapshot:
        paths = set(self.git.files(workdir)) | set(previous or {})
        return file_state.snapshot_files(workdir, sorted(paths), previous)

    # 运行前

    def before(self, task: RunnerTask, *, clock: Clock, base_env: Mapping[str, str],
               extra_env_names: Iterable[str] = ()) -> GuardContext:
        workdir = task.workdir
        report_path = self.layout.guard_report(task.run_id, task.role, task.subject_id)
        agent = build_env(base_env, extra_names=extra_env_names, readonly=task.readonly)
        violations: list[Violation] = []
        snapshot: GitSnapshot | None = None
        files: Snapshot = {}
        try:
            violations += credential_files(self.git, workdir, self.settings.credential_files)
            violations += config_credentials(self.git, workdir)
            snapshot = git_state.take(self.git, workdir)
            files = self._workdir_snapshot(workdir)
        except VcsError as error:
            violations.append(Violation(ViolationKind.GIT_UNREADABLE, str(workdir), str(error)))
        if violations or snapshot is None:
            raise self._blocked(report_path, violations, agent.removed_names)
        context = GuardContext(agent.env, agent.removed_names, report_path, snapshot, files, self.snapshot_forbidden())
        if task.readonly and not task.interactive:
            marker_path = self.layout.readonly_guard(workdir.name)
            try:
                self._release_stale(marker_path)
                readonly.lock(workdir, marker_path, clock, run_id=task.run_id, chmod=self.chmod)
            except (ReadonlyError, ReadonlyHeld) as error:
                violation = Violation(ViolationKind.READONLY_MODIFIED, str(workdir), str(error))
                raise self._blocked(report_path, [violation], agent.removed_names) from error
            context.marker_path = marker_path
        return context

    def _release_stale(self, marker_path: Path) -> None:
        """同一 worktree 上已有锁定：持有进程仍在运行时不再锁定，已不存在时先按标记恢复。"""
        if not marker_path.exists():
            return
        marker = readonly.read_marker(marker_path)
        if self.alive(marker.pid):
            raise ReadonlyHeld(f"只读 worktree 正被进程 {marker.pid}(运行 {marker.run_id})锁定，等它结束后再运行")
        readonly.restore(marker_path, chmod=self.chmod)

    def _blocked(self, report_path: Path, violations: list[Violation], removed: tuple[str, ...]) -> GuardBlocked:
        """写报告与 gate 事件，返回由调用方抛出的 GuardBlocked。"""
        report = GuardReport(tuple(violations), removed_env_names=removed, report_path=report_path)
        write_report(report_path, report, {"phase": "before"})
        self._gate(report)
        return GuardBlocked(violations)

    # 运行后

    def after(self, task: RunnerTask, context: GuardContext,
              tool_calls: Iterable[Mapping[str, Any]] = ()) -> GuardReport:
        restore_error = None
        if context.marker_path is not None:
            try:
                readonly.restore(context.marker_path, chmod=self.chmod)
            except ReadonlyRestoreError as error:
                restore_error = str(error)
                if self.tracer is not None:
                    self.tracer.event("gate", decision=DECISION_RESTORE_FAILED, reason=restore_error)
        workdir = task.workdir
        violations: list[Violation] = []
        try:
            after_git = git_state.take(self.git, workdir)
            violations += git_state.compare(context.git_before, after_git, self.git, workdir)
        except VcsError as error:
            after_git = None
            violations.append(Violation(ViolationKind.GIT_UNREADABLE, str(workdir), str(error)))
        changed: tuple[str, ...] = ()
        added = removed = 0
        if not task.interactive:
            violations += self._forbidden_changes(context.forbidden_before)
            try:
                changes = file_state.compare(context.files_before,
                                             self._workdir_snapshot(workdir, context.files_before))
                changed = changes.paths
                scope, added, removed = self._scope(task, changes)
                violations += scope
            except VcsError as error:
                violations.append(Violation(ViolationKind.GIT_UNREADABLE, str(workdir), str(error)))
        violations += hidden_reads(tool_calls, workdir, hidden_paths(self.layout), self.settings.credential_files)
        report = GuardReport(tuple(violations), changed, added, removed, context.removed_env_names,
                             context.report_path, restore_error)
        write_report(context.report_path, report, {
            "phase": "after",
            "gitBefore": context.git_before.to_dict(),
            "gitAfter": None if after_git is None else after_git.to_dict(),
        })
        self._gate(report)
        return report

    def _scope(self, task: RunnerTask, changes: Changes) -> tuple[list[Violation], int, int]:
        if not changes:
            return [], 0, 0
        details = diff_rules.collect_changes(self.git, task.workdir, HEAD, changes.paths)
        added = sum(item.lines_added for item in details)
        removed = sum(item.lines_removed for item in details)
        if task.readonly:
            return [Violation(ViolationKind.READONLY_MODIFIED, path, "只读 worktree 中的文件被修改")
                    for path in changes.paths], added, removed
        violations = diff_rules.protected_violations(details, self.settings, task.approved_protected_paths)
        if task.tests_only:
            tests = {item.path for item in diff_rules.modified_test_violations(details, self.settings)}
            violations += [Violation(ViolationKind.OUTSIDE_PLAN, item.path, "写复现测试的任务只能改动测试文件(testPaths)")
                           for item in details if item.path not in tests]
        else:
            violations += diff_rules.modified_test_violations(details, self.settings)
        return violations, added, removed

    # 修复分支的 diff 规则

    def check_diff(self, worktree: Path, base_commit: str, *, issue_id: str,
                   approved_protected_paths: Sequence[str] = (),
                   forbidden_baseline: Mapping[str, Snapshot] | None = None) -> GuardReport:
        changes = diff_rules.collect_changes(self.git, worktree, base_commit)
        violations = diff_rules.size_violations(changes, self.settings)
        violations += diff_rules.protected_violations(changes, self.settings, approved_protected_paths)
        violations += diff_rules.modified_test_violations(changes, self.settings)
        violations += diff_rules.skip_violations(changes, self.settings)
        length = self.settings.min_literal_length
        violations += diff_rules.hardcode_violations(
            changes, diff_rules.reproduction_literals(self.layout, issue_id, length), length)
        if forbidden_baseline is not None:
            violations += self._forbidden_changes(forbidden_baseline)
        report = GuardReport(
            tuple(violations), tuple(item.path for item in changes), sum(item.lines_added for item in changes),
            sum(item.lines_removed for item in changes),
        )
        self._gate(report)
        return report

    # 恢复

    def recover(self) -> list[Path]:
        """启动时调用：恢复上次异常退出遗留的只读锁定，返回已恢复的 worktree。"""
        markers = readonly.recover(self.layout.data_dir() / "guards", alive=self.alive, chmod=self.chmod)
        for marker in markers:
            if self.tracer is not None:
                self.tracer.event("gate", decision=DECISION_RECOVERED, reason=f"已恢复 {marker.worktree} 的写权限")
        return [marker.worktree for marker in markers]

    def _gate(self, report: GuardReport) -> None:
        if self.tracer is None:
            return
        decision = DECISION_PASS if report.ok else DECISION_VIOLATION
        self.tracer.event("gate", decision=decision, reason=summary(report.violations) or None,
                          artifact=None if report.report_path is None else str(report.report_path))
