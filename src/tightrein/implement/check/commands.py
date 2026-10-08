"""执行一条项目检查命令并判定结果。

- 命令以 shlex 拆成参数数组，不经 shell；在 worktree 根目录执行，环境只给白名单变量；时限取 limits.timeouts.tests；
- 标准输出逐行写日志(不进内存)，结束后把错误输出的末尾追加到日志；
- 结果分四种：passed；failed(含超时，超时的退出码记为空)；not_run(无法启动，或环境问题重试后仍失败：既不算通过，
  也不交回编码，不让模型对着没跑起来的检查改代码)；invalid(测试命令的退出码不在「测试失败」集合中：收集错误、
  用法错误、没选中测试)；
- 失败输出命中环境问题(连接被拒、端口被占等，正则可配)时就地重试 environmentRetries 次(limits.md：测试环境问题
  就地重试 1 次)；真实的断言失败不重试，测试本身不稳定的也不靠重试掩盖。
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from tightrein.implement.check.affected import TEST_COMMAND, Planned, argv_text
from tightrein.implement.check.output import read_tail, trim
from tightrein.protocol.process import Command, ProcessRunner
from tightrein.protocol.security import child_env
from tightrein.settings.load import Settings

PASSED = "passed"
FAILED = "failed"
NOT_RUN = "not_run"
INVALID = "invalid"
SECTION = "implement.check"


@dataclass(frozen=True)
class CommandSettings:
    timeout_s: float
    environment: tuple[re.Pattern[str], ...]
    environment_retries: int
    test_failure_exit_codes: frozenset[int]
    output_chars: int
    log_tail_bytes: int

    @classmethod
    def from_settings(cls, settings: Settings) -> CommandSettings:
        section = settings.section(SECTION)
        return cls(
            timeout_s=settings.duration("limits.timeouts.tests"),
            environment=tuple(re.compile(pattern, re.IGNORECASE) for pattern in section["environmentPatterns"]),
            environment_retries=int(section["environmentRetries"]),
            test_failure_exit_codes=frozenset(section["testFailureExitCodes"]),
            output_chars=int(section["outputChars"]),
            log_tail_bytes=int(section["logTailBytes"]),
        )


@dataclass(frozen=True)
class CommandResult:
    name: str
    command: str
    result: str  # passed、failed、not_run、invalid
    exit_code: int | None
    log: str
    attempts: int
    output: str | None = None  # 精简后的失败输出，交给模型
    reason: str | None = None
    tests: tuple[str, ...] = ()  # 只跑了这些测试；为空时整组

    @property
    def passed(self) -> bool:
        return self.result == PASSED

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["exitCode"] = data.pop("exit_code")
        data["tests"] = list(self.tests)
        return data


def run(planned: Planned, *, worktree: Path, log: Path, runner: ProcessRunner, environ: Mapping[str, str],
        settings: CommandSettings) -> CommandResult:
    text = argv_text(planned.command, planned.tests)
    try:
        argv = tuple(shlex.split(text))
    except ValueError as error:
        return _result(planned, text, NOT_RUN, None, log, 0, reason=f"命令写法有误：{error}")
    log.parent.mkdir(parents=True, exist_ok=True)
    attempts = 0
    while True:
        attempts += 1
        outcome = runner.run(Command(argv, worktree, child_env(environ), timeout_s=settings.timeout_s,
                                     stdout_path=log))
        if outcome.stderr_tail:
            with log.open("a", encoding="utf-8") as handle:
                handle.write(outcome.stderr_tail)
        if outcome.start_error is not None:
            return _result(planned, text, NOT_RUN, None, log, attempts,
                           reason=f"无法启动：{outcome.start_error}；检查项目依赖是否已装好")
        if outcome.stopped_by is not None:
            return _result(planned, text, FAILED, None, log, attempts, output=_output(log, settings),
                           reason=f"被终止({outcome.stopped_by})：超过 {settings.timeout_s:g} 秒或输出过大")
        if outcome.exit_code == 0:
            return _result(planned, text, PASSED, 0, log, attempts)
        output = read_tail(log, settings.log_tail_bytes)
        if _environment(output, settings.environment):
            if attempts <= settings.environment_retries:
                continue
            return _result(planned, text, NOT_RUN, outcome.exit_code, log, attempts,
                           output=trim(output, max_chars=settings.output_chars),
                           reason=f"输出为测试环境问题(连接被拒、端口被占等)，重试 {attempts - 1} 次后仍失败")
        if planned.command.name == TEST_COMMAND and outcome.exit_code not in settings.test_failure_exit_codes:
            return _result(planned, text, INVALID, outcome.exit_code, log, attempts,
                           output=trim(output, max_chars=settings.output_chars),
                           reason=f"退出码 {outcome.exit_code} 不是测试失败(收集错误、用法错误或没有选中测试)")
        return _result(planned, text, FAILED, outcome.exit_code, log, attempts,
                       output=trim(output, max_chars=settings.output_chars))


def summary(results: Sequence[CommandResult]) -> dict[str, str]:
    """每条命令的结果，下一轮据此决定重跑哪些。"""
    return {result.name: result.result for result in results}


def _result(planned: Planned, text: str, result: str, exit_code: int | None, log: Path, attempts: int, *,
            output: str | None = None, reason: str | None = None) -> CommandResult:
    return CommandResult(planned.command.name, text, result, exit_code, str(log), attempts, output, reason,
                         planned.tests)


def _output(log: Path, settings: CommandSettings) -> str:
    return trim(read_tail(log, settings.log_tail_bytes), max_chars=settings.output_chars)


def _environment(output: str, patterns: Sequence[re.Pattern[str]]) -> bool:
    return any(pattern.search(output) for pattern in patterns)
