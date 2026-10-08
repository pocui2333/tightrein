"""命令行测试共用：临时的工具目录与一个接好的项目 shop，假的外部依赖(Externals)，直接调用 main。

- 子进程一律由 FakeRunner 应答(缺省退出码 1：git 读不到远程，GitHub 为空)，不联网、不调用模型；
- main 不安装信号处理(只在 entry 里装)，测试进程的信号处理不受影响。
"""

import io
import json
import os
import shutil
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from tightrein.cli import main as cli_main
from tightrein.cli.assemble import Externals
from tightrein.onboard.setup import MODULES
from tightrein.protocol.naming import FixedClock
from tightrein.protocol.process import Command, Outcome
from tightrein.store.db import open_database
from tightrein.store.files.layout import ToolLayout, WorkspaceLayout

NOW = datetime(2026, 10, 8, 3, 0, tzinfo=UTC)
HOST = "test-host"
PROJECT = "shop"
DEFAULTS = Path(__file__).resolve().parents[2] / "settings" / "defaults.json"


class FakeRunner:
    """记下每条命令；answers 按 argv[0] 给应答，没有的退出码为 1。"""

    def __init__(self) -> None:
        self.commands: list[Command] = []
        self.answers: dict[str, Outcome] = {}

    def run(self, command: Command) -> Outcome:
        self.commands.append(command)
        return self.answers.get(command.argv[0], Outcome(1, "", "", 1, None, None))


@dataclass
class Cli:
    tool: ToolLayout
    layout: WorkspaceLayout
    externals: Externals
    runner: FakeRunner
    terminated: list[int] = field(default_factory=list)

    def __call__(self, *argv: str, stdin: str = "") -> SimpleNamespace:
        out, err = io.StringIO(), io.StringIO()
        code = cli_main.main(list(argv), self.externals, stdin=io.StringIO(stdin), stdout=out, stderr=err)
        return SimpleNamespace(code=code, out=out.getvalue(), err=err.getvalue())

    def json(self, *argv: str) -> dict[str, Any]:
        result = self(*argv, "--json")
        lines = result.out.splitlines()
        assert len(lines) == 1, result.out  # 标准输出只有一个 JSON 对象
        data: dict[str, Any] = json.loads(lines[0])
        return data

    def conn(self) -> Any:
        return open_database(self.layout.database, clock=self.externals.clock)

    def save(self, table: ModuleType, record: Any) -> None:
        """table 为 store.tables 下的模块(issues、problems…)。"""
        conn = self.conn()
        table.save(conn, record, self.externals.clock)
        conn.close()

    def load(self, table: ModuleType, key: str) -> Any:
        conn = self.conn()
        found = table.get(conn, key)
        conn.close()
        return found


def write_workspace(tool: ToolLayout, name: str, repo: Path) -> WorkspaceLayout:
    layout = tool.workspace(name)
    layout.data_dir.mkdir(parents=True)
    facts = {"repo": str(repo), "mainBranch": "main", "language": "zh",
             "commands": {"test": None, "lint": None, "build": None, "typecheck": None},
             "testPatterns": ["tests/"], "frontendPatterns": []}
    layout.settings.write_text(json.dumps({"project": facts, "overrides": {}}), encoding="utf-8")
    modules = {key: {"status": "disabled", "method": None, "script": None, "guide": None, "reason": "测试不用",
                     "impact": "无"} for key in MODULES}
    layout.setup.write_text(json.dumps({"project": name, "updatedAt": "2026-10-08T00:00:00Z", "modules": modules}),
                            encoding="utf-8")
    return layout


@pytest.fixture
def cli(tmp_path: Path) -> Iterator[Cli]:
    tool = ToolLayout(tmp_path / "tool")
    tool.settings_dir.mkdir(parents=True)
    shutil.copy(DEFAULTS, tool.defaults)
    repo = tmp_path / PROJECT
    (repo / ".git").mkdir(parents=True)
    layout = write_workspace(tool, PROJECT, repo)
    home = tmp_path / "home"
    home.mkdir()
    runner = FakeRunner()
    terminated: list[int] = []
    externals = Externals(
        environ={"PATH": str(tmp_path / "bin"), "HOME": str(home), "USER": "alice"}, runner=runner,
        clock=FixedClock(NOW), tool=tool, home=home, cwd=tmp_path, program=tmp_path / "venv" / "bin" / "tightrein",
        pid=os.getpid(), host=HOST, uid=501, stdin_is_tty=lambda: False, alive=lambda pid: pid == os.getpid(),
        terminate=terminated.append, platform="linux",
    )
    cli_main.build_parser.cache_clear()
    yield Cli(tool, layout, externals, runner, terminated)
