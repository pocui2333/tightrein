"""项目检查命令的选择与执行(architecture/07 4.9、13 第 1 步、14 第 3 步)，fix 与 verify 共用。

- `checks.commands[]`：name、cwd(相对 worktree)、command、when(改动文件匹配任一路径模式才执行，写法同受保护路径)、
  mustNotModify(执行后工作区不得出现新改动)、affected(只运行受影响的测试)。
- affected：`map` 的每一项以正则 pattern 匹配改动文件(相对 worktree)，按 test 模板替换得到测试文件(相对 worktree)，
  只保留存在的文件；`command` 中的 `{tests}` 展开为这些文件(相对 cwd)。映射为空时运行 command 本身覆盖的整组测试。
- full 为真(修复第 1、7 步与接入时在基准版本上自检)时不按 when 过滤，也不使用 affected。
命令以 shlex 拆成参数数组、不经 shell 执行，超时取 checks.timeoutSeconds(超时的退出码记为空，按不通过处理)；
输出写到 log_dir/<名称>.log；命令无法启动记为「未运行」，由调用方按「未运行」处理。
- 测试类复现检查的命令白名单(repro_test_cwd)：shlex 拆分后开头须为某条命令的允许前缀(完整命令、去掉末尾含 `/` 的
  路径参数后的部分、affected.command 中 `{tests}` 之前的部分)，其余参数中须有一个是测试文件(相对该命令的 cwd)或以
  `<测试文件>::` 开头；返回该命令的 cwd。
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tightrein.config.project import MissingSetting, ProjectConfig
from tightrein.guards.protected import matches_path
from tightrein.sources.common.procs import Launcher, ToolCommand, tool_env

TESTS_PLACEHOLDER = "{tests}"
LOG_SUFFIX = ".log"

WorktreeState = Callable[[Path], Mapping[str, str]]


@dataclass(frozen=True)
class Affected:
    map: tuple[tuple[str, str], ...]
    command: str


@dataclass(frozen=True)
class CheckCommand:
    name: str
    cwd: str
    command: str
    when: tuple[str, ...] = ()
    must_not_modify: bool = False
    affected: Affected | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CheckCommand:
        affected = data.get("affected")
        return cls(data["name"], data["cwd"], data["command"], tuple(data.get("when", ())),
                   bool(data.get("mustNotModify", False)),
                   None if affected is None else Affected(tuple((item["pattern"], item["test"])
                                                                for item in affected["map"]), affected["command"]))


@dataclass(frozen=True)
class CheckRun:
    name: str
    command: str
    exit_code: int | None
    log: Path
    modified: tuple[str, ...] = ()
    not_run_reason: str | None = None

    @property
    def passed(self) -> bool:
        return self.exit_code == 0 and not self.modified and self.not_run_reason is None

    @property
    def not_run(self) -> bool:
        return self.not_run_reason is not None


def commands(config: ProjectConfig, key: str = "checks.commands") -> list[CheckCommand]:
    try:
        items = config.get(key)
    except MissingSetting:
        return []
    return [CheckCommand.from_dict(item) for item in items]


def timeout_seconds(config: ProjectConfig) -> float:
    return float(config.get("checks.timeoutSeconds"))


def selected(items: Sequence[CheckCommand], changed: Sequence[str], *, full: bool = False) -> list[CheckCommand]:
    if full:
        return list(items)
    return [item for item in items if not item.when
            or any(matches_path(path, pattern) for path in changed for pattern in item.when)]


def affected_tests(command: CheckCommand, changed: Sequence[str], worktree: Path) -> list[str]:
    if command.affected is None:
        return []
    found: list[str] = []
    for path in changed:
        for pattern, template in command.affected.map:
            if re.search(pattern, path) is None:
                continue
            test = re.sub(pattern, template, path)
            if (worktree / test).is_file() and test not in found:
                found.append(test)
    return found


def argv_for(command: CheckCommand, changed: Sequence[str], worktree: Path, *, full: bool = False) -> tuple[str, ...]:
    tests = [] if full else affected_tests(command, changed, worktree)
    if not tests or command.affected is None:
        return tuple(shlex.split(command.command))
    relative = [os.path.relpath(worktree / test, worktree / command.cwd) for test in tests]
    argv: list[str] = []
    for part in shlex.split(command.affected.command):
        argv += relative if part == TESTS_PLACEHOLDER else [part]
    return tuple(argv)


def file_state(worktree: Path, paths: Sequence[str]) -> dict[str, str]:
    """工作区中给定文件的内容哈希；文件不存在时为空串。供 mustNotModify 比较执行前后的状态。"""
    state = {}
    for path in paths:
        target = worktree / path
        state[path] = hashlib.sha256(target.read_bytes()).hexdigest() if target.is_file() else ""
    return state


def run(items: Sequence[CheckCommand], worktree: Path, changed: Sequence[str], launcher: Launcher, log_dir: Path, *,
        timeout: float, state: WorktreeState, environ: Mapping[str, str], full: bool = False) -> list[CheckRun]:
    log_dir.mkdir(parents=True, exist_ok=True)
    runs = []
    for command in selected(items, changed, full=full):
        argv = argv_for(command, changed, worktree, full=full)
        log = log_dir / f"{command.name}{LOG_SUFFIX}"
        before = state(worktree) if command.must_not_modify else {}
        result = launcher(ToolCommand(argv, worktree / command.cwd, timeout, tool_env(environ), log))
        if not result.started:
            runs.append(CheckRun(command.name, " ".join(argv), None, log, not_run_reason=result.describe()))
            continue
        modified: tuple[str, ...] = ()
        if command.must_not_modify:
            after = state(worktree)
            modified = tuple(sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path)))
        exit_code = None if result.timed_out else result.exit_code
        runs.append(CheckRun(command.name, " ".join(argv), exit_code, log, modified))
    return runs


SELECTOR = "::"


def _prefixes(command: CheckCommand) -> list[tuple[str, ...]]:
    tokens = tuple(shlex.split(command.command))
    found = [tokens]
    trimmed = tokens
    while trimmed and "/" in trimmed[-1]:
        trimmed = trimmed[:-1]
    if trimmed and trimmed != tokens:
        found.append(trimmed)
    if command.affected is not None:
        parts = shlex.split(command.affected.command)
        if TESTS_PLACEHOLDER in parts and parts.index(TESTS_PLACEHOLDER) > 0:
            found.append(tuple(parts[:parts.index(TESTS_PLACEHOLDER)]))
    return found


def repro_test_prefixes(items: Sequence[CheckCommand]) -> list[str]:
    """测试类复现检查命令的允许前缀(去重，保持顺序)，供提示列出。"""
    return list(dict.fromkeys(shlex.join(prefix) for command in items for prefix in _prefixes(command)))


def _selects(arguments: Sequence[str], file: str, cwd: str) -> bool:
    wanted = os.path.normpath(os.path.relpath(file, cwd))
    return any(path and os.path.normpath(path) == wanted
               for path in (argument.split(SELECTOR, 1)[0] for argument in arguments))


def repro_test_cwd(items: Sequence[CheckCommand], command: str, file: str) -> str | None:
    try:
        argv = tuple(shlex.split(command))
    except ValueError:
        return None
    for candidate in items:
        for prefix in _prefixes(candidate):
            if argv[:len(prefix)] == prefix and _selects(argv[len(prefix):], file, candidate.cwd):
                return candidate.cwd
    return None
