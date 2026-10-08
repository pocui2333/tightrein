"""试跑(接入第 4 步，`tightrein project check`)：每个启用与自定义的模块各试跑一次，只取数据，不调用模型。

- 自定义：按方法文档的协议跑项目的脚本：请求 JSON 写入标准输入，每次调用一个临时 scratch 目录(结束即删)；
  退出码非 0 但标准输出是合法响应时以响应为准；标准输出不是单个 JSON 对象为协议错误；按方法文档旁的
  `<文档名>.schema.json` 校验，列出每条错误的路径；
- 启用：调用该模块的取数程序(`collect.<模块>.source.collect`)一次，结果不交给去重、读取位置不前进；
  会调用模型的模块(MODEL_MODULES)与没有取数程序的模块记为跳过，不算不通过；
- 基线：只读 worktree 切到主分支，把 settings.json 中的检查命令全量跑一次，确认基线可用(接入期只做只读的事：
  不采集入库、不改代码、不提 PR)；
- 结果存 store 的 state 表(`onboard.check`，带 setup.json 的哈希)，`project ready` 据此判断；setup.md 据此渲染。
"""

from __future__ import annotations

import hashlib
import importlib
import json
import shlex
import sqlite3
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from tightrein.onboard.setup import ModuleSetup, ModuleStatus
from tightrein.protocol.git import GitError
from tightrein.protocol.git.worktrees import create_readonly, sync_readonly
from tightrein.protocol.handoff import load_schema, schema_errors
from tightrein.protocol.naming import format_iso
from tightrein.protocol.process import Command
from tightrein.protocol.runtime import Runtime
from tightrein.protocol.scripts import secret_env
from tightrein.protocol.security import child_env
from tightrein.store.tables import state

STATE_KEY = "onboard.check"
READY_KEY = "onboard.ready"
POINT = "onboard.check"
PASSED, FAILED, SKIPPED = "passed", "failed", "skipped"
MODEL_MODULES = frozenset({"collect.static"})  # 取数即要调用模型的模块：试跑不做
PACKAGE_ROOT = Path(__file__).resolve().parents[1]  # src/tightrein：方法文档的路径相对它写
BASELINE_WORKTREE = "baseline"
STDERR_TAIL_LINES = 20
PYTHON_SUFFIX = ".py"


@dataclass(frozen=True)
class Trial:
    key: str  # 模块键；基线检查为 `baseline.<命令名>`
    status: str  # passed、failed、skipped
    detail: str
    errors: tuple[str, ...] = ()

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Trial:
        return cls(data["key"], data["status"], data["detail"], tuple(data.get("errors") or ()))


@dataclass(frozen=True)
class Report:
    at: str
    setup_hash: str
    trials: list[Trial] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(trial.status != FAILED for trial in self.trials)

    def to_json(self) -> dict[str, Any]:
        return {"at": self.at, "setupHash": self.setup_hash, "passed": self.passed,
                "trials": [asdict(trial) | {"errors": list(trial.errors)} for trial in self.trials]}

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Report:
        return cls(data["at"], data["setupHash"], [Trial.from_json(item) for item in data.get("trials") or []])


def check(runtime: Runtime, *, only: str | None = None, baseline: bool = True) -> Report:
    """试跑并把结果存进 state 表；only 只试跑一个模块(不跑基线)，与上次其余模块的结果合并后保存。"""
    keys = [key for key, module in runtime.setup.modules.items()
            if module.status is not ModuleStatus.DISABLED and (only is None or key == only)]
    if only is not None and only not in runtime.setup.modules:
        raise LookupError(f"接入清单中没有 {only}")
    trials = [trial_module(runtime, runtime.setup.module(key)) for key in keys]
    if baseline and only is None:
        trials += run_baseline(runtime)
    if only is not None:
        previous = last_report(runtime.conn)
        kept = [trial for trial in previous.trials if trial.key != only] if previous is not None else []
        trials = kept + trials
    report = Report(format_iso(runtime.clock.now()), setup_hash(runtime.workspace.setup), trials)
    state.put(runtime.conn, STATE_KEY, report.to_json(), runtime.clock)
    state.delete(runtime.conn, READY_KEY)  # 重新试跑后要再执行一次 ready
    return report


def last_report(conn: sqlite3.Connection) -> Report | None:
    data = state.get(conn, STATE_KEY)
    return None if data is None else Report.from_json(data)


def setup_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def ready_problems(report: Report | None, current_hash: str) -> list[str]:
    """标为就绪前的条件：试跑过、之后没改过 setup.json、全部通过。"""
    if report is None:
        return ["还没有试跑：先执行 tightrein project check"]
    problems = []
    if report.setup_hash != current_hash:
        problems.append("setup.json 在上次试跑之后改过：重新执行 tightrein project check")
    problems += [f"{trial.key}：{trial.detail}" for trial in report.trials if trial.status == FAILED]
    return problems


def trial_module(runtime: Runtime, module: ModuleSetup) -> Trial:
    if module.status is ModuleStatus.CUSTOM:
        return trial_script(runtime, module)
    return trial_program(runtime, module)


def trial_program(runtime: Runtime, module: ModuleSetup) -> Trial:
    """启用的模块：调用它的取数程序一次。"""
    if module.key in MODEL_MODULES:
        return Trial(module.key, SKIPPED, "取数即要调用模型，试跑不做")
    stage, _, name = module.key.partition(".")
    if stage != "collect":
        return Trial(module.key, SKIPPED, "没有可单独试跑的取数程序，在第一次运行时检查")
    try:
        source = importlib.import_module(f"tightrein.collect.{name}.source")
    except ModuleNotFoundError as error:
        return Trial(module.key, FAILED, f"找不到取数程序：{error.name}")
    result = source.collect(runtime)
    status = str(result.status)
    if status == "done":
        return Trial(module.key, PASSED, f"读到 {result.read} 条")
    if status == "skipped":
        return Trial(module.key, SKIPPED, result.reason or "")
    return Trial(module.key, FAILED, result.reason or status)


