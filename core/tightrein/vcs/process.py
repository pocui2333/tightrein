"""执行 git、gh 与待确认操作中的其他命令(architecture/02 1.2、4.2、4.8)。

- 一律以参数列表启动，不经过 shell；工作目录显式传入；标准输出与错误输出分别捕获。
- 环境为调用方给出的基础环境加上固定的 `GIT_TERMINAL_PROMPT=0`、`LC_ALL=C`，避免交互提示与本地化输出影响解析；
  标准输入不继承终端(没有输入时为 /dev/null)，子进程在新的会话中运行、没有控制终端，ssh 与凭证助手无法等待输入。
- 每条命令都有超时：fetch 取 runtime.vcs.fetchTimeoutSeconds，其余取 runtime.vcs.timeoutSeconds；超时时终止整个
  进程组(含 git 启动的 ssh、git-remote-https)，不等待仍占着输出管道的孙进程。
- 错误只按退出码分类：gh 退出码 4 为未登录；fetch、push 等访问远程的 git 命令退出码 128 或访问远程的命令超时为网络错误；
  其余非零退出为 GitCommandError 或 GhCommandError。只读查询遇到网络错误时间隔 5 秒、20 秒各重试一次。
- 换路：访问远程的 git 命令与 gh 超时，或非零退出且错误输出匹配 runtime.network.errorPatterns 时，立即换另一条路
  (直连与经代理互换，config.network.rerouted)重试一次；没有另一条路时不换。每次换路记入 reroutes 并调用 on_reroute
  (组装根写 gate 事件)；之后仍失败按上面的规则分类，只读查询的间隔重试照旧。
- 错误中的命令与错误输出经 Redactor 脱敏；每条命令写一个 run_script span(传入 tracer 时)。
执行函数可以注入，gh 的测试用返回录制 JSON 的假执行函数，不访问 GitHub。
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tightrein.config import layers, network
from tightrein.config.network import Reroute
from tightrein.observability.redact import Redactor
from tightrein.observability.tracing import Tracer
from tightrein.vcs.errors import (
    CommandFailed,
    GhAuthError,
    GhCommandError,
    GhNotFound,
    GitCommandError,
    GitNotFound,
    NetworkError,
    ProgramNotFound,
    VcsError,
)

GIT = "git"
GH = "gh"
FIXED_ENV: Mapping[str, str] = {"GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}
GH_AUTH_EXIT_CODE = 4
GIT_FATAL_EXIT_CODE = 128
REMOTE_GIT_COMMANDS = frozenset({"fetch", "push", "pull", "ls-remote"})


@dataclass(frozen=True)
class Command:
    argv: tuple[str, ...]
    cwd: Path
    env: Mapping[str, str]
    timeout: float | None
    stdin: str | None = None


@dataclass(frozen=True)
class Completed:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str = ""


Executor = Callable[[Command], Completed]
Sleep = Callable[[float], None]


def subprocess_executor(command: Command) -> Completed:
    process = subprocess.Popen(
        list(command.argv), cwd=command.cwd, env=dict(command.env),
        stdin=subprocess.DEVNULL if command.stdin is None else subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(command.stdin, timeout=command.timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            process.kill()
        process.communicate()
        raise
    return Completed(command.argv, process.returncode, stdout, stderr)


def _runtime(name: str) -> Any:
    return layers.core_value(f"runtime.vcs.{name}")


def tail(text: str, lines: int) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


@dataclass
class VcsProcess:
    """git、gh 与其他命令的执行入口；git 与 gh 的可执行文件路径可以替换。超时、只读远程命令的重试间隔与错误输出
    尾行数取 runtime.vcs.*，没有给出的取核心缺省值(组装根按工作区配置给出)。"""

    git_path: str = GIT
    gh_path: str = GH
    execute: Executor = subprocess_executor
    environ: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    sleep: Sleep = time.sleep
    redactor: Redactor = field(default_factory=Redactor)
    tracer: Tracer | None = None
    timeout: float = field(default_factory=lambda: float(_runtime("timeoutSeconds")))
    fetch_timeout: float = field(default_factory=lambda: float(_runtime("fetchTimeoutSeconds")))
    retry_delays: tuple[float, ...] = field(default_factory=lambda: tuple(_runtime("readRetryDelaysSeconds")))
    stderr_tail_lines: int = field(default_factory=lambda: int(_runtime("stderrTailLines")))
    reroute_host: str = field(default_factory=lambda: str(layers.core_value("runtime.network.rerouteHost")))
    network_patterns: tuple[str, ...] = field(
        default_factory=lambda: tuple(layers.core_value("runtime.network.errorPatterns")))
    on_reroute: Callable[[Reroute], None] | None = None
    reroutes: list[Reroute] = field(default_factory=list)

    def environment(self) -> dict[str, str]:
        return {**self.environ, **FIXED_ENV}

    def _describe(self, argv: Sequence[str]) -> str:
        return self.redactor.text(" ".join(argv))

    def _execute(self, argv: Sequence[str], cwd: Path, stdin: str | None, remote: bool,
                 not_found: type[ProgramNotFound], timeout: float | None = None,
                 env: Mapping[str, str] | None = None) -> Completed:
        if not cwd.is_dir():
            raise VcsError(f"工作目录不存在：{cwd}", argv=argv)
        timeout = self.timeout if timeout is None else timeout
        command = Command(tuple(argv), cwd, self.environment() if env is None else env, timeout, stdin)
        try:
            return self.execute(command)
        except FileNotFoundError as error:
            raise not_found(f"找不到可执行文件 {argv[0]}", argv=argv) from error
        except subprocess.TimeoutExpired as error:
            kind = NetworkError if remote else CommandFailed
            raise kind(f"{self._describe(argv)} 超过 {timeout} 秒未结束", argv=argv) from error

    def _traced(self, argv: Sequence[str], run: Callable[[], Completed]) -> Completed:
        if self.tracer is None:
            return run()
        with self.tracer.span("run_script", attributes={"argv": self._describe(argv)}) as span:
            completed = run()
            span.set(attributes={"exitCode": completed.returncode})
            return completed

    def run(self, argv: Sequence[str], cwd: Path, *, stdin: str | None = None) -> Completed:
        """执行任意命令(待确认操作中的 ln 等)，非零退出不抛出，由调用方判断。"""
        return self._traced(argv, lambda: self._execute(argv, cwd, stdin, False, ProgramNotFound))

    def _failure(self, kind: type[VcsError], argv: Sequence[str], completed: Completed) -> VcsError:
        stderr = self.redactor.text(tail(completed.stderr, self.stderr_tail_lines))
        return kind(
            f"{self._describe(argv)} 退出码 {completed.returncode}：{stderr or '没有错误输出'}",
            argv=argv, returncode=completed.returncode, stderr=stderr,
        )

    def _rerouted(self, argv: Sequence[str], run: Callable[[Mapping[str, str] | None], Completed]) -> Completed:
        """按当前路线执行一次；网络类错误时换另一条路再执行一次并记下。"""
        try:
            completed = run(None)
        except NetworkError as error:
            first: Completed | NetworkError = error
            reason = str(error)
        else:
            if completed.returncode == 0 or not network.is_network_failure(completed.stderr, self.network_patterns):
                return completed
            first = completed
            reason = self.redactor.text(tail(completed.stderr, 1)) or f"退出码 {completed.returncode}"
        environment = self.environment()
        alternate = network.rerouted(environment, self.reroute_host)
        if alternate is None:
            if isinstance(first, NetworkError):
                raise first
            return first
        before, after = network.route(environment, self.reroute_host), network.route(alternate, self.reroute_host)
        try:
            second = run(alternate)
        except NetworkError:
            self._note(Reroute(self._describe(argv), before, after, reason, False))
            raise
        self._note(Reroute(self._describe(argv), before, after, reason, second.returncode == 0))
        return second

    def _note(self, reroute: Reroute) -> None:
        self.reroutes.append(reroute)
        if self.on_reroute is not None:
            self.on_reroute(reroute)

    def _with_retry(self, retry: bool, attempt: Callable[[], Completed]) -> Completed:
        delays = self.retry_delays if retry else ()
        for delay in delays:
            try:
                return attempt()
            except NetworkError:
                self.sleep(delay)
        return attempt()

    def git(self, repo: Path, *args: str, stdin: str | None = None, ok_codes: Sequence[int] = (0,),
            retry: bool = False, timeout: float | None = None) -> Completed:
        """在 repo 中执行 git；退出码不在 ok_codes 中时按退出码分类抛出。retry 只用于只读查询；timeout 为空时取
        runtime.vcs.timeoutSeconds。"""
        argv = (self.git_path, *args)
        remote = bool(args) and args[0] in REMOTE_GIT_COMMANDS

        def once(env: Mapping[str, str] | None) -> Completed:
            return self._traced(argv, lambda: self._execute(argv, repo, stdin, remote, GitNotFound, timeout, env))

        def attempt() -> Completed:
            completed = self._rerouted(argv, once) if remote else once(None)
            if completed.returncode in ok_codes:
                return completed
            if remote and completed.returncode == GIT_FATAL_EXIT_CODE:
                raise self._failure(NetworkError, argv, completed)
            raise self._failure(GitCommandError, argv, completed)

        return self._with_retry(retry, attempt)

    def gh(self, repo: Path, *args: str, retry: bool = False) -> Completed:
        argv = (self.gh_path, *args)

        def once(env: Mapping[str, str] | None) -> Completed:
            return self._traced(argv, lambda: self._execute(argv, repo, None, True, GhNotFound, env=env))

        def attempt() -> Completed:
            completed = self._rerouted(argv, once)
            if completed.returncode == 0:
                return completed
            if completed.returncode == GH_AUTH_EXIT_CODE:
                raise self._failure(GhAuthError, argv, completed)
            raise self._failure(GhCommandError, argv, completed)

        return self._with_retry(retry, attempt)
