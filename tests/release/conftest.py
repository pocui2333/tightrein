"""release 测试共用的夹具：本地真 git 仓库(origin 为本地裸仓库)、假 GitHub、组装好的 Runtime。

不联网、不调用模型。Issue 状态转换与 Issue 文件的写入换成只改 issues 表的简化版：状态机的规则照常用
assess/issue/transitions.transition，副作用(关联问题、GitHub 同步)不在发布的测试范围内。
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tightrein.assess.issue import transitions
from tightrein.onboard.setup import ModuleSetup, ModuleStatus, Setup
from tightrein.protocol.git import Git
from tightrein.protocol.git.git import STATE_KEYS, CommandFailed, failure, launch
from tightrein.protocol.git.github import Check, MergeFacts, PullRequest
from tightrein.protocol.naming import FixedClock
from tightrein.protocol.process import Command, Outcome
from tightrein.protocol.records import EventLog
from tightrein.protocol.runtime import Runtime
from tightrein.protocol.security import Redactor
from tightrein.release import record
from tightrein.settings.load import ProjectFacts, Settings
from tightrein.store.db import open_database
from tightrein.store.files.layout import ToolLayout, WorkspaceLayout
from tightrein.store.tables import issues
from tightrein.store.tables.issues import Issue

ROOT = Path(__file__).resolve().parents[2]
DEFAULTS = ROOT / "settings" / "defaults.json"
NOW = datetime(2026, 10, 8, 3, 0, tzinfo=UTC)
BRANCH = "fix/7-order-page"
# src/order.py：首尾两行隔得够远，修复改第一行、main 改最后一行时 git 能自动合并
ORDER = "PAGE = 1\n\n\ndef page():\n    return PAGE\n\n\nSIZE = 10\n"
FIX_ORDER = ORDER.replace("PAGE = 1", "PAGE = 0")
MAIN_ORDER = ORDER.replace("SIZE = 10", "SIZE = 20")
GIT_ENV = {
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    "HOME": "/nonexistent",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Cui Ty",
    "GIT_AUTHOR_EMAIL": "cty@example.com",
    "GIT_COMMITTER_NAME": "Cui Ty",
    "GIT_COMMITTER_EMAIL": "cty@example.com",
    "TZ": "UTC",
}


class LocalRunner:
    """真实执行命令的简化 ProcessRunner；replies 中按 argv 前缀给出预置结果(用于 gh)，最长的前缀优先，
    每个前缀的结果依次取用，最后一个一直沿用。"""

    def __init__(self) -> None:
        self.commands: list[Command] = []
        self.replies: dict[tuple[str, ...], list[Outcome]] = {}

    def run(self, command: Command) -> Outcome:
        self.commands.append(command)
        for prefix, outcomes in sorted(self.replies.items(), key=lambda item: -len(item[0])):
            if tuple(command.argv[:len(prefix)]) == prefix and outcomes:
                return outcomes.pop(0) if len(outcomes) > 1 else outcomes[0]
        started = time.monotonic()
        done = subprocess.run(list(command.argv), cwd=command.cwd, env=dict(command.env), input=command.stdin,
                              capture_output=True, text=True, timeout=command.timeout_s, check=False)
        return Outcome(done.returncode, done.stdout, done.stderr, int((time.monotonic() - started) * 1000), None, None)


class Repos:
    """origin(裸仓库)、主工作区与另一个 clone(模拟别人往 main 推送)。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.origin = root / "origin.git"
        self.repo = root / "repo"
        self.other = root / "other"
        root.mkdir(parents=True, exist_ok=True)
        self.git(root, "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.git(root, "init", "-q", "-b", "main", str(self.repo))
        self.git(self.repo, "remote", "add", "origin", str(self.origin))
        self.commit(self.repo, "chore: init", {"README.md": "# demo\n", "src/order.py": ORDER,
                                                "src/user.py": "NAME = 'a'\n", ".gitignore": "bin/\n"})
        self.git(self.repo, "push", "-q", "-u", "origin", "main")
        self.git(root, "clone", "-q", str(self.origin), str(self.other))

    def git(self, cwd: Path, *args: str) -> str:
        done = subprocess.run(["git", *args], cwd=cwd, env=GIT_ENV, capture_output=True, text=True, check=False)
        if done.returncode != 0:
            raise AssertionError(f"git {' '.join(args)} 失败：{done.stderr}")
        return done.stdout

    def write(self, cwd: Path, files: dict[str, str]) -> None:
        for path, text in files.items():
            target = cwd / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")

    def commit(self, cwd: Path, message: str, files: dict[str, str]) -> str:
        self.write(cwd, files)
        self.git(cwd, "add", "-A")
        self.git(cwd, "commit", "-q", "-m", message)
        return self.head(cwd)

    def head(self, cwd: Path) -> str:
        return self.git(cwd, "rev-parse", "HEAD").strip()

    def upstream(self, message: str, files: dict[str, str]) -> str:
        """别人在 origin/main 上提交并推送。"""
        self.git(self.other, "pull", "-q", "origin", "main")
        commit = self.commit(self.other, message, files)
        self.git(self.other, "push", "-q", "origin", "main")
        return commit

    def worktree(self, path: Path, branch: str = BRANCH) -> Path:
        self.git(self.repo, "fetch", "-q", "origin")
        self.git(self.repo, "worktree", "add", "-q", "-b", branch, str(path), "origin/main")
        return path


@dataclass
class FakeGitHub:
    """GitHub 的替身：只实现发布用到的方法，PR 存在内存里；原样的只读查询(query、query_json)经 runner 应答。"""

    runner: LocalRunner
    cwd: Path
    origin: Path | None = None  # 给出时 PR 的 head 取 origin 上该分支的 commit
    slug: str = "cty/sample"
    program: str = "gh"
    env: dict[str, str] = field(default_factory=dict)
    timeout_s: float = 60.0
    required: bool = False
    pulls: dict[int, dict[str, Any]] = field(default_factory=dict)
    checks: list[Check] = field(default_factory=list)
    calls: list[tuple[Any, ...]] = field(default_factory=list)
    expected: list[dict[str, str | None] | None] = field(default_factory=list)  # 每次 create_pr 收到的 expected

    def pr_for_branch(self, branch: str) -> PullRequest | None:
        found = [number for number, pull in self.pulls.items() if pull["head_ref"] == branch]
        return self.pr_view(found[-1]) if found else None

    def pull_state(self, branch: str) -> dict[str, str | None]:
        pull = self.pr_for_branch(branch)
        return {"pullRequest": str(pull.number) if pull is not None and pull.state == "OPEN" else None}

    def create_pr(self, branch: str, base: str, title: str, body: str, *, scope: Any,
                  expected: dict[str, str | None] | None = None) -> int:
        self.expected.append(expected)
        for number, pull in self.pulls.items():
            if pull["head_ref"] == branch and pull["state"] == "OPEN":
                if pull["body"] != body:
                    pull["body"] = body
                    self.calls.append(("pr-edit", number))
                return number
        number = max(self.pulls, default=185) + 1
        self.pulls[number] = {"head_ref": branch, "title": title, "body": body, "state": "OPEN", "head": None,
                              "mergeable": "MERGEABLE", "merge_state": "CLEAN", "draft": False, "reviews": [],
                              "merge_commit": None, "comments": []}
        self.calls.append(("pr-create", number, base, title))
        return number

    def pr_view(self, number: int) -> PullRequest:
        pull = self.pulls[number]
        return PullRequest(number, f"https://github.com/{self.slug}/pull/{number}", pull["state"],
                           head_ref=pull["head_ref"], head=self.head(number),
                           merged_at=NOW if pull["state"] == "MERGED" else None, merge_commit=pull["merge_commit"])

    def merge_facts(self, number: int) -> MergeFacts:
        pull = self.pulls[number]
        return MergeFacts(pull["state"], pull["draft"], pull["mergeable"], pull["merge_state"], self.head(number),
                          tuple(pull["reviews"]))

    def head(self, number: int) -> str | None:
        pull = self.pulls[number]
        if pull["head"] is not None or self.origin is None:
            return pull["head"]
        done = subprocess.run(["git", "rev-parse", f"refs/heads/{pull['head_ref']}"], cwd=self.origin, env=GIT_ENV,
                              capture_output=True, text=True, check=False)
        return done.stdout.strip() or None

    def pr_checks(self, number: int) -> list[Check]:
        return list(self.checks)

    def required_checks(self, branch: str) -> bool:
        return self.required

    def merge_pr(self, number: int, head: str, method: str, *, auto: bool = False, scope: Any) -> int:
        self.calls.append(("pr-merge", number, head, method, auto))
        if not auto:
            self.pulls[number]["state"] = "MERGED"
            self.pulls[number]["merge_commit"] = "m" * 40
        return number

    def query(self, *args: str, repo: bool = True, timeout_s: float | None = None, retry: bool = True) -> Outcome:
        argv = (self.program, *args, *(("--repo", self.slug) if repo else ()))
        command = Command(argv=argv, cwd=self.cwd, env=self.env, timeout_s=timeout_s or self.timeout_s)
        return launch(self.runner, command, remote=True, redact=lambda text: text)

    def query_json(self, *args: str, repo: bool = True) -> Any:
        found = self.query(*args, repo=repo)
        if found.exit_code != 0:
            raise failure(CommandFailed, (self.program, *args), found, lambda text: text)
        return json.loads(found.stdout or "null")

    def comment(self, number: int, body: str, *, scope: Any) -> str:
        self.pulls[number]["comments"].append(body)
        return f"https://github.com/{self.slug}/pull/{number}#c"


def make_settings(*overrides: dict[str, Any]) -> Settings:
    defaults = json.loads(DEFAULTS.read_text(encoding="utf-8"))
    project = ProjectFacts(repo=None, main_branch="main", language="zh", test_patterns=("tests/",))
    return Settings.from_data(defaults, *overrides, project=project)


def make_setup(deploy: ModuleStatus = ModuleStatus.DISABLED, method: str | None = None,
               accept: ModuleStatus = ModuleStatus.ENABLED) -> Setup:
    module = ModuleSetup("release.deploy", deploy, method, None, None, "没有部署平台", "以合并时间加一段时间为准")
    observe = ModuleSetup("release.accept", accept, None, None, None, None, None)
    return Setup("demo", "2026-10-08T00:00:00Z", {"release.deploy": module, "release.accept": observe})


def make_runtime(tmp_path: Path, repo: Path, *, settings: Settings | None = None, github: Any = None,
                 setup: Setup | None = None, clock: FixedClock | None = None,
                 conn: sqlite3.Connection | None = None) -> Runtime:
    workspace = WorkspaceLayout(tmp_path / "workspaces" / "demo")
    clock = clock or FixedClock(NOW)
    settings = settings or make_settings()
    redactor = Redactor()
    runner = github.runner if github is not None else LocalRunner()
    run = "R-20261008T030000Z-release"
    return Runtime(
        tool=ToolLayout(ROOT), workspace=workspace, settings=settings, setup=setup or make_setup(),
        conn=conn or open_database(workspace.database, clock=clock), clock=clock, runner=runner, redactor=redactor,
        secrets={}, environ=GIT_ENV, run=run, events=EventLog(workspace.events(run), redactor, clock),
        agents=None, git=Git(repo, runner, GIT_ENV, settings, redactor=redactor, sleep=lambda seconds: None),  # type: ignore[arg-type]
        github=github, slots=None,  # type: ignore[arg-type]
    )


def new_issue(runtime: Runtime, *, status: str = "releasing", severity: str | None = "P2",
              extra: dict[str, Any] | None = None, issue_id: str = "0007") -> Issue:
    issue = Issue(id=issue_id, status=status, title="订单页翻页错误", kind="bug", origin="problem",
                  severity=severity, extra=dict(extra or {}))
    issues.save(runtime.conn, issue, runtime.clock)
    return issue


def delivery_facts(worktree: Path, changed: list[str], **overrides: Any) -> dict[str, Any]:
    facts: dict[str, Any] = {
        "branch": BRANCH, "worktree": str(worktree), "commit": "c" * 40, "diffHash": "d" * 64,
        "changedFiles": [{"path": path, "added": 1, "deleted": 1} for path in changed],
        "checks": [{"name": "pytest", "passed": True}], "acceptedFindings": [],
        "review": {"round": 1, "conclusions": ["light：通过"]}, "highRiskPaths": [],
        "release": {"title": "订单页翻页从第一页开始", "summary": "订单页翻页从第一页开始", "why": "页码从 0 开始算错",
                    "problem": "订单页翻到第二页时显示第一页"},
    }
    facts.update(overrides)
    return facts


def deliver(runtime: Runtime, issue: str, facts: dict[str, Any]) -> None:
    """落一份实施·交付的 handoff。"""
    from tightrein.protocol import handoff
    from tightrein.protocol.handoff import Handoff, Status
    from tightrein.protocol.naming import FileName, format_iso

    path = runtime.workspace.step_file(issue, FileName(record.DELIVER_POINT, "handoff", "json"))
    handoff.write(path, Handoff(record.DELIVER_POINT, issue, runtime.run, Status.PASSED, "交付", facts,
                                created_at=format_iso(runtime.clock.now())))


@pytest.fixture
def repos(tmp_path: Path) -> Repos:
    return Repos(tmp_path / "git")


@pytest.fixture
def worktree(repos: Repos, tmp_path: Path) -> Path:
    return repos.worktree(tmp_path / "workspaces" / "demo" / "worktrees" / "0007")


@pytest.fixture
def runtime(tmp_path: Path, repos: Repos) -> Iterator[Runtime]:
    built = make_runtime(tmp_path, repos.repo)
    yield built
    built.conn.close()


@pytest.fixture(autouse=True)
def simple_transitions(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str | None]]:
    """转换只走状态机的规则并写 issues 表；记下 (Issue, 事件, reason)。"""
    moved: list[tuple[str, str, str | None]] = []

    def apply_event(runtime: Runtime, issue_id: str, event: transitions.IssueEvent, *, reason: str | None = None,
                    actor: str = "user", note: str | None = None, updates: dict[str, Any] | None = None,
                    **_: Any) -> Issue:
        before = issues.get(runtime.conn, issue_id)
        assert before is not None
        after = transitions.transition(before, event, reason=reason, clock=runtime.clock)
        if updates:
            after = replace(after, **updates)
        issues.save(runtime.conn, after, runtime.clock)
        moved.append((issue_id, event.value, reason))
        return after

    def write(runtime: Runtime, issue: Issue, *, body: str | None = None) -> None:
        issues.save(runtime.conn, issue, runtime.clock)

    monkeypatch.setattr(record.transitions, "apply_event", apply_event)
    monkeypatch.setattr(record.files, "write", write)
    return moved


