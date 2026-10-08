"""protocol/git 测试共用的夹具：真实的 git 仓库(origin 为本地裸仓库)、按顺序应答的假进程、临时数据库与配置。

git 以隔离的环境运行，不读本机的全局与系统配置(签名、hook 等个人设置不影响测试)，作者与时区固定。
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tightrein.protocol.git.git import Git, WriteScope
from tightrein.protocol.naming import FixedClock
from tightrein.protocol.process import Command, Outcome
from tightrein.settings.load import ProjectFacts, Settings
from tightrein.store.db import open_database
from tightrein.store.tables import operations

DEFAULTS = Path(__file__).resolve().parents[3] / "settings" / "defaults.json"
NOW = datetime(2026, 10, 7, 3, 0, tzinfo=UTC)
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
    """真实执行命令的简化 ProcessRunner：记下每条命令，便于断言参数与环境。"""

    def __init__(self) -> None:
        self.commands: list[Command] = []

    def run(self, command: Command) -> Outcome:
        self.commands.append(command)
        started = time.monotonic()
        try:
            done = subprocess.run(list(command.argv), cwd=command.cwd, env=dict(command.env), input=command.stdin,
                                  capture_output=True, text=True, timeout=command.timeout_s, check=False)
        except FileNotFoundError as error:
            return Outcome(None, "", "", 0, None, str(error))
        return Outcome(done.returncode, done.stdout, done.stderr, int((time.monotonic() - started) * 1000), None,
                       None)


class ScriptedRunner:
    """按顺序返回预置结果的假进程：每项为 Outcome，或 (退出码, 标准输出, 错误输出)。"""

    def __init__(self, *outcomes: Outcome | tuple[int, str, str]) -> None:
        self.outcomes = list(outcomes)
        self.commands: list[Command] = []

    def run(self, command: Command) -> Outcome:
        self.commands.append(command)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Outcome):
            return outcome
        code, stdout, stderr = outcome
        return Outcome(code, stdout, stderr, 1, None, None)


class GitRepos:
    def __init__(self, root: Path) -> None:
        self.root = root

    def git(self, repo: Path, *args: str) -> str:
        done = subprocess.run(["git", *args], cwd=repo, env=GIT_ENV, capture_output=True, text=True, check=False)
        if done.returncode != 0:
            raise AssertionError(f"git {' '.join(args)} 失败：{done.stderr}")
        return done.stdout

    def write(self, repo: Path, path: str, text: str) -> Path:
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return target

    def commit(self, repo: Path, message: str, files: dict[str, str] | None = None) -> str:
        for path, text in (files or {}).items():
            self.write(repo, path, text)
        self.git(repo, "add", "-A")
        self.git(repo, "commit", "-q", "-m", message)
        return self.head(repo)

    def head(self, repo: Path) -> str:
        return self.git(repo, "rev-parse", "HEAD").strip()

    def origin_and_clone(self) -> tuple[Path, Path]:
        """origin 为裸仓库，main 上有一个提交；返回 (origin, 工作仓库)。"""
        origin, repo = self.root / "origin.git", self.root / "repo"
        self.root.mkdir(parents=True, exist_ok=True)
        self.git(self.root, "init", "-q", "--bare", "-b", "main", str(origin))
        self.git(self.root, "init", "-q", "-b", "main", str(repo))
        self.git(repo, "remote", "add", "origin", str(origin))
        self.write(repo, ".gitignore", "bin/\n")
        self.commit(repo, "chore: init", {
            "README.md": "# demo\n",
            "src/OrderService.cs": "class OrderService\n{\n    int Page = 1;\n}\n",
            "tests/OrderTests.cs": "class OrderTests {}\n",
        })
        self.git(repo, "push", "-q", "-u", "origin", "main")
        return origin, repo

    def upstream(self, origin: Path, message: str, files: dict[str, str]) -> str:
        """另一个 clone 在 origin/main 上提交并推送，返回该提交。"""
        other = self.root / "other"
        if not other.exists():
            self.git(self.root, "clone", "-q", str(origin), str(other))
        self.git(other, "pull", "-q", "origin", "main")
        commit = self.commit(other, message, files)
        self.git(other, "push", "-q", "origin", "main")
        return commit


def make_settings(*overrides: dict[str, object], test_patterns: tuple[str, ...] = ("tests/",)) -> Settings:
    defaults = json.loads(DEFAULTS.read_text(encoding="utf-8"))
    project = ProjectFacts(repo=None, main_branch="main", language="zh", test_patterns=test_patterns)
    return Settings.from_data(defaults, *overrides, project=project)


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def settings_with() -> Callable[..., Settings]:
    """按覆盖层生成配置：settings_with({"git": {...}})。"""
    return make_settings


@pytest.fixture
def scripted() -> type[ScriptedRunner]:
    return ScriptedRunner


@pytest.fixture
def local_runner() -> type[LocalRunner]:
    return LocalRunner


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(NOW)


@pytest.fixture
def conn(tmp_path: Path, clock: FixedClock) -> Iterator[sqlite3.Connection]:
    connection = open_database(tmp_path / "data" / "tightrein.db", clock=clock)
    yield connection
    connection.close()


@pytest.fixture
def scope(conn: sqlite3.Connection, clock: FixedClock) -> WriteScope:
    return WriteScope(conn, clock, "0007", "release.pr")


@pytest.fixture
def interrupt(scope: WriteScope) -> Callable[[str], None]:
    """模拟上次执行在动作中途被中断：该幂等键停在进行中。"""
    def stop(key: str) -> None:
        def crash() -> None:
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            operations.run_once(scope.conn, key, crash, scope.clock)
        found = operations.get(scope.conn, key)
        assert found is not None and found.status == operations.IN_PROGRESS

    return stop


@pytest.fixture
def repos(tmp_path: Path) -> GitRepos:
    return GitRepos(tmp_path / "git")


@pytest.fixture
def clone(repos: GitRepos, settings: Settings) -> tuple[GitRepos, Path, Path, Git]:
    """(repos, origin, 工作仓库, 工作仓库上的 Git)。"""
    origin, repo = repos.origin_and_clone()
    return repos, origin, repo, Git(repo, LocalRunner(), GIT_ENV, settings, sleep=lambda seconds: None)


@pytest.fixture
def fix_branch(clone: tuple[GitRepos, Path, Path, Git]) -> tuple[GitRepos, Path, Git]:
    """工作仓库切到修复分支 cty/fix-order：(repos, origin, Git)。"""
    repos, origin, repo, git = clone
    repos.git(repo, "checkout", "-q", "-b", "cty/fix-order")
    return repos, origin, git
