"""静态巡检的测试共用：真实的 git 仓库(origin 为本地裸仓库)、按调用点应答的假模型调用、只读 worktree 所在的运行时。

git 以隔离的环境运行，不读本机的全局与系统配置；模型调用不经 agents.call，由 FakeCaller 按调用点给出结构化结果。
"""

import os
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tightrein.agents.call import AgentContext
from tightrein.agents.params import CallParams
from tightrein.agents.result import CallResult, CallStatus
from tightrein.protocol.git import Git
from tightrein.protocol.limits import Breaker
from tightrein.protocol.process import SubprocessRunner
from tightrein.protocol.records import EventLog
from tightrein.protocol.resources import IssueBudget, Quota, Slots
from tightrein.protocol.runtime import Runtime
from tightrein.store.files.layout import ToolLayout

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
SERVICE = """def list_orders(page):
    size = 20
    rows = query(page * size)
    return rows


def delete_order(order_id):
    db.delete(order_id)
"""


class Repos:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.origin = root / "origin.git"
        self.repo = root / "repo"

    def git(self, repo: Path, *args: str) -> str:
        done = subprocess.run(["git", *args], cwd=repo, env=GIT_ENV, capture_output=True, text=True, check=False)
        if done.returncode != 0:
            raise AssertionError(f"git {' '.join(args)} 失败：{done.stderr}")
        return done.stdout

    def init(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.git(self.root, "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.git(self.root, "init", "-q", "-b", "main", str(self.repo))
        self.git(self.repo, "remote", "add", "origin", str(self.origin))
        self.push("chore: init", {"README.md": "# demo\n", "src/orders.py": SERVICE,
                                  "tests/test_orders.py": "def test_x():\n    pass\n"})

    def push(self, message: str, files: Mapping[str, str]) -> str:
        for path, text in files.items():
            target = self.repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        self.git(self.repo, "add", "-A")
        self.git(self.repo, "commit", "-q", "-m", message)
        self.git(self.repo, "push", "-q", "-u", "origin", "main")
        return self.git(self.repo, "rev-parse", "HEAD").strip()


class FakeCaller:
    """按调用点应答：answers[调用点] 为输出字典、CallResult 或「(params) -> 两者之一」的函数。"""

    def __init__(self, answers: Mapping[str, Any]) -> None:
        self.answers = dict(answers)
        self.calls: list[CallParams] = []

    def __call__(self, params: CallParams, context: Any) -> CallResult:
        self.calls.append(params)
        answer = self.answers[params.point]
        if callable(answer):
            answer = answer(params)
        if isinstance(answer, CallResult):
            return answer
        return CallResult(CallStatus.OK, "claude", "opus", output=answer)

    def points(self) -> list[str]:
        return [params.point for params in self.calls]


def failed_call(status: CallStatus, violations: tuple[str, ...] = ()) -> CallResult:
    return CallResult(status, "claude", "opus", error=status.value, violations=list(violations))


def claim(file: str = "src/orders.py", line: int = 3, *, rule: str = "unbounded-page", severity: str = "high",
          layer: str = "incremental", trigger: str = "page 为负数时") -> dict[str, Any]:
    return {"file": file, "line": line, "ruleOrPattern": rule, "layer": layer, "severity": severity,
            "statement": f"{rule} 的疑点", "trigger": trigger}


def verdict(value: str = "confirmed", *, file: str = "src/orders.py", line: int = 3,
            symbol: str | None = "list_orders") -> dict[str, Any]:
    return {"analysis": "读了 list_orders", "verdict": value,
            "facts": [{"location": f"{file}:{line}", "observation": "没有校验 page"}],
            "trigger": "page=-1" if value in ("confirmed", "conditional") else None,
            "counterEvidence": [{"check": "上游校验", "entry": f"{file}:1",
                                 "upstreamValidation": {"status": "absent", "location": None}, "result": "没挡住"}],
            "impact": {"kind": "data-correctness", "callSites": [f"{file}:1"], "consequence": "查出错误的数据"}
            if value in ("confirmed", "conditional") else None,
            "sourceOfPhenomenon": None,
            "rootCauses": [{"file": file, "line": line, "symbol": symbol}] if value != "refuted" else [],
            "missingInfo": []}


@pytest.fixture
def repos(tmp_path: Path) -> Repos:
    found = Repos(tmp_path / "git")
    found.init()
    return found


@pytest.fixture
def fake_caller() -> type[FakeCaller]:
    return FakeCaller


@pytest.fixture
def static_runtime(source_runtime: Callable[..., SimpleNamespace], repos: Repos) -> Callable[..., Runtime]:
    """以 repos.repo 为项目主仓库的运行时；controls 覆盖 settings 的控制键。用真实的 Runtime：并行取证经
    protocol.runtime.isolated 在线程里另开连接，要求 Runtime 与 AgentContext 是 dataclass。"""

    def make(*, controls: Mapping[str, Mapping[str, Any]] | None = None,
             run: str = "R-20261005T030000Z-collect") -> Runtime:
        base = source_runtime(modules={"collect.static": {}}, controls=controls, runner=SubprocessRunner(),
                              environ=GIT_ENV)
        events = EventLog(base.workspace.root / "events.jsonl", base.redactor, base.clock)
        agents = AgentContext(base.settings, base.workspace, base.conn, base.clock, base.runner, base.redactor,
                              events, base.environ, Breaker(base.conn, base.clock, base.settings),
                              Quota(base.conn, base.clock, base.settings),
                              IssueBudget(base.conn, base.clock, base.settings))
        git = Git(repos.repo, base.runner, GIT_ENV, base.settings, sleep=lambda seconds: None)
        return Runtime(tool=ToolLayout.discover(), workspace=base.workspace, settings=base.settings,
                       setup=base.setup, conn=base.conn, clock=base.clock, runner=base.runner,
                       redactor=base.redactor, secrets=base.secrets, environ=base.environ, run=run, events=events,
                       agents=agents, git=git, github=None, slots=Slots(base.settings))

    return make


@pytest.fixture
def make_claim() -> Callable[..., dict[str, Any]]:
    return claim


@pytest.fixture
def make_verdict() -> Callable[..., dict[str, Any]]:
    return verdict


@pytest.fixture
def make_failed() -> Callable[..., CallResult]:
    return failed_call


@pytest.fixture
def service_text() -> str:
    return SERVICE