@pytest.fixture
def documents_written(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, dict[str, Any]]]:
    """protocol/documents 的三种文档只记下调用：(种类, 对象, 参数)。"""
    from tightrein.protocol import documents

    written: list[tuple[str, str, dict[str, Any]]] = []

    def recorder(kind: str) -> Callable[..., Path]:
        def write(runtime: Runtime, subject: str, **kwargs: Any) -> Path:
            written.append((kind, subject, kwargs))
            return runtime.workspace.human_document(subject, kind)
        return write

    for kind in ("pending", "failure", "deliver"):
        monkeypatch.setattr(documents, kind, recorder(kind))
    return written


@pytest.fixture
def fake_github(repos: Repos) -> FakeGitHub:
    """gh 的原样命令缺省应答：规则集为空、检查为空(没有检查)。"""
    runner = LocalRunner()
    runner.replies[("gh", "api")] = [Outcome(0, "[]", "", 1, None, None)]
    runner.replies[("gh", "pr", "checks")] = [Outcome(0, "[]", "", 1, None, None)]
    return FakeGitHub(runner, repos.repo, origin=repos.origin)


@pytest.fixture(autouse=True)
def fresh_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    """发布按运行编号缓存的结果(是否要求必需检查、部署记录、是否 fetch 过)每个测试从空开始。"""
    from tightrein.release import deploy, merge

    monkeypatch.setattr(merge, "_CACHE", {})
    monkeypatch.setattr(deploy, "_RECORDS", {})
    monkeypatch.setattr(deploy, "_FETCHED", set())


