"""路径模式与内容模式的匹配(architecture/02 3.8、3.9)，protectedPaths、testPaths、credentialFiles、
protectedPatterns、skipMarkers 共用。

路径模式取 gitignore 的常用子集，路径一律为相对 worktree 的 `/` 分隔路径：
- 以 `/` 结尾的只匹配目录，目录下的全部文件都算匹配：`deploy/`、`.github/`、`tests/`；
- 不含 `/`(结尾的除外)的在任意层级按名称匹配：`*.test.js`、`PermissionMatrix.cs`、`.env`；
- 含 `/` 的从 worktree 根目录起匹配：`Migrations/MigrationList.cs`、`src/*/appsettings*.json`；开头的 `/` 表示根目录；
- `*`、`?`、`[...]` 按 fnmatch 解释，区分大小写。
内容模式是字面文本，不是正则(`[Authorize]` 中的方括号不是字符类)；以字母、数字或下划线开头的模式要求前一个字符
不是字母、数字或下划线，避免 `xit(` 命中 `exit(`。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from fnmatch import fnmatchcase
from functools import lru_cache
from pathlib import PurePosixPath


def matches_path(path: str, pattern: str) -> bool:
    body = pattern.strip("/")
    if not body:
        return False
    segments = PurePosixPath(path).parts
    anchored = pattern.startswith("/") or "/" in body
    if pattern.endswith("/"):
        count = len(segments) - 1
        candidates = ["/".join(segments[:size]) for size in range(1, count + 1)] if anchored else segments[:count]
    else:
        candidates = ["/".join(segments[:size]) for size in range(1, len(segments) + 1)] if anchored else segments
    return any(fnmatchcase(candidate, body) for candidate in candidates)


def matching_pattern(path: str, patterns: Iterable[str]) -> str | None:
    """第一个匹配 path 的模式；都不匹配时为 None。"""
    return next((pattern for pattern in patterns if matches_path(path, pattern)), None)


@lru_cache(maxsize=256)
def _marker(marker: str) -> re.Pattern[str]:
    boundary = r"(?<!\w)" if re.match(r"\w", marker) else ""
    return re.compile(boundary + re.escape(marker))


def contains(line: str, marker: str) -> bool:
    return bool(marker) and _marker(marker).search(line) is not None


def matching_markers(line: str, markers: Iterable[str]) -> list[str]:
    return [marker for marker in markers if contains(line, marker)]
