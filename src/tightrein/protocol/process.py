"""子进程调用的唯一一处(protocol/limits.md「超时」、protocol/security.md)：AI 工具、项目脚本、git、gh 都经这里启动。

- 参数数组启动、不经 shell，`start_new_session=True` 自成进程组；不给 stdin 时用 DEVNULL，不继承终端，
  ssh、凭证助手、agent 工具不会卡在等终端输入；
- 后台线程逐行读 stdout 交给主线程：主线程逐行回调 on_line(返回原因即终止)、按 idle 判断没有动静、按 timeout
  判断超时；stdout 超过上限即终止并归为 overflow，stderr 末尾补一行 OVERFLOW_HINT(大结果写文件、以路径引用)，
  调用方据此报告；stderr 只留最后几行；
- 终止时对整个进程组依次发 SIGINT(等 10s)→SIGTERM(等 5s)→SIGKILL；主进程退出后再对进程组 SIGKILL 一次，
  清掉仍占着输出管道的孙进程(git 起的 ssh、agent 起的子进程)；
- 等待期间本进程被中断(Ctrl-C，或入口把 SIGTERM、SIGHUP 转成的 Interrupted)：先终止子进程组再把中断抛出；
  宽限等待中再被打断直接 SIGKILL；
- 可执行文件不存在或无法启动不抛出，结果的 start_error 写明原因(调用方归为「工具不可用」)。
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
from contextlib import nullcontext
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import IO, Protocol, TextIO

TIMEOUT = "timeout"
IDLE = "idle"
OVERFLOW = "overflow"
INTERRUPT_GRACE_S = 10.0  # 先 SIGINT：agent 工具收到后自己收尾(保存会话、删临时文件)
TERMINATE_GRACE_S = 5.0
POLL_S = 0.05
STDERR_LINE_BYTES = 65536  # stderr 按段读：一行很长也不会整行读进内存
MILLISECONDS_PER_SECOND = 1000
OVERFLOW_HINT = "标准输出超过上限 {limit} 字节，已终止：大结果请写到文件，在输出中以路径引用"


class _Marker(Enum):
    EOF = "eof"
    OVERFLOW = "overflow"


@dataclass(frozen=True)
class Command:
    argv: tuple[str, ...]
    cwd: Path
    env: Mapping[str, str]  # 已经过 security.child_env 过滤
    stdin: str | bytes | None = None  # None 时用 DEVNULL
    timeout_s: float | None = None
    idle_s: float | None = None  # 多久没有输出即超时(流式)
    max_stdout: int = 64 * 1024 * 1024  # 超过即终止，归为 overflow
    stderr_tail_lines: int = 50
    on_line: Callable[[str], str | None] | None = None  # 逐行回调；返回非 None 的字符串即以该原因终止
    stdout_path: Path | None = None  # 给出时逐行写盘并 flush，result.stdout 为空


@dataclass(frozen=True)
class Outcome:
    exit_code: int | None  # 被终止时为 None
    stdout: str
    stderr_tail: str
    duration_ms: int
    stopped_by: str | None  # timeout、idle、overflow、<on_line 返回的原因>
    start_error: str | None  # 可执行文件不存在或无法启动


class ProcessRunner(Protocol):
    def run(self, command: Command) -> Outcome: ...


class Interrupted(BaseException):
    """入口把 SIGTERM、SIGHUP 转成它，与 KeyboardInterrupt 同级：不被 `except Exception` 吞掉。"""


class _Activity:
    """最近一次有输出(stdout 或 stderr)的时间；读线程写、主线程读，单个浮点数的赋值在 GIL 下是原子的。"""

    def __init__(self, at: float) -> None:
        self.at = at


class SubprocessRunner:
    """真实实现。宽限时间与时钟可注入，测试用更短的值。"""

    def __init__(self, *, interrupt_grace_s: float = INTERRUPT_GRACE_S, terminate_grace_s: float = TERMINATE_GRACE_S,
                 poll_s: float = POLL_S, monotonic: Callable[[], float] = time.monotonic) -> None:
        self.interrupt_grace_s = interrupt_grace_s
        self.terminate_grace_s = terminate_grace_s
        self.poll_s = poll_s
        self.monotonic = monotonic

    def run(self, command: Command) -> Outcome:
        started = self.monotonic()
        try:
            process = subprocess.Popen(
                list(command.argv), cwd=command.cwd, env=dict(command.env),
                stdin=subprocess.DEVNULL if command.stdin is None else subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
            )
        except OSError as error:
            return Outcome(None, "", "", self._elapsed_ms(started), None, f"{type(error).__name__}: {error}")
        assert process.stdout is not None and process.stderr is not None
        lines: queue.Queue[str | _Marker] = queue.Queue()
        errors: deque[str] = deque(maxlen=command.stderr_tail_lines)
        activity = _Activity(started)
        threads = [
            threading.Thread(target=_read_stdout, args=(process.stdout, lines, command.max_stdout), daemon=True),
            threading.Thread(target=self._read_stderr, args=(process.stderr, errors, activity), daemon=True),
        ]
        if command.stdin is not None:
            data = command.stdin.encode("utf-8") if isinstance(command.stdin, str) else command.stdin
            threads.append(threading.Thread(target=_feed, args=(process.stdin, data), daemon=True))
        for thread in threads:
            thread.start()
        if command.stdout_path is not None:
            command.stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stdout: list[str] = []
        try:
            with (open(command.stdout_path, "w", encoding="utf-8", newline="")
                  if command.stdout_path is not None else nullcontext()) as sink:
                stopped_by = self._follow(process, command, lines, activity, started, stdout, sink)
        except BaseException:
            self._abandon(process)
            raise
        if stopped_by is not None:
            self._terminate(process)
        self._finish(process, threads)
        exit_code = None if stopped_by is not None else process.returncode
        if stopped_by == OVERFLOW:
            errors.append(OVERFLOW_HINT.format(limit=command.max_stdout))
        return Outcome(exit_code, "".join(stdout), "\n".join(errors), self._elapsed_ms(started), stopped_by, None)

    def _follow(self, process: subprocess.Popen[bytes], command: Command, lines: queue.Queue[str | _Marker],
                activity: _Activity, started: float, stdout: list[str], sink: TextIO | None) -> str | None:
        """读到 stdout 结束并等进程退出；返回终止原因，正常结束为 None。"""
        deadline = None if command.timeout_s is None else started + command.timeout_s
        exited_at: float | None = None
        while True:
            now = self.monotonic()
            if deadline is not None and now >= deadline:
                return TIMEOUT
            if command.idle_s is not None and now - activity.at >= command.idle_s:
                return IDLE
            wait = self.poll_s if deadline is None else min(self.poll_s, deadline - now)
            try:
                item = lines.get(timeout=wait)
            except queue.Empty:
                if process.poll() is not None:
                    exited_at = now if exited_at is None else exited_at
                    # 主进程已退出而管道迟迟不关：孙进程占着它，清掉整个进程组
                    if now - exited_at >= self.terminate_grace_s:
                        _kill_group(process.pid)
                continue
            if item is _Marker.EOF:
                return self._wait(process, deadline)
            if item is _Marker.OVERFLOW:
                return OVERFLOW
            activity.at = self.monotonic()
            if sink is not None:
                sink.write(item)
                sink.flush()
            else:
                stdout.append(item)
            if command.on_line is not None:
                reason = command.on_line(item.rstrip("\r\n"))
                if reason is not None:
                    return reason

    def _wait(self, process: subprocess.Popen[bytes], deadline: float | None) -> str | None:
        while process.poll() is None:
            if deadline is not None and self.monotonic() >= deadline:
                return TIMEOUT
            time.sleep(self.poll_s)
        return None

    def _read_stderr(self, stream: IO[bytes], errors: deque[str], activity: _Activity) -> None:
        for raw in iter(lambda: stream.readline(STDERR_LINE_BYTES), b""):
            errors.append(raw.decode("utf-8", errors="replace").rstrip("\r\n"))
            activity.at = self.monotonic()

    def _terminate(self, process: subprocess.Popen[bytes]) -> None:
        """SIGINT、SIGTERM、SIGKILL 依次发给整个进程组；主进程退出后再清一次进程组。"""
        for number, grace in ((signal.SIGINT, self.interrupt_grace_s), (signal.SIGTERM, self.terminate_grace_s),
                              (signal.SIGKILL, None)):
            try:
                os.killpg(process.pid, number)
            except (ProcessLookupError, PermissionError):
                break
            try:
                process.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                continue
            break
        _kill_group(process.pid)

    def _abandon(self, process: subprocess.Popen[bytes]) -> None:
        """本进程被中断时终止子进程组；宽限等待中再被打断直接 SIGKILL(3879733)。"""
        try:
            self._terminate(process)
        except BaseException:
            _kill_group(process.pid)
            raise
        finally:
            process.wait()

    def _finish(self, process: subprocess.Popen[bytes], threads: list[threading.Thread]) -> None:
        process.wait()
        for thread in threads:
            thread.join(timeout=self.terminate_grace_s)
        if any(thread.is_alive() for thread in threads):
            _kill_group(process.pid)
            for thread in threads:
                thread.join()
        _close(process)

    def _elapsed_ms(self, started: float) -> int:
        return round((self.monotonic() - started) * MILLISECONDS_PER_SECOND)


def _read_stdout(stream: IO[bytes], lines: queue.Queue[str | _Marker], limit: int) -> None:
    """逐行读；按剩余额度限制 readline 的长度，一行没有换行的巨大输出也不会整个读进内存。"""
    total = 0
    while raw := stream.readline(limit - total + 1):
        total += len(raw)
        if total > limit:
            lines.put(_Marker.OVERFLOW)
            return
        lines.put(raw.decode("utf-8", errors="replace"))
    lines.put(_Marker.EOF)


def _feed(stream: IO[bytes] | None, data: bytes) -> None:
    """写完即关闭；子进程不读 stdin 就退出时容忍 BrokenPipe。"""
    if stream is None:
        return
    try:
        stream.write(data)
    except BrokenPipeError:
        pass
    finally:
        try:
            stream.close()
        except BrokenPipeError:
            pass


def _kill_group(group: int) -> None:
    try:
        os.killpg(group, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _close(process: subprocess.Popen[bytes]) -> None:
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            stream.close()