def trial_script(runtime: Runtime, module: ModuleSetup) -> Trial:
    """自定义模块：按方法文档的协议跑一次项目的脚本。"""
    assert module.script is not None and module.guide is not None  # setup.load 已按状态校验必填
    missing = [name for name in module.secrets if name not in runtime.secrets]
    if missing:
        return Trial(module.key, FAILED, f"secrets.json 中没有登记的凭据：{', '.join(missing)}")
    script = runtime.workspace.root / module.script
    argv = (sys.executable, str(script)) if script.suffix == PYTHON_SUFFIX else (str(script),)
    values = {secret_env(name): runtime.secrets[name] for name in module.secrets}
    with tempfile.TemporaryDirectory(prefix="tightrein-check-") as scratch:
        request = {"trial": True, "project": runtime.setup.project, "module": module.key, "scratch": scratch}
        outcome = runtime.runner.run(Command(
            argv, runtime.workspace.root, child_env(runtime.environ, set_values=values),
            stdin=json.dumps(request, ensure_ascii=False), timeout_s=runtime.settings.duration("limits.timeouts.command")))
    stderr = runtime.redactor.text(outcome.stderr_tail)
    return interpret(module.key, outcome.exit_code, outcome.stdout, stderr, start_error=outcome.start_error,
                     stopped_by=outcome.stopped_by, schema=_schema(module.guide))


def interpret(key: str, exit_code: int | None, stdout: str, stderr: str, *, start_error: str | None,
              stopped_by: str | None, schema: dict[str, Any] | None) -> Trial:
    """脚本结果归类。非 0 退出码只在标准输出不是合法响应时才算失败。"""
    if start_error is not None:
        return Trial(key, FAILED, f"无法启动：{start_error}")
    if stopped_by is not None:
        return Trial(key, FAILED, f"被终止({stopped_by})")
    response = _single_object(stdout)
    tail = tuple(stderr.strip().splitlines()[-STDERR_TAIL_LINES:])
    crashed = exit_code != 0
    if response is None:
        if crashed:
            return Trial(key, FAILED, f"退出码 {exit_code}，标准输出没有合法的响应", tail)
        return Trial(key, FAILED, "协议错误：标准输出不是单个 JSON 对象")
    errors = tuple(schema_errors(response, schema)) if schema is not None else ()
    if errors:
        detail = f"退出码 {exit_code}，标准输出没有合法的响应" if crashed else "输出不符合方法文档的格式"
        return Trial(key, FAILED, detail, errors + (tail if crashed else ()))
    note = "" if schema is not None else "；方法文档没有 schema，只检查了是单个 JSON 对象"
    return Trial(key, PASSED, (f"退出码 {exit_code}，以响应为准" if crashed else "输出合格") + note)


def run_baseline(runtime: Runtime) -> list[Trial]:
    """只读 worktree 切到主分支，全量跑一次检查命令。"""
    project = runtime.settings.project
    commands = {name: command for name, command in (project.commands if project else {}).items() if command}
    if not commands:
        return [Trial("baseline", SKIPPED, "settings.json 中没有检查命令")]
    path = runtime.workspace.worktree(BASELINE_WORKTREE)
    try:
        if not path.is_dir():
            create_readonly(runtime.git, path, scope=runtime.scope(runtime.setup.project, POINT))
        synced = sync_readonly(runtime.git, path, marker=_marker(runtime))
    except GitError as error:
        return [Trial("baseline", FAILED, f"只读 worktree 切到主分支失败：{error}")]
    return [_baseline_command(runtime, synced.worktree, name, command, synced.commit)
            for name, command in commands.items()]


def _baseline_command(runtime: Runtime, worktree: Path, name: str, command: str, commit: str) -> Trial:
    key = f"baseline.{name}"
    try:
        argv = tuple(shlex.split(command))
    except ValueError as error:
        return Trial(key, FAILED, f"命令写法不对：{error}")
    outcome = runtime.runner.run(Command(argv, worktree, child_env(runtime.environ),
                                         timeout_s=runtime.settings.duration("limits.timeouts.tests")))
    if outcome.start_error is not None or outcome.stopped_by is not None or outcome.exit_code != 0:
        reason = outcome.start_error or outcome.stopped_by or f"退出码 {outcome.exit_code}"
        tail = tuple(runtime.redactor.text(outcome.stderr_tail).strip().splitlines()[-STDERR_TAIL_LINES:])
        return Trial(key, FAILED, f"`{command}` 在 {commit[:12]} 上不通过({reason})", tail)
    return Trial(key, PASSED, f"`{command}` 在 {commit[:12]} 上通过")


def _marker(runtime: Runtime) -> Path:
    return runtime.workspace.worktrees_dir / f"readonly-{BASELINE_WORKTREE}.json"


def _single_object(stdout: str) -> dict[str, Any] | None:
    try:
        value = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _schema(guide: str) -> dict[str, Any] | None:
    path = (PACKAGE_ROOT / guide).with_suffix(".schema.json")
    return load_schema(path) if path.is_file() else None


def summary(trials: Sequence[Trial]) -> dict[str, int]:
    return {status: sum(trial.status == status for trial in trials) for status in (PASSED, FAILED, SKIPPED)}
