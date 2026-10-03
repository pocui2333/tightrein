"""第 5 步 写复现测试(redesign/05-fix.md)：写一个表达验收标准的测试，程序在基准版本上验证它。

- 任务由 fix-executor 的第一轮执行(会话编号留给第 6 步续接)，安全、数据类由 repro-writer 在另一会话写；
  任务只允许改动测试文件(RunnerTask.tests_only，第二层边界检查拒绝其他改动)；
- 程序检查：改动只有 testPaths 中的文件，输出的 file 是新建的文件(不改已有测试)，command 以 checks.commands 的允许前缀开头并选中它
  (project_checks.repro_test_cwd)；在基准版本(只加了这个测试)上运行：缺陷与新功能须失败，重构的表征测试须通过；
  退出码按 regressions.testFailureExitCodes 判定，无法执行的不算；
- 缺陷类测试在基准版本上就通过，说明问题不成立，结果为 not-reproduced，由调用方退回分诊；其余不合格的带原因交回重写，
  重写续接同一会话，用尽 thresholds.fix.planRounds 次仍不合格时为 failed，写不出(cannot-write)时直接返回，由调用方转待决定；
- 合格的测试登记为本 Issue 的测试类复现检查(regressions/<编号>/，与已有的确定性复现检查合在一份清单)，结果写 repro.json。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tightrein.domain.enums import RegressionKind, RegressionResult, RunnerStatus
from tightrein.guards.protected import matching_pattern
from tightrein.pipeline.checks import project_checks
from tightrein.pipeline.checks.project_checks import CheckCommand
from tightrein.pipeline.checks.regressions import manifest
from tightrein.pipeline.checks.regressions.manifest import CHECKLIST, CheckEntry, ManifestInvalid
from tightrein.pipeline.checks.regressions.repo_test_check import RepoTestCheck
from tightrein.pipeline.fix.prompts.common import FixCalls
from tightrein.pipeline.fix.steps import repro
from tightrein.runner.task import RunnerTask
from tightrein.store.files import atomic
from tightrein.store.repos.regressions import RegressionCheck

FILE = "repro.json"
PASSED, NOT_REPRODUCED, CANNOT_WRITE, FAILED = "passed", "not-reproduced", "cannot-write", "failed"
ENVIRONMENT = "environment"  # 基准版本上的执行重试后仍是环境问题
TEST_PREFIX = "test-"


@dataclass(frozen=True)
class TestRules:
    """验证复现测试所需的项目事实与执行器。"""

    worktree: Path
    test_paths: tuple[str, ...]
    commands: tuple[CheckCommand, ...]
    status: Callable[[Path], Collection[str]]
    untracked: Callable[[Path], Collection[str]]  # 新建(未跟踪)的文件；复现测试须是其中之一
    runner: RepoTestCheck
    directory: Path
    raw_dir: Path


@dataclass
class ReproTest:
    outcome: str
    reason: str = ""
    output: Mapping[str, Any] | None = None
    session_id: str | None = None
    base_result: str = ""
    attempts: list[RunnerStatus] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.outcome == PASSED

    def entry(self, check_id: str) -> CheckEntry:
        output = self.output or {}
        return CheckEntry(check_id, RegressionKind.TEST, output["file"], output["location"], command=output["command"],
                          expected_signature=output.get("expectedSignature") or None)


def problems(output: Mapping[str, Any], rules: TestRules) -> list[str]:
    found = []
    if not rules.test_paths:
        return ["项目没有配置 testPaths，不能写复现测试"]
    changed = set(rules.status(rules.worktree))
    outside = sorted(path for path in changed if matching_pattern(path, rules.test_paths) is None)
    if outside:
        found.append(f"只能改动测试文件，以下文件不在 testPaths 中：{'、'.join(outside)}")
    created = set(rules.untracked(rules.worktree))
    edited = sorted(path for path in changed - created if path not in outside)
    if edited:
        found.append(f"改动了已有的测试文件 {'、'.join(edited)}：复现测试须新建独立的文件，已有测试恢复原样")
    if output["file"] not in created and output["file"] not in edited:
        found.append(f"测试文件 {output['file']} 没有新建")
    if matching_pattern(output["file"], rules.test_paths) is None or manifest.unsafe_path(output["file"]):
        found.append(f"{output['file']} 须为仓库内匹配 testPaths 的测试文件")
    if project_checks.repro_test_cwd(rules.commands, output["command"], output["file"]) is None:
        prefixes = "；".join(project_checks.repro_test_prefixes(rules.commands)) or "无(没有 checks.commands)"
        found.append(f"command 须以项目检查命令的允许前缀开头并选中 {output['file']}：允许前缀为 {prefixes}")
    return found


def write(calls: FixCalls, build: Callable[[int, Sequence[str]], RunnerTask], rules: TestRules, *,
          expects_pass: bool, defect: bool, rounds: int) -> ReproTest:
    """build(attempt, feedback) 组装任务；重写续接上一次的会话。"""
    feedback: list[str] = []
    found = ReproTest(FAILED)
    for attempt in range(1, rounds + 2):
        result = calls.run(build(attempt, feedback), resume_session=found.session_id)
        found.attempts.append(result.status)
        found.session_id = result.session_id or found.session_id
        if result.status is not RunnerStatus.OK or result.output is None:
            found.reason = f"写复现测试的执行器返回 {result.status.value}" + (
                f"({result.error_type})" if result.error_type else "")
            feedback = [f"上一次执行没有完成：{found.reason}"]
            continue
        output = result.output
        found.output = output
        if output["status"] == "cannot-write":
            found.outcome, found.reason = CANNOT_WRITE, output["reason"] or "没有给出原因"
            return found
        feedback = problems(output, rules)
        if feedback:
            found.reason = "；".join(feedback)
            continue
        base = rules.runner.base(found.entry(f"{TEST_PREFIX}base"), rules.directory, rules.worktree, rules.raw_dir)
        execution = base.execution
        expected = RegressionResult.PASSED if expects_pass else RegressionResult.FAILED
        found.base_result = f"{execution.result.value}：{execution.detail}"
        if base.environment:
            found.outcome, found.reason = ENVIRONMENT, execution.detail
            return found
        if execution.result is expected:
            found.outcome, found.reason = PASSED, ""
            return found
        if execution.result is RegressionResult.PASSED and defect:
            found.outcome = NOT_REPRODUCED
            found.reason = f"复现测试在基准版本上通过，问题不成立：{execution.detail}"
            return found
        if execution.result is RegressionResult.PASSED:
            feedback = [f"测试在当前代码上已经通过，没有表达验收标准：{execution.detail}"]
        elif execution.result is RegressionResult.FAILED:
            feedback = [f"表征测试在当前代码上失败，应固定现有行为：{execution.detail}"]
        else:
            feedback = [f"测试无法在当前代码上执行，不算复现：{execution.detail}"]
        found.reason = "；".join(feedback)
    return found


def register(conn: sqlite3.Connection | None, directory: Path, relative: str, issue_id: str,
             fingerprints: Sequence[str], test: ReproTest, worktree: Path) -> RegressionCheck:
    """把测试登记为测试类复现检查；本步之前写的测试类检查被替换，确定性的接口、页面、静态检查保留。"""
    kept: list[dict[str, Any]] = []
    try:
        loaded = manifest.load(directory)
        kept = [entry.to_dict() for entry in loaded.checks if not entry.id.startswith(TEST_PREFIX)]
    except ManifestInvalid:
        pass
    entry = test.entry(f"{TEST_PREFIX}1")
    content = (worktree / entry.file).read_text(encoding="utf-8")
    data = {"issue": issue_id, "problems": list(fingerprints), "checks": [*kept, entry.to_dict()]}
    if (directory / CHECKLIST).is_file():
        (directory / CHECKLIST).unlink()
    checks = repro.write(conn, directory, relative, repro.ReproFiles(data, {entry.stored_name: content}, "fix"))
    return next(check for check in checks if check.check_id == entry.id)


def save(fix_dir: Path, values: Mapping[str, Any]) -> None:
    atomic.write_text(fix_dir / FILE, json.dumps(dict(values), ensure_ascii=False, indent=2) + "\n")


def load(fix_dir: Path) -> dict[str, Any] | None:
    path = fix_dir / FILE
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
