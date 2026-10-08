"""受影响的测试与检查：修正轮次只重跑这些，全量留给第一轮与通过前的最后一次。

- 受影响的测试按改动文件与项目的测试路径模式(project.testPatterns)推断：改动的本身是测试就跑它；
  改的是源码文件，就找名字对得上的测试文件(`foo.py` ↔ `test_foo.py`、`foo_test.go`、`foo.spec.ts`、`FooTests.cs`)；
  只保留仓库中存在的；
- 测试命令写了 `{tests}` 才能只跑一部分：有受影响的测试时展开为这些文件，没有时整组跑(全量时去掉占位)；
- 修正轮次的其他检查(lint、类型检查、构建)：上一轮没通过的，或这一轮又改了文件的才重跑；
- `when`(controls.implement.check.when，命令名 → 路径模式)：非全量时只跑相对基准的改动中有文件匹配的命令
  (如前端构建只在改了 web/ 时跑)；全量时不按它过滤。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath

from tightrein.protocol.boundaries import matching_pattern

TESTS_PLACEHOLDER = "{tests}"
TEST_COMMAND = "test"
PASSED = "passed"
# 测试文件名去掉这些前后缀后就是被测文件的名字
_TEST_AFFIXES = re.compile(r"^(test_|tests_)|(_test|_tests|\.test|\.spec|_spec|Tests|Test|Spec)$")


@dataclass(frozen=True)
class ProjectCommand:
    name: str  # test、lint、typecheck、build
    command: str

    @property
    def narrowable(self) -> bool:
        return TESTS_PLACEHOLDER in self.command


@dataclass(frozen=True)
class Planned:
    command: ProjectCommand
    tests: tuple[str, ...] = ()  # 只跑这些测试；为空时整组


def configured(commands: Mapping[str, str | None]) -> list[ProjectCommand]:
    """项目配置的检查命令(settings.json 的 project.commands)，没探测到的(null)跳过。"""
    return [ProjectCommand(name, command) for name, command in commands.items() if command]


def selected(commands: Sequence[ProjectCommand], changed: Iterable[str],
             when: Mapping[str, Sequence[str]]) -> list[ProjectCommand]:
    """非全量时按 when 选命令：没写 when 的命令总是选；写了的要有改动文件匹配其中一个模式。"""
    paths = list(changed)
    return [command for command in commands if command.name not in when
            or any(matching_pattern(path, when[command.name]) is not None for path in paths)]


def index_tests(files: Iterable[str], patterns: Sequence[str]) -> dict[str, list[str]]:
    """被测文件名 → 测试文件：一次建好，按改动文件逐个查(O(1))。"""
    index: dict[str, list[str]] = {}
    for path in files:
        if matching_pattern(path, patterns) is not None:
            index.setdefault(_subject(path), []).append(path)
    return index


def affected_tests(changed: Iterable[str], index: Mapping[str, Sequence[str]], patterns: Sequence[str]) -> list[str]:
    known = {test for tests in index.values() for test in tests}
    found: list[str] = []
    for path in changed:
        candidates = [path] if matching_pattern(path, patterns) is not None else index.get(PurePosixPath(path).stem, ())
        found += [test for test in candidates if test in known and test not in found]
    return found


def plan(commands: Sequence[ProjectCommand], *, full: bool, tests: Sequence[str], changed_this_round: Sequence[str],
         previous: Mapping[str, str]) -> list[Planned]:
    """previous：上一次每条命令的结果(passed、failed …)。"""
    if full:
        return [Planned(command) for command in commands]
    planned = []
    for command in commands:
        failed_before = previous.get(command.name) != PASSED
        if command.name == TEST_COMMAND and command.narrowable:
            if tests:
                planned.append(Planned(command, tuple(tests)))
            elif failed_before:
                planned.append(Planned(command))
        elif failed_before or changed_this_round:
            planned.append(Planned(command))
    return planned


def remaining(commands: Sequence[ProjectCommand], done: Sequence[Planned]) -> list[Planned]:
    """受影响的都过了以后补跑的全量：没跑过的，以及只跑了一部分测试的。"""
    complete = {item.command.name for item in done if not item.tests}
    return [Planned(command) for command in commands if command.name not in complete]


def argv_text(command: ProjectCommand, tests: Sequence[str]) -> str:
    """把 `{tests}` 展开为测试文件(全量时去掉)。路径以 shell 记号拼接，由调用方 shlex 拆分。"""
    if not command.narrowable:
        return command.command
    return command.command.replace(TESTS_PLACEHOLDER, " ".join(_quote(test) for test in tests)).strip()


def _subject(path: str) -> str:
    return _TEST_AFFIXES.sub("", PurePosixPath(path).stem)


def _quote(value: str) -> str:
    return value if re.fullmatch(r"[\w./@:+-]+", value) else "'" + value.replace("'", "'\"'\"'") + "'"
