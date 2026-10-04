"""启动 agent 进程(architecture/02 2.7、2.8)：参数列表启动、不经过 shell，独立进程组，逐行读取标准输出。

- 每读到一行交给调用方的处理函数；处理函数返回终止原因(轮数、费用超限)时终止进程。
- 到达时间上限或需要终止时，向整个进程组发送 SIGINT，等待 runtime.runner.interruptGraceSeconds(默认 10 秒)；仍未退出
  发送 SIGTERM，再等 runtime.runner.terminateGraceSeconds(默认 5 秒)后 SIGKILL，agent 启动的子进程随进程组一并终止。
  等待时间可以注入，测试用更短的值。
- 等待期间本进程被中断(KeyboardInterrupt 或入口转换的 SIGTERM、SIGHUP)时同样按上面的顺序终止进程组，再把中断抛出。
- 交互模式把标准输入输出直接交给 agent 工具，等待进程结束。
启动器可以注入：单元测试用返回录制输出的假启动器，不启动真实的 claude、codex、agy。
"""

from __future__ import annotations

import os
import queue
import signal
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Protocol

from tightrein.config import layers
from tightrein.config.project import ProjectConfig

TIMEOUT = "timeout"
MILLISECONDS_PER_SECOND = 1000

LineHandler = Callable[[str], str | None]


@dataclass(frozen=True)
class Invocation:
    argv: tuple[str, ...]
    cwd: Path
    env: Mapping[str, str]
    stdin: bytes | None = None


@dataclass(frozen=True)
class ProcessOutcome:
    """exit_code 为进程的退出码(被信号终止时为负数)；stopped_by 为 timeout 或处理函数给出的终止原因。"""

    exit_code: int
    stderr_tail: str
    stopped_by: str | None
    duration_ms: int


class ProcessLauncher(Protocol):
    def run(self, invocation: Invocation, on_line: LineHandler, timeout_ms: int | None) -> ProcessOutcome: ...

    def run_interactive(self, invocation: Invocation) -> int: ...


def _pump(stream: IO[bytes], sink: Callable[[str | None], None]) -> None:
    for raw in iter(stream.readline, b""):
        sink(raw.decode("utf-8", errors="replace").rstrip("\r\n"))
    sink(None)


def _runtime(name: str, given: float | None) -> float:
    return float(layers.core_value(f"runtime.runner.{name}") if given is None else given)


class SubprocessLauncher:
    """两个宽限时间、错误输出尾行数与轮询间隔取 runtime.runner.*；没有给出的取核心缺省值。"""

    def __init__(self, *, interrupt_grace: float | None = None, terminate_grace: float | None = None,
                 stderr_tail_lines: int | None = None, poll_seconds: float | None = None,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self.interrupt_grace = _runtime("interruptGraceSeconds", interrupt_grace)
        self.terminate_grace = _runtime("terminateGraceSeconds", terminate_grace)
        self.stderr_tail_lines = int(_runtime("stderrTailLines", stderr_tail_lines))
        self.poll_seconds = _runtime("pollSeconds", poll_seconds)
        self.monotonic = monotonic

    @classmethod
    def from_config(cls, config: ProjectConfig) -> SubprocessLauncher:
        return cls(interrupt_grace=float(config.get("runtime.runner.interruptGraceSeconds")),
                   terminate_grace=float(config.get("runtime.runner.terminateGraceSeconds")),
                   stderr_tail_lines=int(config.get("runtime.runner.stderrTailLines")),
                   poll_seconds=float(config.get("runtime.runner.pollSeconds")))

    def run(self, invocation: Invocation, on_line: LineHandler, timeout_ms: int | None) -> ProcessOutcome:
        """可执行文件不存在时抛出 FileNotFoundError，由调用方记为 tool-unavailable。"""
        started = self.monotonic()
        process = subprocess.Popen(
            list(invocation.argv), cwd=invocation.cwd, env=dict(invocation.env),
            stdin=subprocess.PIPE if invocation.stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
        )
        if process.stdout is None or process.stderr is None:
            raise OSError("无法读取子进程的输出")
        lines: queue.Queue[str | None] = queue.Queue()
        errors: deque[str] = deque(maxlen=self.stderr_tail_lines)

        def keep_error(line: str | None) -> None:
            if line is not None:
                errors.append(line)

        readers = [
            threading.Thread(target=_pump, args=(process.stdout, lines.put), daemon=True),
            threading.Thread(target=_pump, args=(process.stderr, keep_error), daemon=True),
        ]
        if invocation.stdin is not None and process.stdin is not None:
            readers.append(threading.Thread(target=self._feed, args=(process.stdin, invocation.stdin), daemon=True))
        for reader in readers:
            reader.start()
        deadline = None if timeout_ms is None else started + timeout_ms / MILLISECONDS_PER_SECOND
        try:
            stopped_by = self._read(lines, on_line, deadline)
            if stopped_by is None:
                stopped_by = self._wait(process, deadline)
        except BaseException:
            self._abandon(process)
            raise
        if stopped_by is not None:
            self._terminate(process)
        process.wait()
        for reader in readers:
            reader.join(timeout=self.terminate_grace)
        duration_ms = round((self.monotonic() - started) * MILLISECONDS_PER_SECOND)
        return ProcessOutcome(process.returncode, "\n".join(errors), stopped_by, duration_ms)

    @staticmethod
    def _feed(stream: IO[bytes], data: bytes) -> None:
        try:
            stream.write(data)
        except BrokenPipeError:
            pass
        finally:
            stream.close()

    def _read(self, lines: queue.Queue[str | None], on_line: LineHandler, deadline: float | None) -> str | None:
        while True:
            remaining = None if deadline is None else deadline - self.monotonic()
            if remaining is not None and remaining <= 0:
                return TIMEOUT
            try:
                line = lines.get(timeout=self.poll_seconds if remaining is None else min(remaining, self.poll_seconds))
            except queue.Empty:
                continue
            if line is None:
                return None
            reason = on_line(line)
            if reason is not None:
                return reason

    def _wait(self, process: subprocess.Popen[bytes], deadline: float | None) -> str | None:
        remaining = None if deadline is None else max(deadline - self.monotonic(), 0)
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            return TIMEOUT
        return None

    def _terminate(self, process: subprocess.Popen[bytes]) -> None:
        """SIGINT、SIGTERM、SIGKILL 依次发给整个进程组。"""
        for number, grace in ((signal.SIGINT, self.interrupt_grace), (signal.SIGTERM, self.terminate_grace),
                              (signal.SIGKILL, None)):
            try:
                os.killpg(process.pid, number)
            except ProcessLookupError:
                break
            try:
                process.wait(timeout=grace)
                if grace is not None:
                    self._kill_rest(process.pid)
                    break
            except subprocess.TimeoutExpired:
                continue

    def _abandon(self, process: subprocess.Popen[bytes]) -> None:
        """本进程被中断(Ctrl+C、SIGTERM)时终止子进程组；宽限等待再被打断时直接 SIGKILL。"""
        try:
            self._terminate(process)
        except BaseException:
            self._kill_rest(process.pid)
            raise
        finally:
            process.wait()

    @staticmethod
    def _kill_rest(group: int) -> None:
        """主进程已退出，进程组中剩下的子进程直接终止。"""
        try:
            os.killpg(group, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def run_interactive(self, invocation: Invocation) -> int:
        completed = subprocess.run(list(invocation.argv), cwd=invocation.cwd, env=dict(invocation.env), check=False)
        return completed.returncode
