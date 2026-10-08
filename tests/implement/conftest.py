"""implement 测试共用的夹具：真 git 仓库当修复 worktree、组装好的 Runtime 与 ImplementContext。

git 照常真实执行(隔离环境，不读本机配置)；项目检查命令、Playwright、Schemathesis 等其余子进程按 argv 前缀预置结果
(`world.respond`)。不联网、不调用模型：模型调用由各测试 monkeypatch 掉 agents 的 call。
测试文件不直接 import 本文件，只用夹具：`world`、`new_world`(换配置、接入清单或凭据)、`settings_with`、`setup_with`。
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tightrein.implement.context import ImplementContext
from tightrein.onboard.setup import MODULES, ModuleSetup, ModuleStatus, Setup
from tightrein.protocol.git import Git
from tightrein.protocol.handoff import Handoff, Status
from tightrein.protocol.naming import FixedClock, format_iso
from tightrein.protocol.process import Command, Outcome
from tightrein.protocol.records import EventLog
from tightrein.protocol.resources import Slots
from tightrein.protocol.runtime import Runtime
from tightrein.protocol.security import Redactor
from tightrein.settings.load import ProjectFacts, Settings
from tightrein.store.db import open_database
from tightrein.store.files.layout import ToolLayout, WorkspaceLayout
from tightrein.store.tables import issues
from tightrein.store.tables.issues import Issue

ROOT = Path(__file__).resolve().parents[2]
DEFAULTS = ROOT / "settings" / "defaults.json"
NOW = datetime(2026, 10, 8, 3, 0, tzinfo=UTC)
RUN = "R-20261008T030000Z-implement"
ISSUE = "0018"
BODY = "## 问题\n\n订单列表只显示当天的订单。\n\n## 验收标准\n\n- 列表显示最近 7 天的订单\n- 接口 GET /api/orders 返回 200\n"
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
BASE_FILES = {
    "src/orders.py": "def recent(days):\n    return days\n",
    "src/users.py": "NAME = 'a'\n",
    "tests/test_orders.py": "from src.orders import recent\n\n\ndef test_recent():\n    assert recent(1) == 1\n",
    "README.md": "# demo\n",
}


@dataclass
class Reply:
    """预置的子进程结果；stdout 在命令给了 stdout_path 时写进那个文件(与真实的 ProcessRunner 一致)。"""

    exit_code: int | None = 0
    stdout: str = ""
    stderr: str = ""
    stopped_by: str | None = None
    start_error: str | None = None
    effect: Callable[[Command], None] | None = None  # 执行时的副作用(写结果文件、改工作区等)


class Runner:
    """git 真实执行；其他命令按 argv 前缀(最长者优先)依次取预置结果，最后一个一直沿用。"""

    def __init__(self) -> None:
        self.commands: list[Command] = []
        self.replies: dict[tuple[str, ...], list[Reply]] = {}

    def reply(self, prefix: tuple[str, ...], *replies: Reply) -> None:
        self.replies[prefix] = list(replies)

    def others(self) -> list[Command]:
        return [command for command in self.commands if command.argv[0] != "git"]

    def run(self, command: Command) -> Outcome:
        self.commands.append(command)
        for prefix, replies in sorted(self.replies.items(), key=lambda item: -len(item[0])):
            if tuple(command.argv[:len(prefix)]) == prefix and replies:
                found = replies.pop(0) if len(replies) > 1 else replies[0]
                if found.effect is not None:
                    found.effect(command)
                if command.stdout_path is not None:
                    command.stdout_path.parent.mkdir(parents=True, exist_ok=True)
                    command.stdout_path.write_text(found.stdout, encoding="utf-8")
                return Outcome(found.exit_code, "" if command.stdout_path else found.stdout, found.stderr, 1,
                               found.stopped_by, found.start_error)
        if command.argv[0] != "git":
            raise AssertionError(f"没有预置结果的命令：{command.argv}")
        started = time.monotonic()
        done = subprocess.run(list(command.argv), cwd=command.cwd, env=dict(command.env), input=command.stdin,
                              capture_output=True, text=True, timeout=command.timeout_s, check=False)
        return Outcome(done.returncode, done.stdout, done.stderr, int((time.monotonic() - started) * 1000), None, None)


class Repo:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.mkdir(parents=True)
        self.git("init", "-q", "-b", "main")
        self.base = self.commit("chore: init", BASE_FILES)

    def git(self, *args: str) -> str:
        done = subprocess.run(["git", *args], cwd=self.path, env=GIT_ENV, capture_output=True, text=True, check=False)
        if done.returncode != 0:
            raise AssertionError(f"git {' '.join(args)} 失败：{done.stderr}")
        return done.stdout

    def write(self, files: dict[str, str | None]) -> None:
        for path, text in files.items():
            target = self.path / path
            if text is None:
                target.unlink()
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")

    def commit(self, message: str, files: dict[str, str | None]) -> str:
        self.write(files)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD").strip()


def make_settings(*overrides: dict[str, Any], commands: dict[str, str | None] | None = None) -> Settings:
    defaults = json.loads(DEFAULTS.read_text(encoding="utf-8"))
    project = ProjectFacts(repo=None, main_branch="main", language="zh",
                           commands=commands if commands is not None else {"test": "pytest -q {tests}",
                                                                           "lint": "ruff check ."},
                           test_patterns=("tests/",))
    return Settings.from_data(defaults, *overrides, project=project)


def make_setup(**modules: ModuleSetup) -> Setup:
    found = {key: ModuleSetup(key, ModuleStatus.DISABLED, None, None, None, "测试中不启用", None) for key in MODULES}
    found.update({key.replace("__", "."): value for key, value in modules.items()})
    return Setup(project="demo", updated_at=format_iso(NOW), modules=found)


@dataclass
class World:
    tmp: Path
    repo: Repo
    runner: Runner
    clock: FixedClock
    runtime: Runtime
    calls: list[Any] = field(default_factory=list)

    def git(self) -> Git:
        return Git(self.repo.path, self.runner, GIT_ENV, self.runtime.settings)

    def respond(self, prefix: tuple[str, ...], *replies: dict[str, Any]) -> None:
        """给以 prefix 开头的命令预置结果，每项为 Reply 的字段。"""
        self.runner.reply(prefix, *(Reply(**item) for item in replies))

    def context(self, *, round: int = 1, latest: dict[str, Handoff] | None = None, decisions: list[Any] | None = None,
                body: str = BODY) -> ImplementContext:
        issue = issues.get(self.runtime.conn, ISSUE)
        assert issue is not None
        return ImplementContext(issue=issue, body=body, notes=None, knowledge=[], decisions=decisions or [],
                                worktree=self.repo.path, git=self.git(), base_commit=self.repo.base, round=round,
                                latest=latest or {})

    def handoff(self, point: str, facts: dict[str, Any], status: Status = Status.PASSED, *,
                at: datetime = NOW) -> Handoff:
        return Handoff(point=point, subject=ISSUE, run=RUN, status=status, summary=f"{point} 的结论", facts=facts,
                       created_at=format_iso(at))


def make_world(tmp: Path, *, settings: Settings | None = None, setup: Setup | None = None,
               secrets: dict[str, str] | None = None) -> World:
    clock = FixedClock(NOW)
    repo = Repo(tmp / "repo")
    workspace = WorkspaceLayout(tmp / "workspaces" / "demo")
    conn = open_database(workspace.database, clock=clock)
    issues.save(conn, Issue(id=ISSUE, status="implementing", title="订单列表只显示当天", kind="bug", origin="problem",
                            branch="fix/18-orders"), clock)
    redactor = Redactor()
    runner = Runner()
    settings = settings or make_settings()
    runtime = Runtime(
        tool=ToolLayout(ROOT), workspace=workspace, settings=settings, setup=setup or make_setup(), conn=conn,
        clock=clock, runner=runner, redactor=redactor, secrets=secrets or {}, environ=GIT_ENV, run=RUN,
        events=EventLog(workspace.events(RUN), redactor, clock), agents=SimpleNamespace(),  # type: ignore[arg-type]
        git=Git(repo.path, runner, GIT_ENV, settings), github=None, slots=Slots(settings),
    )
    return World(tmp, repo, runner, clock, runtime)


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    found = make_world(tmp_path)
    yield found
    found.runtime.conn.close()


@pytest.fixture
def new_world(tmp_path: Path) -> Iterator[Callable[..., World]]:
    made: list[World] = []

    def build(**options: Any) -> World:
        found = make_world(tmp_path / f"w{len(made)}", **options)
        made.append(found)
        return found

    yield build
    for found in made:
        found.runtime.conn.close()


@pytest.fixture
def settings_with() -> Callable[..., Settings]:
    return make_settings


@pytest.fixture
def setup_with() -> Callable[..., Setup]:
    return make_setup
