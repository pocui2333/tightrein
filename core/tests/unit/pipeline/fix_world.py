"""fix、verify、release 测试共用：已放行的 Issue、修复 worktree 目录、假执行器、假 git 与待确认操作的执行。

假 git 按 worktree 中文件相对基准内容的差异回答 diff、status 与 diff_hash，不起真实的 git 仓库；fix-executor 的假
执行器直接写 worktree 中的文件。
"""

import difflib
import hashlib
from dataclasses import dataclass, field, replace
from pathlib import Path

from pipeline_world import make_signal
from triage_world import FakeRunner, make_triage_world, store_triaged, triage_outputs

from tightrein.domain.enums import IssueStatus, RunnerStatus, Severity
from tightrein.pipeline.issue.service import IssueDeps, IssueService
from tightrein.pipeline.issue.steps import transitions
from tightrein.runner.result import RunnerResult
from tightrein.store.repos import issues
from tightrein.vcs.git_read import Diff, FileDiff, HeadState
from tightrein.vcs.parse import RepoStatus, StatusEntry

BASE = "1" * 40
FINGERPRINT = "a1b2c3d4e5f60718"
SERVICE = "\n".join(f"line {number}" for number in range(1, 41)) + "\n"
CONTROLLER = "\n".join(f"controller {number}" for number in range(1, 21)) + "\n"
SERVICE_PATH = "src/Services/OrderService.src"
CONTROLLER_PATH = "src/Controllers/OrderController.src"


class FakeGit:
    """worktree 的基准内容为 base；diff 与 status 按当前文件计算。"""

    def __init__(self, root: Path, base: dict[str, str]):
        self.root = root
        self.base = dict(base)
        self.head_commit = BASE
        self.branch = "cty/fix-order-500"

    def _current(self):
        found = {}
        for path in self.root.rglob("*"):
            if path.is_file():
                found[path.relative_to(self.root).as_posix()] = path.read_text(encoding="utf-8")
        return found

    def _changed(self):
        current = self._current()
        return sorted(path for path in set(current) | set(self.base) if current.get(path) != self.base.get(path))

    def diff(self, repo, base, head=None, paths=None):
        current = self._current()
        files = []
        for path in self._changed():
            if path not in self.base:
                continue
            before, after = self.base[path].splitlines(), current.get(path, "").splitlines()
            added = tuple(line for line in after if line not in before)
            removed = tuple(line for line in before if line not in after)
            if paths is None or path in paths:
                files.append(FileDiff(path, len(added), len(removed), added_lines=added, removed_lines=removed))
        return Diff(tuple(files))

    def patch(self, repo, base):
        current = self._current()
        parts = []
        for path in self._changed():
            if path not in self.base:
                continue
            lines = difflib.unified_diff(self.base[path].splitlines(True), current.get(path, "").splitlines(True),
                                         f"a/{path}", f"b/{path}", n=0)
            parts.append(f"diff --git a/{path} b/{path}\n" + "".join(lines))
        return "".join(parts)

    def untracked(self, repo, include_ignored=False):
        return tuple(path for path in self._changed() if path not in self.base)

    def files(self, repo):
        return tuple(sorted(self.base))

    def diff_hash(self, repo, base):
        digest = hashlib.sha256()
        current = self._current()
        for path in self._changed():
            digest.update(f"{path}\0{current.get(path, '')}\0".encode())
        return digest.hexdigest()

    def status(self, repo):
        entries = tuple(StatusEntry(path, "changed") for path in self._changed())
        return RepoStatus(self.head_commit, self.branch, None, 0, 0, entries)

    def head(self, repo):
        return HeadState(self.head_commit, self.branch)

    def rev_parse(self, repo, ref):
        return self.head_commit

    def branch_exists(self, repo, branch):
        return False


class FixRunner(FakeRunner):
    """写测试与写代码的任务(fix-executor、repro-writer)每次调用依次取一组改动(相对 worktree 的路径到新内容)写入
    worktree，再返回预置的输出；每次调用的结果带一个会话编号，resumed 记录续接的(角色, 会话编号)。"""

    WRITERS = ("fix-executor", "repro-writer")

    def __init__(self, outputs=None):
        super().__init__(outputs)
        self.edits = []
        self.sessions = []
        self.resumed = []

    def run(self, task, *, clock, runner_override=None, model_override=None, resume_session=None):
        if task.role in self.WRITERS and self.edits:
            for path, text in self.edits.pop(0).items():
                target = task.workdir / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
        self.resumed.append((task.role, resume_session))
        result = super().run(task, clock=clock, runner_override=runner_override, model_override=model_override)
        return replace(result, session_id=resume_session or f"session-{len(self.tasks)}")

    def run_interactive(self, task, *, clock, first_input):
        self.sessions.append(("new", task, first_input))
        return RunnerResult(RunnerStatus.OK, "fake")

    def resume_interactive(self, task, *, clock, first_input):
        self.sessions.append(("resume", task, first_input))
        return RunnerResult(RunnerStatus.FAILED, "fake", error_type="resume-unsupported")


@dataclass
class FixWorld:
    base: object
    runner: FixRunner = field(default_factory=FixRunner)
    issue_id: str = ""

    def __getattr__(self, name):
        return getattr(self.base, name)

    @property
    def worktree(self):
        return self.base.layout.fix_worktree(self.issue_id)

    def env(self):
        return transitions.IssueEnv(self.base.conn, self.base.layout, self.base.clock, self.base.config)

    def event(self, event, **options):
        return transitions.apply_event(self.env(), issues.get(self.base.conn, self.issue_id), event, **options)

    def issue(self):
        return issues.get(self.base.conn, self.issue_id).issue

    def git(self):
        return FakeGit(self.worktree, {SERVICE_PATH: SERVICE, CONTROLLER_PATH: CONTROLLER})


def make_fix_world(tmp_path, *, signal=None, manual=None, **config_changes):
    """一个去向为提 Issue 的问题、由它创建的 Issue(已放行；开启 autonomy 时由创建自动放行)与修复 worktree 目录；
    manual 为 (标题, 需求) 时改为用户需求的 Issue(不关联问题)。"""
    base = make_triage_world(tmp_path, **config_changes)
    service = IssueService(IssueDeps(base.layout, base.config, base.conn, base.clock, base.events,
                                     snapshot=base.worktree))
    if manual is not None:
        world = FixWorld(base, issue_id=service.create_manual(*manual, Severity.P2).issue.id)
    else:
        store_triaged(base, "P-0001", signal or make_signal(1), outputs=triage_outputs("P-0001"),
                      fingerprint=FINGERPRINT)
        world = FixWorld(base, issue_id=service.create().items[0].issue_id)
    if world.issue().status is IssueStatus.NEEDS_DECISION or world.issue().is_manual:
        service.approve(world.issue_id)
    worktree = world.worktree
    (worktree / "src" / "Services").mkdir(parents=True)
    (worktree / "src" / "Controllers").mkdir(parents=True)
    (worktree / SERVICE_PATH).write_text(SERVICE, encoding="utf-8")
    (worktree / CONTROLLER_PATH).write_text(CONTROLLER, encoding="utf-8")
    return world


def failed_result(status=RunnerStatus.FAILED):
    return RunnerResult(status, "fake", error_type="fake-error")

