"""边界与关卡(protocol/boundaries.md)：改动计数、每轮改动检查、两级受保护文件、命令白名单、读取越界、关卡。

只看改动的文件，不调用模型；规则的取值都在 settings 的 `boundaries` 段，项目只能在受保护文件两级上追加。

路径模式取 gitignore 的常用子集，路径一律是相对 worktree、以 `/` 分隔：
- 以 `/` 结尾的只配目录，目录下的全部文件都算命中：`migrations/`、`.github/workflows/`；
- 不含 `/`(结尾的除外)的在任意层级按名称匹配：`*.pem`、`.env`、`package.json`；
- 含 `/` 的从根目录起匹配：`src/*/appsettings*.json`；开头的 `/` 表示根目录；
- `*`、`?`、`[...]` 按 fnmatch 解释，区分大小写。
"""

from __future__ import annotations

import os
import re
import shlex
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any

from tightrein.settings.load import ProjectFacts, Settings

OUTSIDE = "outside"
FORBIDDEN = "forbidden"
OVER_CAP = "over_cap"
READ_ONLY_CHANGED = "read_only_changed"
COMMAND = "command"
HIDDEN_READ = "hidden_read"

# 只提交最终结果、不访问文件的工具：参数是结论正文，正文中引用的代码(如 `it.key`)不是路径
OUTPUT_ONLY_TOOLS = frozenset({"StructuredOutput"})
# 从工具参数中切出可能是路径的片段
_PATH_SEPARATORS = re.compile(r"[\s'\"`=,;|&<>()]+")

# 固定人工的关卡：不可逆或越权的情形，配置改不成自动
MANDATORY_GATES: frozenset[str] = frozenset({"forbidden_changed", "high_risk_merge", "over_cap", "needs_decision"})
AUTO_PREFIX = "auto"

# shlex 以 punctuation_chars 拆出的这些记号说明命令里有串联、管道、重定向或子命令
_SHELL_OPERATORS = frozenset(";&|<>()")
_SUBSTITUTIONS = ("`", "$(")


@dataclass(frozen=True)
class Change:
    """一个改动的文件；status 取 git 的状态字母(A、M、D、R)，未跟踪的新文件为 `?`。"""

    path: str
    added: int
    deleted: int
    status: str


@dataclass(frozen=True)
class Violation:
    kind: str  # outside、forbidden、over_cap、read_only_changed、command
    path: str | None
    detail: str


def matches_path(path: str, pattern: str) -> bool:
    body = pattern.strip("/")
    if not body:
        return False
    segments = PurePosixPath(path).parts
    anchored = pattern.startswith("/") or "/" in body
    # 只配目录的模式不拿文件名本身比较，只比较它所在的各级目录
    size = len(segments) - 1 if pattern.endswith("/") else len(segments)
    candidates = ["/".join(segments[:end]) for end in range(1, size + 1)] if anchored else segments[:size]
    return any(fnmatchcase(candidate, body) for candidate in candidates)


def matching_pattern(path: str, patterns: Iterable[str]) -> str | None:
    """第一个命中 path 的模式；都不命中时为 None。"""
    return next((pattern for pattern in patterns if matches_path(path, pattern)), None)


def forbidden(paths: Iterable[str], settings: Settings) -> list[str]:
    """命中禁改级受保护路径的文件：agent 不能改，碰到就停下交人。"""
    return _matching(paths, tuple(settings.get("boundaries.protected.forbidden")))


def high_risk(paths: Iterable[str], settings: Settings) -> list[str]:
    """命中高风险级受保护路径的文件：可以改，合并必须人工确认。"""
    return _matching(paths, tuple(settings.get("boundaries.protected.highRisk")))


def counted(changes: Sequence[Change], settings: Settings) -> tuple[int, int]:
    """计入改动量上限的文件数与增删行数：不算测试文件、锁文件与生成的文件。"""
    excluded = _uncounted_patterns(settings, settings.project)
    kept = [change for change in changes if matching_pattern(change.path, excluded) is None]
    return len(kept), sum(change.added + change.deleted for change in kept)