@pytest.fixture
def github_runtime(tmp_path: Path, repos: Repos, fake_github: FakeGitHub) -> Iterator[Runtime]:
    built = make_runtime(tmp_path, repos.repo, github=fake_github)
    yield built
    built.conn.close()


def stale_at_decision(monkeypatch: pytest.MonkeyPatch, git: Git) -> list[dict[str, str | None]]:
    """让做决定时(第一次)观察到的 HEAD 与执行前重新观察的不同(做决定之后别人动过仓库)；返回每次观察的结果。"""
    real = git.state
    seen: list[dict[str, str | None]] = []

    def state(keys: Sequence[str] = STATE_KEYS, *, base: str = "HEAD") -> dict[str, str | None]:
        found = real(keys, base=base)
        seen.append(found)
        return {**found, "head": "0" * 40} if len(seen) == 1 else found

    monkeypatch.setattr(git, "state", state)
    return seen


@pytest.fixture
def kit() -> SimpleNamespace:
    """测试文件要用的构造函数与常量(importlib 导入模式下测试文件不能直接 import conftest)。"""
    return SimpleNamespace(BRANCH=BRANCH, NOW=NOW, ORDER=ORDER, FIX_ORDER=FIX_ORDER, MAIN_ORDER=MAIN_ORDER,
                           FakeGitHub=FakeGitHub, LocalRunner=LocalRunner,
                           make_runtime=make_runtime, make_settings=make_settings, make_setup=make_setup,
                           new_issue=new_issue, deliver=deliver, delivery_facts=delivery_facts,
                           stale_at_decision=stale_at_decision)
