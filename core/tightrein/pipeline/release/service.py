"""ReleaseService(architecture/07 第 17 到 21 章)：release、commit、sync、push、pr、track、summary、cleanup、comment。

每个 git 写操作经 vcs.operations.OperationPlanner 生成一个待确认操作，由用户确认后执行；执行成功后 follow_up 记录结果、
推进 Issue 状态。关卡 gates.release-writes 为 auto 的项目，提交、合并主干、推送与提 PR 生成后直接执行(vcs/unattended.py)，
release 随之继续，Issue 离开提交阶段(合并主干后回到合并前验证、提 PR 后进入待合并)即停。
Issue 有 GitHub 镜像时 PR 描述末尾写 `Closes #<编号>`，合并后 GitHub 自动关闭镜像。缺省不合并 PR、不涉及生产发布，
之后只读跟踪 PR、部署与进入 master；关卡 gates.merge 为 auto 时 track 对打开的 PR 判断合并条件(steps/auto_merge.py)，
满足即合并并删除远程修复分支，不满足的原因记进交接文档 autoMerge。交接文档 release-<编号>.json
每次在上一版的基础上累积。--output 模式只渲染提交信息、文件清单、冲突报告、PR 标题与描述，不生成也不执行任何操作。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import tzinfo
from pathlib import Path
from typing import Any

from tightrein.config.project import ProjectConfig
from tightrein.domain import issue_sections
from tightrein.domain.clock import Clock, format_iso
from tightrein.domain.issue import in_phase
from tightrein.domain.enums import (
    DeploymentStatus,
    HandoffStatus,
    IssueEvent,
    IssuePhase,
    IssueStatus,
    OperationKind,
    OperationStatus,
    RunStage,
    VerifyPhase,
)
from tightrein.observability.events import EventLog
from tightrein.config import gates
from tightrein.config.gates import Gate
from tightrein.pipeline.checks import ci
from tightrein.pipeline.common import conventions, deploys, stage_runs
from tightrein.pipeline.issue.steps import edit, github, transitions
from tightrein.pipeline.issue.steps.transitions import IssueEnv
from tightrein.domain import release_format
from tightrein.pipeline.fix.render import documents as fix_documents
from tightrein.pipeline.fix.steps import repro_test
from tightrein.store.files import documents as handoff_documents
from tightrein.pipeline.release.render import commit_list, conflicts, pr_comment
from tightrein.pipeline.release.render import summary as summary_render
from tightrein.pipeline.release.steps import cleanup as cleanup_step
from tightrein.pipeline.release.steps import commit, precheck, pull_request, push, sync, track_deploy, track_master
from tightrein.pipeline.release.steps import auto_merge, track_pr, work_summary
from tightrein.store.files import atomic, issue_files
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.repos import deployments, issues, pending_operations, pulls
from tightrein.store.repos.deployments import Deployment
from tightrein.store.repos.pulls import PullRecord
from tightrein.vcs.errors import VcsError
from tightrein.vcs import gh_issues, operations, unattended
from tightrein.vcs.executor import FollowUp
from tightrein.vcs.operations import PendingOperation

STAGE = RunStage.RELEASE
ACTOR = "release"
PR_EVENT = "release-pr"
CONFLICT_EVENT = "release-pr-conflict"
DEPLOY_EVENT = "release-deploy-failed"
MANUAL_EVENT = "release-manual-deploy"
TRACKED = (IssueStatus.PENDING_MERGE, IssueStatus.DONE)
MERGE_KINDS = frozenset({OperationKind.MERGE_MAIN, OperationKind.COMMIT_MERGE})
CONFLICTS_REPORT = "conflicts.md"
CONFLICT_FILES = "conflict-files.json"
AUTO_MERGE_REASON = "gates.merge 为 auto，且满足全部合并条件"
MERGE_DECISION = "merge-decision.md"
MERGE_DECISION_EVENT = "release-merge-decision"
REVERT_EVENT = "release-revert"


@dataclass(frozen=True)
class ReleaseResult:
    issue_id: str
    status: HandoffStatus
    message: str
    operation: str | None = None
    path: Path | None = None


@dataclass
class TrackReport:
    lines: list[str]
    skipped: list[tuple[str, str]]
    summary: str


@dataclass
class ReleaseDeps:
    layout: WorkspaceLayout
    config: ProjectConfig
    conn: sqlite3.Connection
    clock: Clock
    events: EventLog
    git: Any
    planner: Any = None
    gh: Any = None
    notifier: Any = None
    zone: tzinfo | None = None
    operations: Any = None
    branch_prefix: str | None = None
    deploys: Any = None  # pipeline/common/deploys.DeploySource


class ReleaseService:
    def __init__(self, deps: ReleaseDeps) -> None:
        self.deps = deps

    # 公共部分

    @property
    def output_mode(self) -> bool:
        return self.deps.layout.output_dir is not None

    def _env(self) -> IssueEnv:
        return IssueEnv(self.deps.conn, self.deps.layout, self.deps.clock, self.deps.config, self.deps.zone)

    def _issue(self, issue_id: str) -> Any:
        return transitions.record_of(self._env(), issue_id).issue

    def _worktree(self, issue_id: str) -> Path:
        return self.deps.layout.fix_worktree(issue_id)

    def _file(self, issue_id: str, name: str) -> Path:
        if self.output_mode:
            return self.deps.layout.output_path(name)
        return self.deps.layout.fix_file(issue_id, name)

    def _latest(self, stage: RunStage, issue_id: str, phase: VerifyPhase | None = None) -> Mapping[str, Any] | None:
        found = stage_runs.latest_outputs(self.deps.conn, self.deps.layout, stage, issue_id, phase)
        return None if found is None else found[1]

    def outputs(self, issue_id: str) -> dict[str, Any]:
        """在上一版的基础上累积的交接文档 outputs。"""
        previous = self._latest(STAGE, issue_id)
        if previous is not None:
            return dict(previous)
        return {"issueId": issue_id, "branch": self._issue(issue_id).branch or "", "commits": [], "syncs": [],
                "push": None, "pr": None, "deployments": [], "masterAt": None, "pendingOperations": [],
                "acceptedFindings": [], "cleanup": None}

    def _save(self, issue_id: str, outputs: Mapping[str, Any], status: HandoffStatus, next_action: str,
              reason: str | None = None) -> Path:
        deps = self.deps
        run = stage_runs.begin(STAGE, deps.layout, deps.conn, deps.clock, deps.events)
        path = run.handoff(STAGE, issue_id, status, outputs, next_action, reason)
        run.end(status)
        return path

    def _pending(self, issue_id: str, operation: PendingOperation, message: str) -> ReleaseResult:
        if unattended.release_direct(self.deps.config, operation.kind):
            return self._direct(issue_id, operation, message)
        outputs = self.outputs(issue_id)
        outputs["pendingOperations"] = [*outputs["pendingOperations"], operation.id]
        path = self._save(issue_id, outputs, HandoffStatus.BLOCKED, f"确认 {operation.id}：tightrein confirm "
                          f"{operation.id}", f"等待确认 {operation.id}")
        return ReleaseResult(issue_id, HandoffStatus.BLOCKED, f"{message}\n{operations.describe(operation)}",
                             operation.id, path)

    def _direct(self, issue_id: str, operation: PendingOperation, message: str) -> ReleaseResult:
        """直接执行；成功时由 follow_up 记录结果，失败或过期时停下并带回原因(与用户确认后执行的结果相同)。"""
        result = self.deps.operations.run_unattended(operation.id, reason=unattended.RELEASE_REASON,
                                                     clock=self.deps.clock)
        executed = result.operation
        if executed.status is OperationStatus.EXECUTED:
            return ReleaseResult(issue_id, HandoffStatus.OK, f"{message}\n已直接执行 {operation.id}"
                                 f"({operation.kind.label})")
        detail = str(result.error) if result.error is not None else json.dumps(executed.result, ensure_ascii=False)
        status = HandoffStatus.FAILED if executed.status is OperationStatus.FAILED else HandoffStatus.BLOCKED
        return ReleaseResult(issue_id, status, f"{operation.id}({operation.kind.label}){executed.status.label}："
                             f"{detail}")

    def _conventions(self) -> conventions.Conventions:
        return conventions.resolve(self.deps.config, self.deps.config.repo)

    def _slug(self) -> str | None:
        urls = self.deps.git.remotes(self.deps.config.repo).get(github.ORIGIN_URL) or ()
        return gh_issues.repo_slug(urls[0]) if urls else None

    def _relations(self, issue: Any) -> list[str]:
        """PR 描述的关联：关闭镜像 Issue、父 Issue 与排在前面的子任务。"""
        lines = []
        if issue.github is not None:
            lines.append(github.closes_line(issue.github, self._slug()))
        for label, other in (("父 Issue", issue.parent), ("排在之后的子任务，前一个", issue.depends_on)):
            found = issues.get(self.deps.conn, other) if other is not None else None
            if found is not None:
                link = f"#{found.issue.github.number}" if found.issue.github is not None else f"Issue {other}"
                lines.append(f"{label}：{link}" + (f"(PR {found.issue.pr})" if found.issue.pr else ""))
        return lines

    def _history(self, issue_id: str, text: str, updates: Mapping[str, object] | None = None) -> None:
        if not self.output_mode:
            transitions.annotate(self._env(), issue_id, text, updates=updates)

    def _event(self, issue_id: str, event: IssueEvent, note: str, updates: Mapping[str, object] | None = None) -> None:
        if not self.output_mode:
            transitions.apply_event(self._env(), transitions.record_of(self._env(), issue_id), event, actor=ACTOR,
                                    note=note, updates=updates)

    # release：从当前进度连续推进到下一个待确认操作

    def release(self, issue_id: str, *, accept_findings: bool = False) -> ReleaseResult:
        issue = self._issue(issue_id)
        if not in_phase(issue, IssuePhase.SUBMIT):
            return ReleaseResult(issue_id, HandoffStatus.BLOCKED, f"Issue 当前为「{issue.status.label}」，合并前验证通过后才能发布")
        worktree = self._worktree(issue_id)
        steps: list[Callable[[str], ReleaseResult]] = [self.sync, self.push, self.pr]
        if not self.deps.git.status(worktree).clean:
            steps.insert(0, lambda subject: self.commit(subject, accept_findings=accept_findings))
        for step in steps:
            result = step(issue_id)
            if result.operation is not None or result.status is not HandoffStatus.OK or self.output_mode \
                    or not in_phase(self._issue(issue_id), IssuePhase.SUBMIT):
                return result
        return ReleaseResult(issue_id, HandoffStatus.OK, "PR 已是最新，等待用户在 GitHub 上审核")

    # 提交

    def commit(self, issue_id: str, *, accept_findings: bool = False) -> ReleaseResult:
        deps = self.deps
        issue = self._issue(issue_id)
        fix = stage_runs.latest_outputs(deps.conn, deps.layout, RunStage.FIX, issue_id)
        checked = precheck.check(issue, fix, accept_findings)
        if not checked.ok:
            path = None
            if fix is not None and not self.output_mode:
                path = self._save(issue_id, {**self.outputs(issue_id)}, HandoffStatus.BLOCKED,
                                  f"处理后重跑 release commit {issue_id}", checked.reason)
            return ReleaseResult(issue_id, HandoffStatus.BLOCKED, checked.reason or "", path=path)
        worktree = self._worktree(issue_id)
        changed = deps.git.status(worktree).changed_paths
        if not changed:
            return ReleaseResult(issue_id, HandoffStatus.OK, "工作区没有改动，不需要提交")
        files, differences = commit.files(changed, fix[1])
        if differences:
            return ReleaseResult(issue_id, HandoffStatus.BLOCKED, "apply 之后工作区有变化：" + "；".join(differences)
                                 + f"；先执行 fix apply {issue_id} --review-only")
        message = commit.message(deps.config, self._conventions(), issue, fix[1])
        text = commit_list.render(message, files, checked.accepted)
        if self.output_mode:
            atomic.write_text(self._file(issue_id, "commit.md"), text)
            return ReleaseResult(issue_id, HandoffStatus.OK, text)
        if checked.accepted:
            outputs = self.outputs(issue_id)
            outputs["acceptedFindings"] = list(checked.accepted)
            self._save(issue_id, outputs, HandoffStatus.OK, "确认提交")
            self._history(issue_id, "照常提交，接受的未通过项：" + "；".join(
                f"{item['check']} {item['problem']}" for item in checked.accepted))
        operation = deps.planner.plan_commit(issue_id, worktree, message, files)
        return self._pending(issue_id, operation, text)

    # 同步主干

    def sync(self, issue_id: str, *, cont: bool = False, abort: bool = False) -> ReleaseResult:
        deps = self.deps
        worktree = self._worktree(issue_id)
        if abort:
            operation = deps.planner.plan_abort_merge(issue_id, worktree)
            return self._pending(issue_id, operation, "放弃合并会丢弃合并中的全部改动，回到合并前的状态")
        if deps.git.merge_head(worktree) is not None:
            return self._continue(issue_id, worktree) if cont else self._conflicts(issue_id, worktree)
        if cont:
            return ReleaseResult(issue_id, HandoffStatus.BLOCKED, "没有进行中的合并")
        main = deps.config.main_branch
        deps.git.fetch(worktree)
        incoming = deps.git.log(worktree, f"HEAD..origin/{main}")
        if not incoming:
            return ReleaseResult(issue_id, HandoffStatus.OK, f"origin/{main} 没有新提交")
        if not deps.git.status(worktree).clean:
            return ReleaseResult(issue_id, HandoffStatus.BLOCKED, "工作区不干净，先提交或处理改动后再同步主干")
        fix = self._latest(RunStage.FIX, issue_id) or {}
        ours = {item["path"] for item in fix.get("changedFiles") or []}
        theirs = set(deps.git.diff(worktree, "HEAD", f"origin/{main}").paths)
        listing = "\n".join(f"- {item.commit[:12]} {item.subject}" for item in incoming)
        overlap = "、".join(sorted(ours & theirs)) or "无"
        message = f"origin/{main} 上的新提交：\n{listing}\n与本修复改动文件的交集：{overlap}"
        if self.output_mode:
            return ReleaseResult(issue_id, HandoffStatus.OK, message)
        operation = deps.planner.plan_merge_main(issue_id, worktree)
        return self._pending(issue_id, operation, message)

    def _conflicts(self, issue_id: str, worktree: Path) -> ReleaseResult:
        git = self.deps.git
        files = [conflicts.ConflictFile(path, self._side_log(worktree, "MERGE_HEAD..HEAD", path),
                                        self._side_log(worktree, "HEAD..MERGE_HEAD", path),
                                        sync.hunks((worktree / path).read_text(encoding="utf-8", errors="replace")))
                 for path in git.conflict_files(worktree)]
        path = self._file(issue_id, CONFLICTS_REPORT)
        atomic.write_text(path, conflicts.render(issue_id, files))
        atomic.write_text(self._file(issue_id, CONFLICT_FILES), json.dumps([item.path for item in files]) + "\n")
        reason = f"合并 origin/main 有冲突：{'、'.join(item.path for item in files)}；冲突报告 {path}"
        if not self.output_mode:
            self._save(issue_id, self.outputs(issue_id), HandoffStatus.BLOCKED, f"解决冲突后 release sync {issue_id} "
                       "--continue，或 --abort 放弃合并", reason)
        return ReleaseResult(issue_id, HandoffStatus.BLOCKED, reason, path=path)

    def _side_log(self, worktree: Path, rev_range: str, path: str) -> str:
        found = self.deps.git.log(worktree, rev_range, [path])
        return "\n".join(f"{item.commit[:12]} {item.subject}" for item in found)

    def _continue(self, issue_id: str, worktree: Path) -> ReleaseResult:
        deps = self.deps
        files = sorted(set(deps.git.conflict_files(worktree)) | set(self._recorded_conflicts(issue_id)))
        left = sync.markers_left(worktree, files)
        if left:
            return ReleaseResult(issue_id, HandoffStatus.BLOCKED, f"还有冲突标记：{'、'.join(left)}")
        choices = [{"file": path, "resolution": sync.resolution(
            deps.git.show(worktree, "HEAD", path), deps.git.show(worktree, "MERGE_HEAD", path),
            (worktree / path).read_text(encoding="utf-8", errors="replace"))} for path in files]
        outputs = self.outputs(issue_id)
        outputs["syncs"] = [*outputs["syncs"], {"mainCommit": deps.git.merge_head(worktree), "conflicts": choices,
                                                "mergeCommit": None}]
        self._save(issue_id, outputs, HandoffStatus.OK, "确认合并提交")
        operation = deps.planner.plan_commit_merge(issue_id, worktree, files)
        listing = "\n".join(f"- {item['file']}：{item['resolution']}" for item in choices)
        return self._pending(issue_id, operation, f"冲突解决的取舍：\n{listing}")

    def _recorded_conflicts(self, issue_id: str) -> list[str]:
        """生成冲突报告时记录的冲突文件；用户 git add 之后它们不再显示为冲突。"""
        path = self._file(issue_id, CONFLICT_FILES)
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []

    # 推送

    def push(self, issue_id: str) -> ReleaseResult:
        deps = self.deps
        issue = self._issue(issue_id)
        worktree = self._worktree(issue_id)
        main = deps.config.main_branch
        deps.git.fetch(worktree)
        if deps.git.log(worktree, f"HEAD..origin/{main}"):
            return ReleaseResult(issue_id, HandoffStatus.BLOCKED, f"origin/{main} 有新提交，先执行 release sync {issue_id}")
        pending = push.commits_to_push(deps.git, worktree, issue.branch, main)
        if not pending:
            return ReleaseResult(issue_id, HandoffStatus.OK, "没有需要推送的提交")
        if self.output_mode:
            return ReleaseResult(issue_id, HandoffStatus.OK, pending)
        operation = deps.planner.plan_push(issue_id, worktree, issue.branch)
        return self._pending(issue_id, operation, f"将推送到 origin/{issue.branch} 的提交：\n{pending}")

    # 提 PR

    def pr(self, issue_id: str) -> ReleaseResult:
        deps = self.deps
        record = transitions.record_of(self._env(), issue_id)
        issue = record.issue
        branch = issue.branch or ""
        found = self._conventions()
        problem = pull_request.branch_problem(branch, found, deps.config)
        if problem is not None:
            suggestion = pull_request.suggest(issue, deps.branch_prefix, found, deps.config)
            return ReleaseResult(issue_id, HandoffStatus.BLOCKED, f"{problem}；建议改名为 {suggestion}，确认后改名再提 PR")
        worktree = self._worktree(issue_id)
        deps.git.fetch(worktree)
        document = issue_files.read(deps.layout.root / record.path)
        fix = self._latest(RunStage.FIX, issue_id) or {}
        release = self.outputs(issue_id)
        checks = pull_request.verification(fix, repro_test.load(deps.layout.fixes_dir(issue_id)),
                                           self._latest(RunStage.VERIFY, issue_id, VerifyPhase.LOCAL),
                                           release.get("acceptedFindings") or [])
        pull = pull_request.text(edit.sections(document.body), fix, self._relations(issue), checks)
        body = pull_request.body(found, pull)
        title = pull_request.title(issue, fix)
        path = self._file(issue_id, "pr-body.md")
        atomic.write_text(path, body)
        if self.output_mode:
            atomic.write_text(self._file(issue_id, "pr-title.txt"), title + "\n")
            return ReleaseResult(issue_id, HandoffStatus.OK, f"{title}\n\n{body}", path=path)
        operation = deps.planner.plan_pull_request(issue_id, worktree, branch, title, body)
        if operation is None:
            return ReleaseResult(issue_id, HandoffStatus.OK, "PR 描述没有变化，不需要更新", path=path)
        return self._pending(issue_id, operation, f"标题：{title}\n目标分支：{deps.config.main_branch}\n描述：{path}")

    # 执行后续

    def follow_up(self) -> FollowUp:
        return FollowUp(executed=self.on_executed)

    def on_executed(self, operation: PendingOperation) -> None:
        issue_id = operation.subject_id
        result = dict(operation.result or {})
        outputs = self.outputs(issue_id)
        outputs["pendingOperations"] = [item for item in outputs["pendingOperations"] if item != operation.id]
        if operation.kind is OperationKind.COMMIT:
            message = operation.description.text.removeprefix("提交信息：\n")
            outputs["commits"] = [*outputs["commits"], {"commit": result["commit"], "message": message.strip(),
                                                        "files": list(operation.description.files),
                                                        "operationId": operation.id}]
            self._history(issue_id, f"提交 {result['commit'][:12]}：{message.strip()}")
        elif operation.kind in MERGE_KINDS:
            if outputs["syncs"] and outputs["syncs"][-1]["mergeCommit"] is None and operation.kind is \
                    OperationKind.COMMIT_MERGE:
                outputs["syncs"][-1] = {**outputs["syncs"][-1], "mergeCommit": result["commit"]}
            else:
                main = operation.preconditions["state"].get("originMain") or result["commit"]
                outputs["syncs"] = [*outputs["syncs"], {"mainCommit": main, "conflicts": [],
                                                        "mergeCommit": result["commit"]}]
            self._event(issue_id, IssueEvent.MAIN_MERGED, f"合并 origin/main 于 {result['commit'][:12]}，需要重新执行合并前验证")
        elif operation.kind is OperationKind.PUSH:
            outputs["push"] = {"remoteBranch": f"origin/{result['branch']}", "commits": [result["commit"]],
                               "at": format_iso(self.deps.clock.now())}
            self._history(issue_id, f"推送 {result['commit'][:12]} 到 origin/{result['branch']}")
        elif operation.kind is OperationKind.PULL_REQUEST:
            self._pull_created(issue_id, operation, result, outputs)
        elif operation.kind is OperationKind.ABORT_MERGE:
            self._history(issue_id, "放弃合并 origin/main")
        elif operation.kind is OperationKind.REVERT_PULL_REQUEST:
            url = ((operation.result or {}).get("outputs") or [""])[-1].strip()
            outputs["revert"] = {**(outputs.get("revert") or {}), "url": url or None}
            self._history(issue_id, f"已提撤销 PR：{url}")
            if self.deps.notifier is not None:
                self.deps.notifier.notify(REVERT_EVENT, issue_id, f"Issue {issue_id} 的撤销 PR 等待用户决定：{url}")
        elif operation.kind is OperationKind.PR_COMMENT:
            self._history(issue_id, "AI 评审结论已写入 PR 评论")
        elif operation.kind is OperationKind.CLEANUP:
            outputs["cleanup"] = {"operationId": operation.id, "status": operation.status.value}
            self._history(issue_id, "已删除修复 worktree 与本地分支")
        self._save(issue_id, outputs, HandoffStatus.OK, "继续 release")

    def _pull_created(self, issue_id: str, operation: PendingOperation, result: Mapping[str, Any],
                      outputs: dict[str, Any]) -> None:
        deps = self.deps
        url = result["url"]
        number = int(url.rstrip("/").rsplit("/", 1)[-1])
        issue = self._issue(issue_id)
        body = self._file(issue_id, "pr-body.md").read_text(encoding="utf-8")
        title = pull_request.title(issue, self._latest(RunStage.FIX, issue_id) or {})
        existing = pulls.get(deps.conn, issue_id)
        pulls.save(deps.conn, PullRecord(issue_id, number, url, issue.branch or "", title, "OPEN",
                                         existing.created_at if existing is not None else deps.clock.now()))
        outputs["pr"] = {"number": number, "url": url, "title": title,
                         "bodySha256": hashlib.sha256(body.encode("utf-8")).hexdigest(), "state": "open",
                         "mergeable": None, "mergeCommit": None, "mergedAt": None, "closedAt": None, "userNote": None}
        if in_phase(issue, IssuePhase.SUBMIT):
            self._event(issue_id, IssueEvent.PR_CREATED, f"PR #{number}：{url}", updates={"pr": url})
        else:
            self._history(issue_id, f"更新 PR #{number} 的描述")
        if not self.output_mode:
            self._review_comment(issue_id, number, outputs)
        if deps.notifier is not None:
            deps.notifier.notify(PR_EVENT, issue_id, f"Issue {issue_id} 的 PR 等待审核：{url}")

    # 跟踪 PR、部署与生产发布

    def track(self) -> TrackReport:
        deps = self.deps
        lines: list[str] = []
        skipped: list[tuple[str, str]] = []
        fetched = False
        for record in [item for status in TRACKED for item in issues.find(deps.conn, status=status)]:
            issue = record.issue
            pull = pulls.get(deps.conn, issue.id)
            if pull is None or (issue.status is IssueStatus.DONE and issue.phase is None
                                and (pull.master_at is not None or pull.merge_commit is None)):
                continue
            try:
                if issue.status is IssueStatus.PENDING_MERGE:
                    lines += self._track_pull(issue.id, pull)
                    pull = pulls.get(deps.conn, issue.id)
                if in_phase(self._issue(issue.id), IssuePhase.DEPLOY_CHECK):
                    lines += self._track_deploy(issue.id, pull)
                if pull.merge_commit is not None and pull.master_at is None:
                    if not fetched:
                        deps.git.fetch(deps.config.repo)
                        fetched = True
                    lines += self._track_master(issue.id, pull)
            except VcsError as error:
                skipped.append((issue.id, f"只读查询失败：{error}"))
        return TrackReport(lines, skipped, summary_render.render(lines, skipped))

    def _track_pull(self, issue_id: str, pull: PullRecord) -> list[str]:
        deps = self.deps
        now = deps.clock.now()
        found = deps.gh.pr_view(deps.config.repo, str(pull.number))
        merge = self._auto_merge(issue_id, pull) if found.state == track_pr.OPEN and bool(
            gates.auto(deps.config, Gate.MERGE)) else None
        if merge is not None and merge["merged"]:
            found = deps.gh.pr_view(deps.config.repo, str(pull.number))
        update = track_pr.judge(found, pull.created_at, pull.last_reminded_at, now,
                                deps.config.whole_threshold("release.prReminderWorkdays"),
                                deps.config.non_working_days())
        record = replace(pull, state=found.state, mergeable=found.mergeable, merge_commit=found.merge_commit,
                         merged_at=found.merged_at, closed_at=found.closed_at, last_checked_at=now,
                         reviews=[dict(item) for item in found.reviews],
                         last_reminded_at=now if update.remind else pull.last_reminded_at,
                         close_note=update.note if update.state == track_pr.CLOSED else pull.close_note)
        pulls.save(deps.conn, record)
        outputs = self.outputs(issue_id)
        if outputs.get("pr") is not None:
            outputs["pr"] = {**outputs["pr"], "state": found.state.lower(), "mergeable": found.mergeable,
                             "mergeCommit": found.merge_commit,
                             "mergedAt": None if found.merged_at is None else format_iso(found.merged_at),
                             "closedAt": None if found.closed_at is None else format_iso(found.closed_at),
                             "userNote": record.close_note}
        lines = []
        if merge is not None:
            outputs["autoMerge"] = merge
            if not merge["merged"]:
                lines.append(f"Issue {issue_id} 的 PR #{pull.number} 未自动合并：{'；'.join(merge['reasons'])}")
        if update.state == track_pr.MERGED:
            self._event(issue_id, IssueEvent.PR_MERGED, f"PR #{pull.number} 已合并，合并提交 {found.merge_commit}")
            lines.append(f"Issue {issue_id} 的 PR #{pull.number} 已合并，进入部署跟踪")
        elif update.state == track_pr.CLOSED:
            note = f"PR #{pull.number} 关闭且未合并" + (f"；用户说明：{update.note}" if update.note else "")
            self._event(issue_id, IssueEvent.PR_CLOSED, note)
            lines.append(f"Issue {issue_id} 以修复未采纳关闭；如关闭理由是「不是缺陷」一类，执行 retriage --verdict 改判")
        if update.remind:
            lines.append(f"Issue {issue_id} 的 PR #{pull.number} 已超过提醒期限仍未处理：{pull.url}")
        if update.conflict and deps.notifier is not None:
            deps.notifier.notify(CONFLICT_EVENT, issue_id, f"PR #{pull.number} 与 main 冲突，执行 release sync {issue_id}")
            lines.append(f"Issue {issue_id} 的 PR 与 main 冲突：执行 release sync {issue_id} 在本地合并")
        self._save(issue_id, outputs, HandoffStatus.OK, "继续跟踪")
        return lines

    def _auto_merge(self, issue_id: str, pull: PullRecord) -> dict[str, Any]:
        """自动合并(redesign/07-release.md 第 2 节)：改动命中 release.autoMergeBlockPaths 时不合并，写决策简报交用户；
        仓库有分支保护与必需检查时满足 own_blockers 即开启 GitHub 原生自动合并(只开启一次)；否则满足全部条件时由
        本工具合并。原因与上一次不同时才写 Issue 历史。"""
        deps = self.deps
        previous = self.outputs(issue_id)
        fix = self._latest(RunStage.FIX, issue_id)
        record: dict[str, Any] = {"merged": False, "native": False, "reasons": [], "operationId": None,
                                  "at": format_iso(deps.clock.now()), "decision": None}
        changed = [item["path"] for item in (fix or {}).get("changedFiles") or []]
        blocked = auto_merge.blocked_paths(deps.config, changed)
        if blocked:
            record["reasons"] = ["改动涉及需要用户决定的路径：" + "、".join(f"{path}({pattern})" for path, pattern in blocked)]
            record["decision"] = self._merge_decision(issue_id, pull, blocked, previous)
            return self._unmerged(issue_id, pull, record, previous)
        facts = deps.gh.pr_merge_facts(deps.config.repo, pull.number)
        slug = self._slug()
        if slug is None:
            record["reasons"] = [f"无法从 {deps.config.repo} 的 origin 远程解析出 GitHub 仓库"]
            return self._unmerged(issue_id, pull, record, previous)
        local = self._latest(RunStage.VERIFY, issue_id, VerifyPhase.LOCAL)
        native = deps.gh.required_checks(deps.config.repo, slug, deps.config.main_branch)
        record["native"] = native
        state = ci.judge(deps.gh.pr_checks(deps.config.repo, pull.number) if native else None)
        record["ci"] = state.to_dict()
        reasons = auto_merge.own_blockers(local, fix, previous, facts) if native else \
            auto_merge.blockers(local, fix, previous, facts)
        if state.reason is not None:
            reasons = [*reasons, state.reason]
        record["reasons"] = reasons
        earlier = previous.get("autoMerge") or {}
        if not reasons and native and earlier.get("native") and earlier.get("enabledFor") == facts.head:
            return {**earlier, "at": record["at"]}
        if reasons:
            return self._unmerged(issue_id, pull, record, previous)
        method = str(deps.config.get("release.mergeMethod"))
        operation = deps.planner.plan_merge_pull_request(issue_id, pull.number, slug, pull.branch, facts.head, method,
                                                         auto=native)
        result = deps.operations.run_unattended(operation.id, reason=AUTO_MERGE_REASON, clock=deps.clock)
        record["operationId"] = operation.id
        if result.operation.status is OperationStatus.EXECUTED:
            if native:
                record["enabledFor"] = facts.head
                self._history(issue_id, f"已为 PR #{pull.number} 开启 GitHub 自动合并({method})：必需检查通过后由 GitHub 合并")
            else:
                record["merged"] = True
                self._history(issue_id, f"自动合并 PR #{pull.number}({method})并删除远程分支 {pull.branch}：满足全部合并条件")
            return record
        record["reasons"] = [f"合并操作{result.operation.status.label}："
                             f"{result.error or json.dumps(result.operation.result, ensure_ascii=False)}"]
        return self._unmerged(issue_id, pull, record, previous)

    def _unmerged(self, issue_id: str, pull: PullRecord, record: dict[str, Any],
                  previous: Mapping[str, Any]) -> dict[str, Any]:
        if (previous.get("autoMerge") or {}).get("reasons") != record["reasons"]:
            self._history(issue_id, f"PR #{pull.number} 未自动合并：{'；'.join(record['reasons'])}")
        return record

    def _merge_decision(self, issue_id: str, pull: PullRecord, blocked: list[tuple[str, str]],
                        previous: Mapping[str, Any]) -> str:
        """改动命中高风险路径时的决策简报(decision 类型)，同一组命中只写一次；返回相对工作区的路径。"""
        deps = self.deps
        path = deps.layout.fixes_dir(issue_id) / MERGE_DECISION
        relative = deps.layout.relative(path)
        if (previous.get("autoMerge") or {}).get("decision") == relative and path.is_file():
            return relative
        listing = "\n".join(f"- `{file}`(规则 `{pattern}`)" for file, pattern in blocked)
        background = (f"PR #{pull.number}({pull.url})的改动涉及 CI、依赖、迁移、权限认证或密钥配置等路径，按 "
                      f"release.autoMergeBlockPaths 不自动合并，需要用户决定：\n{listing}")
        writer = fix_documents.Writer(deps.layout.fixes_dir(issue_id), issue_id, deps.config.language, deps.zone)
        writer.write(MERGE_DECISION, fix_documents.decision(
            writer, deps.clock.now(), background=background,
            options=[("merge", "审阅这些文件后在 GitHub 上合并", True),
                     ("review", "要求补充评审后再合并", False), ("close", "关闭 PR，不采用这次修复", False)],
            recommendation="审阅列出的文件后在 GitHub 上合并",
            reason="其余合并条件由验证与评审覆盖，这些路径的改动影响构建、依赖、数据或权限，需要人确认",
            name="merge-decision", source="release"))
        if deps.notifier is not None:
            deps.notifier.notify(MERGE_DECISION_EVENT, issue_id, f"Issue {issue_id} 的 PR 需要用户决定是否合并：{path}")
        return relative

    # AI 评审结论写进 PR

    def _review_comment(self, issue_id: str, number: int, outputs: dict[str, Any]) -> None:
        """修复最后一轮评审的结论以评论写入 PR(release.reviewComment)，同一轮只发一次；只作说明，不作批准。"""
        deps = self.deps
        fix = self._latest(RunStage.FIX, issue_id) or {}
        rounds = fix.get("rounds") or []
        slug = self._slug()
        if not bool(deps.config.get("release.reviewComment")) or not rounds or slug is None:
            return
        last = rounds[-1]
        posted = outputs.get("reviewComment") or {}
        if posted.get("round") == last["round"] and posted.get("pr") == number:
            return
        body = pr_comment.review(last, self._review_conclusions(issue_id, last))
        key = f"review-comment:{number}:{last['round']}"
        operation = deps.planner.plan_pr_comment(issue_id, number, slug, body, key)
        outputs["reviewComment"] = {"pr": number, "round": last["round"], "operationId": operation.id}
        if unattended.release_direct(deps.config, operation.kind):
            deps.operations.run_unattended(operation.id, reason=unattended.RELEASE_REASON, clock=deps.clock)

    def _review_conclusions(self, issue_id: str, last: Mapping[str, Any]) -> list[str]:
        found = []
        for review in last.get("reviews") or []:
            path = self.deps.layout.fixes_dir(issue_id) / fix_documents.review_name(last["round"], review["mode"])
            conclusion = handoff_documents.read(path).conclusion.strip() if path.is_file() else ""
            found.append(f"{review['mode']}：{'通过' if review['passed'] else '有阻断项'}" + (
                f"。{conclusion}" if conclusion else ""))
        return found

    # 撤销

    def revert(self, issue_id: str, reason: str) -> ReleaseResult:
        """部署后确认发现这次合并导致回归时，提撤销该合并的 PR 交用户决定(redesign/07-release.md 第 3 节)；
        由部署后确认调用，也可以手动执行 release revert。不改变 Issue 状态。"""
        deps = self.deps
        issue = self._issue(issue_id)
        pull = pulls.get(deps.conn, issue_id)
        if issue.status is not IssueStatus.DONE or pull is None or pull.merge_commit is None:
            return ReleaseResult(issue_id, HandoffStatus.BLOCKED, "只能撤销已合并(完成)的修复：没有合并提交")
        outputs = self.outputs(issue_id)
        earlier = outputs.get("revert") or {}
        record = pending_operations.get(deps.conn, earlier["operationId"]) if earlier.get("operationId") else None
        if record is not None and record.status is OperationStatus.PENDING:
            return ReleaseResult(issue_id, HandoffStatus.BLOCKED, f"撤销 PR 已在等待确认：{record.id}", record.id)
        found = self._conventions()
        kind = release_format.HOTFIX
        branch = release_format.branch(found.branch, kind=kind, issue_id=issue_id, slug=f"revert-{issue.slug}",
                                       prefix=deps.branch_prefix if found.personal_prefix else None)
        title = f"Revert: {pull.title}"
        lines = [f"撤销 PR #{pull.number}({pull.url})的合并提交 {pull.merge_commit[:12]}。", "",
                 f"原因：{reason}", "", f"关联：Issue {issue_id}" + (
                     f"(#{issue.github.number})" if issue.github is not None else ""),
                 "", "这个 PR 由 tightrein 生成，不会自动合并，请用户决定是否合并。"]
        mainline = str(deps.config.get("release.mergeMethod")) == "merge"
        operation = deps.planner.plan_revert_pull_request(issue_id, pull.merge_commit, branch, title,
                                                          "\n".join(lines), mainline)
        outputs["revert"] = {"reason": reason, "branch": branch, "operationId": operation.id, "url": None}
        self._save(issue_id, outputs, HandoffStatus.OK, f"确认 {operation.id} 后由用户决定是否合并撤销 PR")
        self._history(issue_id, f"提撤销合并的 PR({operation.id})：{reason}")
        return self._pending(issue_id, operation, f"撤销 PR：{title}\n分支：{branch}\n原因：{reason}")

    def _track_deploy(self, issue_id: str, pull: PullRecord) -> list[str]:
        """部署跟踪：按部署来源找包含合并提交的部署；没有配置部署来源时，合并后经过观察期视为已部署。"""
        deps = self.deps
        if deps.deploys is None or not deps.deploys.configured():
            return self._observed(issue_id, pull)
        found = deps.deploys.find(pull.merge_commit)
        if found.record is None:
            return [f"Issue {issue_id} 还没有包含合并提交的部署"]
        record = found.record
        previous = deployments.get(deps.conn, record.commit)
        deployed_at = deps.clock.now() if found.status is DeploymentStatus.SUCCEEDED else None
        if previous is not None and previous.deployed_at is not None:
            deployed_at = previous.deployed_at
        deployments.save(deps.conn, Deployment(record.commit, found.status, deps.clock.now(), record.id, record.url,
                                               deployed_at))
        self._record_deployment(issue_id, {"workflowRunId": record.id, "commit": record.commit,
                                           "status": found.status.value, "url": record.url,
                                           "source": deploys.SOURCE})
        if previous is not None and previous.status is found.status:
            return []
        lines = []
        if found.status is DeploymentStatus.FAILED:
            self._history(issue_id, f"部署失败：{record.url or record.id}")
            if deps.notifier is not None:
                deps.notifier.notify(DEPLOY_EVENT, issue_id, f"包含 Issue {issue_id} 修复的部署失败：{record.url}")
            lines.append(f"Issue {issue_id} 的部署失败，是否由本修复引起请用户判断：{record.url or record.id}")
        elif found.status is DeploymentStatus.SUCCEEDED:
            lines += self._deployed(issue_id, f"已部署(部署 commit {record.commit[:12]})")
        return lines

    def _observed(self, issue_id: str, pull: PullRecord) -> list[str]:
        """没有部署来源：合并时间加 release.deploy.observationHours 之后按已部署记录(source 为 merge-time)。"""
        deps = self.deps
        if pull.merged_at is None:
            return []
        due = deploys.observed_at(deps.config, pull.merged_at)
        if deps.clock.now() < due:
            return [f"Issue {issue_id} 已合并，{deploys.UNCONFIGURED}：{format_iso(due)} 之后进行部署后确认"]
        if deployments.get(deps.conn, pull.merge_commit) is not None:
            return []
        deployments.save(deps.conn, Deployment(pull.merge_commit, DeploymentStatus.SUCCEEDED, deps.clock.now(), None,
                                               None, due))
        self._record_deployment(issue_id, {"workflowRunId": deploys.MERGE_TIME, "commit": pull.merge_commit,
                                           "status": DeploymentStatus.SUCCEEDED.value, "url": None,
                                           "source": deploys.MERGE_TIME})
        return self._deployed(issue_id, "合并后已过观察期，视为已部署(没有配置部署来源)")

    def _record_deployment(self, issue_id: str, entry: dict[str, Any]) -> None:
        outputs = self.outputs(issue_id)
        outputs["deployments"] = [*[item for item in outputs["deployments"]
                                    if item["workflowRunId"] != entry["workflowRunId"]], entry]
        self._save(issue_id, outputs, HandoffStatus.OK, "继续跟踪")

    def _deployed(self, issue_id: str, text: str) -> list[str]:
        deps = self.deps
        self._history(issue_id, text)
        lines = [f"Issue {issue_id} {text}，等待部署后确认：verify staging {issue_id}"]
        fix = self._latest(RunStage.FIX, issue_id) or {}
        manual = track_deploy.manual_paths(deps.config, [item["path"] for item in fix.get("changedFiles") or []])
        if manual:
            note = f"改动了需要手动部署的部分：{'、'.join(manual)}，请联系负责人手动部署"
            self._history(issue_id, note)
            if deps.notifier is not None:
                deps.notifier.notify(MANUAL_EVENT, issue_id, note)
            lines.append(f"Issue {issue_id} {note}")
        return lines

    def _track_master(self, issue_id: str, pull: PullRecord) -> list[str]:
        deps = self.deps
        if not track_master.in_master(deps.git.branches_containing(deps.config.repo, pull.merge_commit)):
            return []
        now = deps.clock.now()
        pulls.save(deps.conn, replace(pull, master_at=now))
        outputs = self.outputs(issue_id)
        outputs["masterAt"] = now.date().isoformat()
        self._save(issue_id, outputs, HandoffStatus.OK, "已进入 master")
        self._history(issue_id, f"修复已进入 {track_master.MASTER}")
        return [f"Issue {issue_id} 的修复已进入 {track_master.MASTER}"]

    # 工作总结、PR 回复草稿与收尾清理

    def summary(self, issue_id: str) -> ReleaseResult:
        deps = self.deps
        record = transitions.record_of(self._env(), issue_id)
        issue = record.issue
        if issue.status is not IssueStatus.DONE:
            return ReleaseResult(issue_id, HandoffStatus.BLOCKED, "PR 合并后才能生成工作总结")
        document = issue_files.read(deps.layout.root / record.path)
        local = self._latest(RunStage.VERIFY, issue_id, VerifyPhase.LOCAL)
        staging = self._latest(RunStage.VERIFY, issue_id, VerifyPhase.STAGING)
        verifications = []
        if local is not None:
            verifications.append(f"合并前验证：{'通过' if local['conclusion'] == 'passed' else local['conclusion']}")
        if staging is not None and staging["conclusion"] == "passed":
            verifications.append("已在测试环境验证通过")
        shots = [item["evidence"][0] for item in (local or {}).get("items", [])
                 if item["category"] == "screenshot" and item["evidence"]]
        text = work_summary.summary(issue.title, issue_sections.find(edit.sections(document.body), issue_sections.PROBLEM) or "",
                                    self._latest(RunStage.FIX, issue_id) or {}, verifications, shots,
                                    deps.config.whole_threshold("release.summaryItems"),
                                    deps.config.whole_threshold("release.summaryScreenshots"),
                                    work_summary.page_note(local))
        path = self._file(issue_id, "summary.md")
        atomic.write_text(path, text)
        return ReleaseResult(issue_id, HandoffStatus.OK, text, path=path)

    def comment(self, issue_id: str) -> ReleaseResult:
        staging = self._latest(RunStage.VERIFY, issue_id, VerifyPhase.STAGING)
        if staging is None or staging["conclusion"] != "passed":
            return ReleaseResult(issue_id, HandoffStatus.BLOCKED, "部署后确认通过后才能起草 PR 回复")
        path = self._file(issue_id, "pr-comment.md")
        text = pr_comment.render(staging)
        atomic.write_text(path, text)
        return ReleaseResult(issue_id, HandoffStatus.OK, f"PR 回复草稿(确认内容后由用户自行回复到 PR)：\n{text}", path=path)

    def cleanup(self, issue_id: str) -> ReleaseResult:
        deps = self.deps
        issue = self._issue(issue_id)
        if not issue.is_closed:
            return ReleaseResult(issue_id, HandoffStatus.BLOCKED, "Issue 完成或取消后才能清理")
        worktree = self._worktree(issue_id)
        if not worktree.is_dir() or issue.branch is None:
            return ReleaseResult(issue_id, HandoffStatus.OK, "修复 worktree 已不存在")
        status = deps.git.status(worktree)
        if not status.clean:
            return ReleaseResult(issue_id, HandoffStatus.BLOCKED,
                                 f"worktree 中有未提交的内容，先处理：{'、'.join(status.changed_paths)}")
        operation = deps.planner.plan_cleanup(issue_id, worktree, issue.branch)
        return self._pending(issue_id, operation, cleanup_step.risks(issue.branch, issue.close_reason))