def check_round(changes: Sequence[Change], *, worktree: Path, settings: Settings, project: ProjectFacts | None,
                tests_only: bool = False) -> list[Violation]:
    """一轮可写步骤结束后的检查：越界(worktree 之外、只写测试的任务改了测试以外的文件)、禁改文件、超量。"""
    violations = [Violation(OUTSIDE, change.path, "改动落在 worktree 之外")
                  for change in changes if not _inside(worktree, change.path)]
    if tests_only:
        tests = project.test_patterns if project is not None else ()
        violations += [Violation(OUTSIDE, change.path, "只写测试的任务改了测试以外的文件")
                       for change in changes if matching_pattern(change.path, tests) is None]
    violations += [Violation(FORBIDDEN, path, "命中禁改文件") for path in forbidden((c.path for c in changes), settings)]
    files, lines = counted(changes, settings)
    cap = settings.get("boundaries.changeCap")
    if files > cap["files"]:
        violations.append(Violation(OVER_CAP, None, f"改动了 {files} 个文件(不含测试与生成文件)，超过上限 {cap['files']}"))
    if lines > cap["lines"]:
        violations.append(Violation(OVER_CAP, None, f"增删 {lines} 行(不含测试与生成文件)，超过上限 {cap['lines']}"))
    return violations


def command_allowed(command: str, allowed: Sequence[str]) -> bool:
    """命令以白名单中某一条开头(按 shell 记号比较)，且不含串联、管道、重定向与子命令。"""
    if any(marker in command for marker in _SUBSTITUTIONS):
        return False
    tokens = _tokens(command)
    if not tokens or any(set(token) <= _SHELL_OPERATORS for token in tokens):
        return False
    for entry in allowed:
        prefix = _tokens(entry)
        if prefix and tuple(tokens[: len(prefix)]) == tuple(prefix):
            return True
    return False


def hidden_reads(calls: Iterable[tuple[str, Any]], *, workdir: Path, hidden: Sequence[Path],
                 credential_patterns: Sequence[str], allowed: Sequence[Path] = (), home: Path | None = None) -> list[str]:
    """会话记录中的工具调用((工具名, 参数))读了隐藏目录或凭据文件的，每个路径说明一次。

    参数中所有字符串按分隔符切成片段，含 `/` 或 `.` 的当作路径：相对路径按 workdir 解析，`~/` 按 home 展开，
    解析符号链接后落在 hidden 某项之内(且不在 allowed 之内)的算读了隐藏目录；文件名匹配凭据文件模式的，只有解析后的
    路径确实存在时才算读取(搜索文本或代码片段中的 `it.key` 不指向文件)。只提交结果的工具(OUTPUT_ONLY_TOOLS)不检查。
    """
    roots = [os.path.realpath(path) for path in hidden]
    permitted = [os.path.realpath(path) for path in allowed]
    found: dict[str, str] = {}
    for name, arguments in calls:
        if name in OUTPUT_ONLY_TOOLS:
            continue
        for text in _strings(arguments):
            for token in _PATH_SEPARATORS.split(text):
                if not token or token in found or ("/" not in token and "." not in token):
                    continue
                expanded = str(home / token[2:]) if home is not None and token.startswith("~/") else token
                resolved = os.path.realpath(expanded if os.path.isabs(expanded) else workdir / expanded)
                inside = _within(resolved, roots) and not _within(resolved, permitted)
                pattern = matching_pattern(PurePosixPath(token).as_posix(), credential_patterns)
                if pattern is not None and not os.path.exists(resolved):
                    pattern = None
                if inside or pattern is not None:
                    reason = "隐藏目录" if inside else f"凭据文件(匹配 {pattern})"
                    found[token] = f"{HIDDEN_READ}：{name or '工具'} 访问了{reason} {token}"
    return list(found.values())


def gate_is_auto(gate: str, settings: Settings) -> bool:
    """可配置关卡的取值以 `auto` 开头即按条件自动(条件由该关卡的发起方判断)；固定人工的关卡永远为否。"""
    if gate in MANDATORY_GATES:
        return False
    return str(settings.get(f"boundaries.gates.{gate}")).startswith(AUTO_PREFIX)


def _matching(paths: Iterable[str], patterns: tuple[str, ...]) -> list[str]:
    return sorted({path for path in paths if matching_pattern(path, patterns) is not None})


def _uncounted_patterns(settings: Settings, project: ProjectFacts | None) -> tuple[str, ...]:
    tests = project.test_patterns if project is not None else ()
    return (*settings.get("boundaries.uncounted"), *tests)


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def _within(path: str, roots: Sequence[str]) -> bool:
    return any(path == root or path.startswith(root + os.sep) for root in roots)


def _inside(worktree: Path, path: str) -> bool:
    """解析符号链接后仍在 worktree 之内：agent 建一个指向外面的链接也算越界。"""
    root = os.path.realpath(worktree)
    target = os.path.realpath(os.path.join(root, path))
    return target == root or target.startswith(root + os.sep)


@lru_cache(maxsize=512)
def _tokens(command: str) -> tuple[str, ...]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return tuple(lexer)
    except ValueError:  # 引号不成对
        return ()
