"""以 --output 沙箱模式运行被测模块(architecture/03 2.6.3，design 15.5)。

被测模块(triage、fix、issue)在各自的流水线模块中实现；本模块定义模块运行器的接口 ModuleRunner 与按命令行启动子进程的
SubprocessModuleRunner，测试以临时目录中的 Python 脚本扮演模块。每次运行启动一个子进程：

    <入口> <模块> --input <用例>/input/<交接文档> --output <outputs/变体/用例/次数> --commit <input.commit>
      --ignore-state --runner <执行器> [--model <模型>] [--gate-decisions <用例>/gates.json] [用例的附加参数]
      --workspace <快照>/workspaces/<项目>

入口缺省为 `python -m tightrein`，PYTHONPATH 指向快照中的 core/，因此运行的是该版本的核心与 skill。子进程在独立
进程组中运行，超时后整个进程组被终止(复用 extensions.invoke.SubprocessRunner)。
运行结束后从输出目录收集：交接文档(handoff/ 下恰好一份，且符合交接文档的 schema)、执行器结果
(raw/runner/*/result.json)、事件文件中 invoke_agent 的用量、会话记录路径、改动(changes.patch，修复模块在 --output
模式下写出)与标准错误。

运行结果的归类：
- 交接文档 ok 且执行器没有达到上限、输出没有不合 schema：进入评分；
- 交接文档 blocked 或 failed，或执行器结果为 limit-reached、schema-invalid：全部适用项记为 fail；
- 执行器无法启动(tool-unavailable)：环境问题，由评测运行器停止整个评测；
- 子进程超时、无法启动或异常退出且没有交接文档：记为不通过，保留标准错误输出的路径。
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Protocol

from tightrein.config import layers
from tightrein.contracts import validate
from tightrein.domain.enums import HandoffStatus, RunnerStatus, Stage
from tightrein.evaluation.cases import ModuleCase
from tightrein.evaluation.variants import Variant
from tightrein.extensions.invoke import ProcessRequest, ProcessRunner, SubprocessRunner
from tightrein.observability import events
from tightrein.runner.result import TOOL_UNAVAILABLE, Usage
from tightrein.store.files import atomic
from tightrein.store.files.layout import ToolLayout, WorkspaceLayout

DEFAULT_ENTRY = (sys.executable, "-m", "tightrein")
DIFF_FILE = "changes.patch"
STDERR_FILE = "stderr.log"
RESULT_FILE = "result.json"
PYTHONPATH = "PYTHONPATH"
FAILING_RUNNER = frozenset({RunnerStatus.LIMIT_REACHED, RunnerStatus.SCHEMA_INVALID})
MILLISECONDS_PER_SECOND = 1000
# --output 模式下运行目录与事件日志都在输出目录中，与运行编号和日期无关；以下取值只用于调用 layout 的方法
ANY_RUN = "R-19700101-000000-loop"
ANY_DAY = date.min
ANY_NAME = "any"


@dataclass(frozen=True)
class SandboxRequest:
    case: ModuleCase
    variant: Variant
    snapshot: Path
    project: str
    output_dir: Path
    attempt: int

    @property
    def module(self) -> Stage:
        return self.case.module

    @property
    def workspace(self) -> Path:
        return ToolLayout(self.snapshot).workspace(self.project).root


@dataclass(frozen=True)
class SandboxOutcome:
    handoff: Mapping[str, Any] | None
    runner_statuses: tuple[RunnerStatus, ...] = ()
    usage: Usage = Usage()
    duration_ms: int = 0
    transcripts: tuple[str, ...] = ()
    diff_text: str | None = None
    error: str | None = None
    stderr_path: Path | None = None
    unavailable: bool = False

    @property
    def status(self) -> RunnerStatus:
        """被测模块中执行器的最终状态；模块没有调用执行器时按交接文档判断。"""
        if self.runner_statuses:
            return self.runner_statuses[-1]
        ok = self.handoff is not None and self.handoff.get("status") == HandoffStatus.OK.value
        return RunnerStatus.OK if ok else RunnerStatus.FAILED

    def failure(self) -> str | None:
        """被测版本自身失败的原因；为空表示进入评分。"""
        if self.error is not None:
            return self.error
        if self.handoff is None:
            return "没有交接文档"
        status = self.handoff.get("status")
        if status != HandoffStatus.OK.value:
            return f"交接文档为 {status}：{self.handoff.get('blockedReason') or '没有说明'}"
        failing = [item.value for item in self.runner_statuses if item in FAILING_RUNNER]
        if failing:
            return f"执行器结果为 {'、'.join(failing)}"
        return None


class ModuleRunner(Protocol):
    def run(self, request: SandboxRequest) -> SandboxOutcome: ...


def arguments(request: SandboxRequest) -> list[str]:
    case, variant = request.case, request.variant
    argv = [request.module.value, "--input", str(case.input_file), "--output", str(request.output_dir),
            "--commit", case.commit, "--ignore-state", "--runner", variant.runner]
    if variant.model is not None:
        argv += ["--model", variant.model]
    if case.gates_file is not None:
        argv += ["--gate-decisions", str(case.gates_file)]
    return [*argv, *case.args, "--workspace", str(request.workspace)]


def _handoff(output_dir: Path) -> tuple[dict[str, Any] | None, str | None]:
    directory = WorkspaceLayout(output_dir, output_dir).handoff_dir(ANY_RUN)
    found = sorted(path for path in directory.glob("*.json") if not path.stem.rsplit(".", 1)[-1].isdigit()) \
        if directory.is_dir() else []
    if len(found) != 1:
        return None, f"输出目录中的交接文档有 {len(found)} 份，应恰好一份" if found else None
    try:
        document = json.loads(found[0].read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        return None, f"交接文档不是合法的 JSON：{error}"
    errors = validate.validate_handoff(document)
    if errors:
        return None, "交接文档不合 schema：" + "；".join(str(error) for error in errors)
    return document, None


def _runner_results(output_dir: Path) -> list[dict[str, Any]]:
    layout = WorkspaceLayout(output_dir, output_dir)
    root = layout.runner_raw_dir(ANY_RUN, ANY_NAME, ANY_NAME).parent
    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(root.glob(f"*/{RESULT_FILE}"))]


def _usage(output_dir: Path) -> Usage:
    log = WorkspaceLayout(output_dir, output_dir).events_log(ANY_DAY)
    if not log.is_file():
        return Usage()
    return Usage.total(Usage(event.input_tokens, event.output_tokens, None, event.cost_usd)
                       for event in events.read(log) if event.operation == "invoke_agent")


def collect(output_dir: Path, duration_ms: int = 0, stderr_path: Path | None = None,
            process_error: str | None = None) -> SandboxOutcome:
    """从输出目录收集一次运行的结果；有交接文档时以交接文档为准，子进程的退出码不再作为运行错误。"""
    handoff, problem = _handoff(output_dir)
    results = _runner_results(output_dir)
    transcripts = WorkspaceLayout(output_dir, output_dir).transcripts_dir(ANY_RUN)
    diff = output_dir / DIFF_FILE
    return SandboxOutcome(
        handoff=handoff,
        runner_statuses=tuple(RunnerStatus(result["status"]) for result in results),
        usage=_usage(output_dir), duration_ms=duration_ms,
        transcripts=tuple(path.relative_to(output_dir).as_posix() for path in sorted(transcripts.glob("*.jsonl"))),
        diff_text=diff.read_text(encoding="utf-8") if diff.is_file() else None,
        error=None if handoff is not None else (process_error or problem),
        stderr_path=stderr_path,
        unavailable=any(result.get("errorType") == TOOL_UNAVAILABLE for result in results),
    )


class SubprocessModuleRunner:
    def __init__(self, environ: Mapping[str, str], *, entry: Sequence[str] = DEFAULT_ENTRY,
                 timeout_seconds: float | None = None, process: ProcessRunner | None = None,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        """timeout_seconds 缺省取 runtime.evaluation.sandboxTimeoutSeconds 的核心缺省值。"""
        self.environ = dict(environ)
        self.entry = tuple(entry)
        self.timeout_seconds = float(layers.core_value("runtime.evaluation.sandboxTimeoutSeconds")) \
            if timeout_seconds is None else timeout_seconds
        self.process = process or SubprocessRunner()
        self.monotonic = monotonic

    def run(self, request: SandboxRequest) -> SandboxOutcome:
        request.output_dir.mkdir(parents=True, exist_ok=True)
        core = request.snapshot / "core"
        started = self.monotonic()
        outcome = self.process(ProcessRequest(
            (*self.entry, *arguments(request)), request.snapshot, {**self.environ, PYTHONPATH: str(core)}, b"",
            self.timeout_seconds,
        ))
        duration = round((self.monotonic() - started) * MILLISECONDS_PER_SECOND)
        stderr_path = None
        if outcome.stderr:
            stderr_path = request.output_dir / STDERR_FILE
            atomic.write_text(stderr_path, outcome.stderr.decode("utf-8", errors="replace"))
        error = None
        if outcome.start_error is not None:
            error = f"子进程无法启动：{outcome.start_error}"
        elif outcome.timed_out:
            error = f"子进程超过 {self.timeout_seconds:g} 秒未结束，已终止"
        elif outcome.exit_code != 0:
            error = f"子进程以 {outcome.exit_code} 退出"
        return collect(request.output_dir, duration, stderr_path, error)
