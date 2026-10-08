"""基准检查：建好 worktree 后先跑准备命令，再在基准 commit 上全量跑项目检查。

- 任一失败判为配置错误、转人工，不进入编码：免得把基准上已有的失败算到修复头上；
- 项目检查的结果按「基准 commit + 命令」缓存在 WorkspaceLayout.cache_dir：同一基准 commit 只跑一次，多个 Issue
  共用(准备命令装依赖，每个 worktree 都要跑，不缓存)；
- 命令按 shlex 拆分、不经 shell；起不来(可执行文件不存在)与失败分开记，都算不通过。
"""

from __future__ import annotations

import hashlib
import json
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from tightrein.implement.check.affected import ProjectCommand, argv_text
from tightrein.protocol.process import Command
from tightrein.protocol.security import child_env
from tightrein.store.files.json import read_json, write_json

if TYPE_CHECKING:
    from tightrein.protocol.runtime import Runtime

PREPARE = "prepare"
# 基准上全量跑的项目检查(项目事实 project.commands 中写了的)
CHECKS = ("test", "lint", "typecheck", "build")
PASSED = "passed"
FAILED = "failed"
NOT_RUN = "not_run"
CACHE_DIR = "baseline"


@dataclass(frozen=True)
class CommandRun:
    name: str
    command: str
    result: str  # passed、failed、not_run
    exit_code: int | None
    detail: str | None  # 失败时的原因：退出码、被终止、起不来


@dataclass(frozen=True)
class Baseline:
    commit: str
    runs: tuple[CommandRun, ...]
    cached: bool

    @property
    def passed(self) -> bool:
        return all(run.result == PASSED for run in self.runs)

    def facts(self) -> dict[str, object]:
        return {"commit": self.commit, "cached": self.cached, "passed": self.passed,
                "commands": [asdict(run) for run in self.runs]}


def prepare(runtime: Runtime, worktree: Path, log: Path) -> list[CommandRun]:
    """准备命令(如装依赖)：project.commands.prepare，每个 worktree 跑一次。"""
    return _run_all(runtime, worktree, _commands(runtime, (PREPARE,)), log)


def check(runtime: Runtime, worktree: Path, commit: str, log: Path) -> Baseline:
    """基准 commit 上的全量项目检查；同一 commit 与同一组命令跑过的直接取缓存。"""
    commands = _commands(runtime, CHECKS)
    cache = runtime.workspace.cache_dir / CACHE_DIR / f"{commit}-{_digest(commands)}.json"
    if cache.is_file():
        data = read_json(cache)
        return Baseline(commit, tuple(CommandRun(**item) for item in data["commands"]), cached=True)
    found = Baseline(commit, tuple(_run_all(runtime, worktree, commands, log)), cached=False)
    # 起不来的命令多半是环境没装好，修好环境后应重跑，不缓存
    if all(run.result != NOT_RUN for run in found.runs):
        write_json(cache, found.facts())
    return found


def describe(runs: Sequence[CommandRun]) -> list[str]:
    return [f"{run.name}：`{run.command}` {run.detail}" for run in runs if run.result != PASSED]


def _commands(runtime: Runtime, names: Sequence[str]) -> dict[str, str]:
    """项目事实中写了的命令；测试命令里的 `{tests}` 去掉(基准上跑全量)。"""
    project = runtime.settings.project
    configured: Mapping[str, str | None] = project.commands if project is not None else {}
    return {name: argv_text(ProjectCommand(name, str(configured[name])), ()) for name in names if configured.get(name)}


def _run_all(runtime: Runtime, worktree: Path, commands: Mapping[str, str], log: Path) -> list[CommandRun]:
    runs = []
    for name, command in commands.items():
        run, output = _run(runtime, worktree, name, command)
        runs.append(run)
        _append(log, f"$ {command}\n{output}")
    return runs


def _run(runtime: Runtime, worktree: Path, name: str, command: str) -> tuple[CommandRun, str]:
    argv = tuple(shlex.split(command))
    outcome = runtime.runner.run(Command(argv=argv, cwd=worktree, env=child_env(runtime.environ),
                                         timeout_s=runtime.settings.duration("limits.timeouts.tests")))
    output = runtime.redactor.text(f"{outcome.stdout}\n{outcome.stderr_tail}".strip())
    if outcome.start_error is not None:
        return CommandRun(name, command, NOT_RUN, None, f"起不来：{outcome.start_error}"), output
    if outcome.stopped_by is not None:
        return CommandRun(name, command, FAILED, None, f"被终止({outcome.stopped_by})"), output
    if outcome.exit_code != 0:
        return CommandRun(name, command, FAILED, outcome.exit_code, f"退出码 {outcome.exit_code}"), output
    return CommandRun(name, command, PASSED, 0, None), output


def _digest(commands: Mapping[str, str]) -> str:
    return hashlib.sha256(json.dumps(commands, sort_keys=True).encode()).hexdigest()[:12]


def _append(log: Path, text: str) -> None:
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as handle:
        handle.write(text.rstrip() + "\n\n")
