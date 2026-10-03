"""外部工具进程的启动(architecture/04 1.2)：Schemathesis、Playwright、Semgrep 等一律经 Launcher 启动，测试注入
返回录制输出的替身，不调用真实工具。

- SubprocessLauncher 复用 extensions.invoke.SubprocessRunner：参数数组、不经 shell、独立进程组、超时终止整个进程组；
  标准输入为空。标准输出超过上限同样终止进程，结果记为 overflow。
- log_file 给出时把命令行、退出码、标准输出与标准错误写入该文件(经脱敏)，供人查看。
- 工具进程的环境由 tool_env 生成：guards.credentials.build_env 的白名单(不含凭证)加代理设置，再加调用方显式给出的
  变量(例如 token 与密码只经这里传给 Schemathesis 与 Playwright)。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from tightrein.extensions.invoke import ProcessRequest, ProcessRunner, SubprocessRunner
from tightrein.guards.credentials import build_env
from tightrein.observability.redact import Redactor

PROXY_NAMES = ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy")


@dataclass(frozen=True)
class ToolCommand:
    argv: tuple[str, ...]
    cwd: Path
    timeout_seconds: float
    env: Mapping[str, str] = field(default_factory=dict)
    log_file: Path | None = None


@dataclass(frozen=True)
class ToolRun:
    """exit_code 为空表示进程没有启动(start_error 说明原因)或被终止。"""

    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    overflow: bool = False
    start_error: str | None = None

    @property
    def started(self) -> bool:
        return self.start_error is None

    def describe(self) -> str:
        if self.start_error is not None:
            return f"无法启动：{self.start_error}"
        if self.timed_out:
            return "超时，进程组已终止"
        if self.overflow:
            return "标准输出超过上限，进程已终止"
        return f"退出码 {self.exit_code}"


Launcher = Callable[[ToolCommand], ToolRun]


def tool_env(environ: Mapping[str, str], extra: Mapping[str, str] | None = None,
             keep: Iterable[str] = ()) -> dict[str, str]:
    env = build_env(environ, extra_names=(*PROXY_NAMES, *keep)).env
    env.update(extra or {})
    return env


class SubprocessLauncher:
    def __init__(self, runner: ProcessRunner | None = None, redactor: Redactor | None = None) -> None:
        self.runner = runner or SubprocessRunner()
        self.redactor = redactor or Redactor()

    def __call__(self, command: ToolCommand) -> ToolRun:
        outcome = self.runner(ProcessRequest(command.argv, command.cwd, dict(command.env), b"",
                                             command.timeout_seconds))
        run = ToolRun(
            outcome.exit_code, outcome.stdout.decode("utf-8", errors="replace"),
            outcome.stderr.decode("utf-8", errors="replace"), outcome.timed_out, outcome.overflow,
            outcome.start_error,
        )
        if command.log_file is not None:
            self._log(command.log_file, command.argv, run)
        return run

    def _log(self, log_file: Path, argv: tuple[str, ...], run: ToolRun) -> None:
        text = f"$ {' '.join(argv)}\n# {run.describe()}\n\n[stdout]\n{run.stdout}\n\n[stderr]\n{run.stderr}\n"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.write_text(self.redactor.text(text), encoding="utf-8")
