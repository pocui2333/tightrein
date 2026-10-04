"""以子进程调用扩展(architecture/10 2.1 到 2.5、2.8)。

- 以参数数组启动、不经过 shell，工作目录为扩展所在目录，在独立的进程组中运行；环境变量由
  guards.credentials.build_env 生成(不含凭证)，另加扩展点名、协议版本、长期缓存目录、stack.yaml 声明的非敏感变量
  与核心给出的工具命令(TIGHTREIN_SEMGREP，供 core/semgrep 使用)。
- 标准输入写入一个请求 JSON 后关闭；标准输出只能是一个响应 JSON，超过 64 MB 时终止进程并按 protocol-error 处理；
  标准错误只保留最后 1 MB，经脱敏后写入 raw/extensions/<扩展点>[-<序号>].stderr.log。
- 超时或标准输出超限时终止整个进程组：先 SIGTERM，等待片刻仍未退出再 SIGKILL；主进程退出后仍占着输出管道的
  子进程同样终止。等待期间本进程被中断时同样终止进程组，再把中断抛出。
- 结果归类：超时为 timeout；退出码非 0 且标准输出没有合法的响应为 crashed，附标准错误的最后 50 行；标准输出不是
  单个 JSON 对象或 protocol 不一致为 protocol-error；响应或 output 不符合 schema 为 schema-invalid，附每条错误的
  JSON 路径与原因；扩展以 status: error 返回时取响应中的错误码，not-applicable 按核心默认处理。
  退出码非 0 而标准输出含合法的响应时以响应为准。
- extend 模式先调用下一层；下一层没有得到 output 时直接返回下一层的结果，否则把 output 作为 base 调用项目扩展，
  两层的 notes 依次合并。
- 每次启动进程写一个 run_script span；`--output` 模式下请求与响应的副本写入输出目录的 extensions/。
进程的启动可以注入，测试以小的 Python 脚本作为假扩展。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from tightrein.config import layers
from tightrein.config.project import ProjectConfig
from tightrein.contracts import validate
from tightrein.domain.enums import ExtensionErrorCode, ExtensionLayer, ExtensionPoint
from tightrein.extensions import defaults, points
from tightrein.extensions.resolve import Implementation
from tightrein.extensions.result import ExtensionFailure, PointResult
from tightrein.guards.credentials import build_env
from tightrein.observability.redact import Redactor
from tightrein.observability.tracing import STATUS_ERROR, STATUS_OK, Tracer
from tightrein.store.files.layout import UserLayout, WorkspaceLayout

ENV_POINT = "TIGHTREIN_EXTENSION_POINT"
ENV_PROTOCOL = "TIGHTREIN_EXTENSION_PROTOCOL"
ENV_CACHE_DIR = "TIGHTREIN_CACHE_DIR"
READ_SIZE = 65536
MILLISECONDS_PER_SECOND = 1000
SCRATCH_PREFIX = "tightrein-ext-"
STATUS_ERROR_RESPONSE = "error"
OVERFLOW_MESSAGE = "标准输出超过上限，进程已终止；大体积结果应写入 scratchDir 并以路径引用"


@dataclass(frozen=True)
class ProcessRequest:
    argv: tuple[str, ...]
    cwd: Path
    env: Mapping[str, str]
    stdin: bytes
    timeout_seconds: float


@dataclass(frozen=True)
class ProcessOutcome:
    """exit_code 为空表示进程没有启动；stderr 只有最后一段。"""

    exit_code: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    overflow: bool = False
    start_error: str | None = None


ProcessRunner = Callable[[ProcessRequest], ProcessOutcome]


class _Collector:
    """读取一个管道：标准输出保留全部并在超限时报告，标准错误只保留最后 limit 个字节。"""

    def __init__(self, limit: int, keep_tail: bool) -> None:
        self.limit = limit
        self.keep_tail = keep_tail
        self.data = bytearray()
        self.exceeded = threading.Event()

    def pump(self, stream: IO[bytes]) -> None:
        for chunk in iter(lambda: stream.read1(READ_SIZE), b""):
            self.data.extend(chunk)
            if len(self.data) <= self.limit:
                continue
            if self.keep_tail:
                del self.data[:len(self.data) - self.limit]
            else:
                self.exceeded.set()
                return


def _runtime(name: str, given: float | None) -> float:
    return float(layers.core_value(f"runtime.extensions.{name}") if given is None else given)


class SubprocessRunner:
    """输出上限、终止宽限与轮询间隔取 runtime.extensions.*；没有给出的取核心缺省值。"""

    def __init__(
        self, *, max_stdout_bytes: int | None = None, max_stderr_bytes: int | None = None,
        kill_grace_seconds: float | None = None, poll_seconds: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_stdout_bytes = int(_runtime("maxStdoutBytes", max_stdout_bytes))
        self.max_stderr_bytes = int(_runtime("maxStderrBytes", max_stderr_bytes))
        self.kill_grace_seconds = _runtime("killGraceSeconds", kill_grace_seconds)
        self.poll_seconds = _runtime("pollSeconds", poll_seconds)
        self.monotonic = monotonic

    @classmethod
    def from_config(cls, config: ProjectConfig) -> SubprocessRunner:
        return cls(max_stdout_bytes=int(config.get("runtime.extensions.maxStdoutBytes")),
                   max_stderr_bytes=int(config.get("runtime.extensions.maxStderrBytes")),
                   kill_grace_seconds=float(config.get("runtime.extensions.killGraceSeconds")),
                   poll_seconds=float(config.get("runtime.extensions.pollSeconds")))

    def __call__(self, request: ProcessRequest) -> ProcessOutcome:
        try:
            process = subprocess.Popen(
                list(request.argv), cwd=request.cwd, env=dict(request.env), stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
            )
        except OSError as error:
            return ProcessOutcome(None, b"", b"", start_error=f"{type(error).__name__}: {error}")
        stdout = _Collector(self.max_stdout_bytes, keep_tail=False)
        stderr = _Collector(self.max_stderr_bytes, keep_tail=True)
        threads = [
            threading.Thread(target=self._feed, args=(process.stdin, request.stdin), daemon=True),
            threading.Thread(target=stdout.pump, args=(process.stdout,), daemon=True),
            threading.Thread(target=stderr.pump, args=(process.stderr,), daemon=True),
        ]
        for thread in threads:
            thread.start()
        deadline = self.monotonic() + request.timeout_seconds
        timed_out = False
        try:
            while process.poll() is None:
                if stdout.exceeded.is_set():
                    break
                if self.monotonic() >= deadline:
                    timed_out = True
                    break
                time.sleep(self.poll_seconds)
        except BaseException:
            # 本进程被中断(Ctrl+C、入口转换的 SIGTERM、SIGHUP)：终止扩展的进程组后把中断抛出
            try:
                self._terminate(process)
            finally:
                self._kill_group(process.pid)
            raise
        if process.poll() is None:
            self._terminate(process)
        for thread in threads:
            thread.join(timeout=self.kill_grace_seconds)
        if any(thread.is_alive() for thread in threads):
            self._kill_group(process.pid)
            for thread in threads:
                thread.join()
        return ProcessOutcome(process.returncode, bytes(stdout.data), bytes(stderr.data), timed_out,
                              stdout.exceeded.is_set())

    @staticmethod
    def _feed(stream: IO[bytes] | None, data: bytes) -> None:
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

    def _terminate(self, process: subprocess.Popen[bytes]) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=self.kill_grace_seconds)
        except subprocess.TimeoutExpired:
            pass
        self._kill_group(process.pid)
        process.wait()

    @staticmethod
    def _kill_group(group: int) -> None:
        try:
            os.killpg(group, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def extension_environment(
    implementation: Implementation, environ: Mapping[str, str], user: UserLayout,
    tools: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """扩展进程的环境变量；长期缓存目录不存在时创建。tools 为核心解析好的工具命令，不经 build_env 过滤。"""
    if implementation.cache_name is None:
        raise ValueError(f"{implementation.point.value} 是核心默认实现，没有扩展进程")
    agent = build_env({**environ, **implementation.env}, extra_names=implementation.env.keys())
    cache_dir = user.extension_cache(implementation.cache_name)
    cache_dir.mkdir(parents=True, exist_ok=True)
    return {
        **agent.env,
        **(tools or {}),
        ENV_POINT: implementation.point.value,
        ENV_PROTOCOL: str(points.PROTOCOL),
        ENV_CACHE_DIR: str(cache_dir),
    }


def parse_response(stdout: bytes) -> dict[str, Any] | None:
    """标准输出中的单个 JSON 对象；不是 UTF-8、不是 JSON 或不是对象时为空。"""
    try:
        value = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _tail(text: str, lines: int) -> tuple[str, ...]:
    return tuple(text.strip().splitlines()[-lines:])


@dataclass(frozen=True)
class Interpretation:
    result: PointResult
    response: dict[str, Any] | None
    response_status: str


class Invoker:
    """一次运行内调用扩展的入口；run_id 决定标准错误与原始输出的保存位置。"""

    def __init__(
        self, layout: WorkspaceLayout, user: UserLayout, *, run_id: str, runner: ProcessRunner | None = None,
        environ: Mapping[str, str] | None = None, redactor: Redactor | None = None, tracer: Tracer | None = None,
        scratch_root: Path | None = None, monotonic: Callable[[], float] = time.monotonic,
        stderr_tail_lines: int | None = None, tools: Mapping[str, str] | None = None,
    ) -> None:
        self.layout = layout
        self.tools = dict(tools or {})
        self.stderr_tail_lines = int(_runtime("stderrTailLines", stderr_tail_lines))
        self.user = user
        self.run_id = run_id
        self.runner = runner or SubprocessRunner()
        self.environ = dict(os.environ if environ is None else environ)
        self.redactor = redactor or Redactor()
        self.tracer = tracer
        self.scratch_root = scratch_root
        self.monotonic = monotonic
        self._numbers: dict[ExtensionPoint, int] = {}

    def call(
        self, implementation: Implementation, *, repo: Path | None, commit: str | None, input: Mapping[str, Any],
    ) -> PointResult:
        if implementation.layer is ExtensionLayer.DEFAULT:
            return defaults.result(implementation.point)
        if implementation.base is None:
            return self._run(implementation, repo, commit, input, None)
        lower = self.call(implementation.base, repo=repo, commit=commit, input=input)
        if lower.output is None:
            return lower
        upper = self._run(implementation, repo, commit, input, lower.output)
        return PointResult(upper.point, upper.implementation, upper.output, upper.failure,
                           lower.notes + upper.notes, upper.cached)

    def build_request(
        self, implementation: Implementation, repo: Path | None, commit: str | None, input: Mapping[str, Any],
        base: Mapping[str, Any] | None, scratch_dir: Path,
    ) -> dict[str, Any]:
        """请求 JSON；调用方给出的输入不合格属于程序错误，抛出 SchemaValidationError。"""
        spec = points.SPECS[implementation.point]
        request = {
            "protocol": points.PROTOCOL,
            "point": implementation.point.value,
            "workspace": str(self.layout.root.absolute()),
            "repo": str(repo.absolute()) if spec.uses_repo and repo is not None else None,
            "commit": commit if spec.uses_repo else None,
            "options": dict(implementation.options),
            "scratchDir": str(scratch_dir),
            "base": None if base is None else dict(base),
            "input": dict(input),
        }
        validate.check(points.REQUEST_SCHEMA, request)
        validate.check(spec.input_schema, request["input"])
        return request

    def _run(
        self, implementation: Implementation, repo: Path | None, commit: str | None, input: Mapping[str, Any],
        base: Mapping[str, Any] | None,
    ) -> PointResult:
        number = self._next_number(implementation.point)
        span = None
        if self.tracer is not None:
            span = self.tracer.start_span("run_script", attributes={
                "point": implementation.point.value, "implementation": implementation.layer.value,
                "command": self.redactor.text(" ".join(implementation.command)), "cached": False,
            })
        started = self.monotonic()
        try:
            with tempfile.TemporaryDirectory(prefix=SCRATCH_PREFIX, dir=self.scratch_root) as scratch:
                request = self.build_request(implementation, repo, commit, input, base, Path(scratch))
                self._copy(implementation.point, number, "request", request)
                outcome = self.runner(ProcessRequest(
                    implementation.argv, implementation.cwd or self.layout.root,
                    extension_environment(implementation, self.environ, self.user, self.tools),
                    json.dumps(request, ensure_ascii=False).encode("utf-8"), implementation.timeout_seconds,
                ))
        except BaseException as error:
            if span is not None and self.tracer is not None:
                self.tracer.end_span(span, STATUS_ERROR, error_type=type(error).__name__)
            raise
        duration_ms = round((self.monotonic() - started) * MILLISECONDS_PER_SECOND)
        stderr = self.redactor.text(outcome.stderr.decode("utf-8", errors="replace"))
        self._save_stderr(implementation.point, number, stderr)
        interpretation = interpret(implementation, outcome, stderr, self.stderr_tail_lines)
        if interpretation.response is not None:
            self._copy(implementation.point, number, "response", interpretation.response)
        if span is not None and self.tracer is not None:
            failure = interpretation.result.failure
            self.tracer.end_span(span, STATUS_ERROR if failure is not None else STATUS_OK, attributes={
                "exitCode": outcome.exit_code, "status": interpretation.response_status,
                "errorCode": _error_code(interpretation), "durationMs": duration_ms,
            })
        return interpretation.result

    def _next_number(self, point: ExtensionPoint) -> int:
        number = self._numbers.get(point, 0) + 1
        while self.layout.extension_stderr(self.run_id, point, number).exists():
            number += 1
        self._numbers[point] = number
        return number

    def _save_stderr(self, point: ExtensionPoint, number: int, text: str) -> None:
        if not text:
            return
        path = self.layout.extension_stderr(self.run_id, point, number)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def _copy(self, point: ExtensionPoint, number: int, kind: str, document: Mapping[str, Any]) -> None:
        path = self.layout.extension_exchange(point, number, kind)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _error_code(interpretation: Interpretation) -> str | None:
    if interpretation.result.failure is not None:
        return interpretation.result.failure.code.value
    response = interpretation.response
    if response is not None and response.get("status") == STATUS_ERROR_RESPONSE:
        return response["error"]["code"]
    return None


def _failed(implementation: Implementation, code: ExtensionErrorCode, message: str, *, hint: str | None = None,
            details: tuple[str, ...] = (), notes: tuple[str, ...] = ()) -> PointResult:
    failure = ExtensionFailure(code, message, hint, details)
    return PointResult(implementation.point, implementation.layer, failure=failure, notes=notes)


def interpret(implementation: Implementation, outcome: ProcessOutcome, stderr: str, tail_lines: int) -> Interpretation:
    """按 architecture/10 2.4 把进程的结果归类；stderr 为已脱敏的标准错误，失败时附上最后 tail_lines 行。"""
    point = implementation.point
    if outcome.start_error is not None:
        failure = _failed(implementation, ExtensionErrorCode.CRASHED, f"无法启动扩展：{outcome.start_error}")
        return Interpretation(failure, None, STATUS_ERROR_RESPONSE)
    if outcome.timed_out:
        message = f"超过 {implementation.timeout_seconds} 秒未结束，进程组已终止"
        return Interpretation(_failed(implementation, ExtensionErrorCode.TIMEOUT, message), None, STATUS_ERROR_RESPONSE)
    if outcome.overflow:
        return Interpretation(_failed(implementation, ExtensionErrorCode.PROTOCOL_ERROR, OVERFLOW_MESSAGE), None,
                              STATUS_ERROR_RESPONSE)
    response = parse_response(outcome.stdout)
    crashed = outcome.exit_code != 0
    if response is None or response.get("protocol") != points.PROTOCOL:
        if crashed:
            message = f"退出码 {outcome.exit_code}，标准输出没有合法的响应"
            failure = _failed(implementation, ExtensionErrorCode.CRASHED, message, details=_tail(stderr, tail_lines))
            return Interpretation(failure, response, STATUS_ERROR_RESPONSE)
        message = ("标准输出不是单个 JSON 对象" if response is None
                   else f"响应的 protocol 为 {response.get('protocol')}，请求为 {points.PROTOCOL}")
        return Interpretation(_failed(implementation, ExtensionErrorCode.PROTOCOL_ERROR, message), response,
                              STATUS_ERROR_RESPONSE)
    errors = validate.validate(points.RESPONSE_SCHEMA, response)
    if errors:
        details = tuple(str(error) for error in errors)
        if crashed:
            message = f"退出码 {outcome.exit_code}，标准输出没有合法的响应"
            return Interpretation(_failed(implementation, ExtensionErrorCode.CRASHED, message,
                                          details=details + _tail(stderr, tail_lines)), response, STATUS_ERROR_RESPONSE)
        return Interpretation(_failed(implementation, ExtensionErrorCode.SCHEMA_INVALID, "响应不符合外层 schema",
                                      details=details), response, STATUS_ERROR_RESPONSE)
    notes = tuple(response.get("notes", ()))
    status = response["status"]
    if status == STATUS_ERROR_RESPONSE:
        error = response["error"]
        code = ExtensionErrorCode(error["code"])
        if code is ExtensionErrorCode.NOT_APPLICABLE:
            reason = f"{implementation.layer.label}报告不适用：{error['message']}"
            return Interpretation(defaults.result(point, (*notes, reason)), response, status)
        return Interpretation(_failed(implementation, code, error["message"], hint=error.get("hint"), notes=notes),
                              response, status)
    output_errors = validate.validate(points.SPECS[point].output_schema, response["output"])
    if output_errors:
        details = tuple(f"$.output{error.path[1:]}: {error.reason}" for error in output_errors)
        return Interpretation(_failed(implementation, ExtensionErrorCode.SCHEMA_INVALID, "output 不符合该扩展点的 schema",
                                      details=details, notes=notes), response, status)
    return Interpretation(PointResult(point, implementation.layer, output=response["output"], notes=notes),
                          response, status)
