"""测试类复现检查(architecture/04 7.1)：在 worktree 中运行项目测试命令选中的一个测试。

命令白名单由注入的 resolve 判断(pipeline/project_checks.repro_test_cwd：开头须为某条 checks.commands 的允许前缀，
且选中了测试文件)，执行目录为该命令的 cwd；不经 shell，超时取 checks.timeoutSeconds。
结果：无法启动、超时为 not-run；退出码 0 为通过；在 regressions.testFailureExitCodes 中为失败(测试失败)；其他非零
(收集错误、用法错误、没有选中测试)为 invalid，不算复现。测试文件不在 worktree 中时为 not-run。

测试失败时的处理(38-external-techniques.md 第 1 项)，规则取 regressions.testFailurePatterns：
- 任何执行中，输出命中环境问题的就地重试 regressions.envRetries 次，仍失败为 not-run；
- 基准版本上的执行(base，写复现测试时)另做归因：命中测试本身有问题的为 invalid，交回重写；登记了预期异常签名
  (expectedSignature)时，输出包含该签名才算有效复现，签名优先于其余规则。修复后的执行不做这两项，失败即为失败。
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from tightrein.config.project import ProjectConfig
from tightrein.domain.enums import RegressionResult
from tightrein.pipeline.checks import project_checks
from tightrein.sources.common.procs import Launcher, ToolCommand, tool_env
from tightrein.pipeline.checks.regressions.manifest import CheckEntry
from tightrein.pipeline.checks.regressions.runner import Execution, not_run

LOG_FILE = "{check}.test.log"

# (command, file) -> 执行目录(相对 worktree)；命令不在白名单或没有选中测试文件时为空
Resolve = Callable[[str, str], str | None]


class FailureKind(str, Enum):
    """测试失败的归因类别。"""
    TEST_BROKEN = "test-broken"    # 测试本身有问题(语法、导入、夹具)
    ENVIRONMENT = "environment"    # 环境问题(连接被拒、超时)
    ASSERTION = "assertion"        # 业务断言失败(有效结果)


def classify_failure(text: str, patterns: Mapping[str, Sequence[str]]) -> FailureKind:
    """按输出与配置的正则(不区分大小写)判断失败原因；都不命中时视为业务断言失败。"""
    for key, kind in (("environment", FailureKind.ENVIRONMENT), ("testBroken", FailureKind.TEST_BROKEN)):
        if any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns.get(key, ())):
            return kind
    return FailureKind.ASSERTION


@dataclass(frozen=True)
class BaseRun:
    """基准版本上的执行结果；environment 为真表示重试后仍是环境问题，应停下报告而不是重写测试。"""

    execution: Execution
    environment: bool = False


class RepoTestCheck:
    def __init__(self, launcher: Launcher, environ: Mapping[str, str], timeout_seconds: float,
                 resolve: Resolve, failure_codes: Collection[int], *,
                 failure_patterns: Mapping[str, Sequence[str]] | None = None, env_retries: int = 0) -> None:
        self.launcher = launcher
        self.environ = environ
        self.timeout_seconds = timeout_seconds
        self.resolve = resolve
        self.failure_codes = frozenset(failure_codes)
        self.failure_patterns = dict(failure_patterns or {})
        self.env_retries = env_retries

    @classmethod
    def configured(cls, launcher: Launcher, environ: Mapping[str, str], config: ProjectConfig,
                   resolve: Resolve) -> RepoTestCheck:
        return cls(launcher, environ, project_checks.timeout_seconds(config), resolve,
                   tuple(config.get("regressions.testFailureExitCodes")),
                   failure_patterns=config.get("regressions.testFailurePatterns"),
                   env_retries=int(config.get("regressions.envRetries")))

    def __call__(self, entry: CheckEntry, directory: Path, worktree: Path, raw_dir: Path) -> Execution:
        execution, _, _ = self._execute(entry, directory, worktree, raw_dir)
        return execution

    def base(self, entry: CheckEntry, directory: Path, worktree: Path, raw_dir: Path) -> BaseRun:
        """基准版本上的执行：测试失败时按归因与预期异常签名判断是否算有效复现。"""
        execution, kind, text = self._execute(entry, directory, worktree, raw_dir)
        if kind is FailureKind.ENVIRONMENT:
            return BaseRun(execution, environment=True)
        if execution.result is not RegressionResult.FAILED:
            return BaseRun(execution)
        if entry.expected_signature:
            if entry.expected_signature in text:
                return BaseRun(execution)
            return BaseRun(Execution(RegressionResult.INVALID, f"测试失败但输出不包含预期异常签名"
                                     f"「{entry.expected_signature}」，不算复现；{execution.detail}"))
        if kind is FailureKind.TEST_BROKEN:
            return BaseRun(Execution(RegressionResult.INVALID, f"测试本身有问题(语法、导入或夹具错误)，不算复现；"
                                     f"{execution.detail}"))
        return BaseRun(execution)

    def _execute(self, entry: CheckEntry, directory: Path, worktree: Path,
                 raw_dir: Path) -> tuple[Execution, FailureKind | None, str]:
        """执行测试；测试失败且输出命中环境问题时就地重试。返回结果、失败归因(未失败时为空)与输出文本。"""
        cwd = None if entry.command is None else self.resolve(entry.command, entry.file)
        if cwd is None:
            return Execution(RegressionResult.INVALID, f"命令不以 checks.commands 中某条命令开头或没有选中 "
                             f"{entry.file}：{entry.command}"), None, ""
        if not (worktree / entry.file).is_file():
            return not_run(f"测试文件 {entry.file} 不在 worktree 中"), None, ""
        raw_dir.mkdir(parents=True, exist_ok=True)
        log = raw_dir / LOG_FILE.format(check=f"{directory.name}-{entry.id}")
        for attempt in range(self.env_retries + 1):
            run = self.launcher(ToolCommand(tuple(shlex.split(entry.command)), worktree / cwd,
                                            self.timeout_seconds, tool_env(self.environ), log))
            if not run.started or run.timed_out or run.exit_code is None:
                return not_run(f"测试命令{run.describe()}，日志 {log}"), None, ""
            if run.exit_code == 0:
                return Execution(RegressionResult.PASSED, f"测试通过，日志 {log}"), None, ""
            if run.exit_code not in self.failure_codes:
                return Execution(RegressionResult.INVALID, f"测试命令退出码 {run.exit_code}，不是测试失败(收集错误、"
                                 f"用法错误或没有选中测试)，日志 {log}"), None, ""
            text = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""
            kind = classify_failure(text, self.failure_patterns)
            if kind is not FailureKind.ENVIRONMENT:
                return Execution(RegressionResult.FAILED, f"测试失败(退出码 {run.exit_code})，日志 {log}"), kind, text
        return not_run(f"测试输出为环境问题(连接被拒、超时等)，重试 {self.env_retries} 次后仍失败，日志 {log}"), \
            FailureKind.ENVIRONMENT, text
