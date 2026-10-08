"""检查命令的输出精简：交给模型前只留失败段落与报错行(前带 2 行上下文)和末尾统计，完整输出存日志只给路径。

- 通过行只认 `xxx::yyy PASSED` 这种精确写法：写成 `.*PASSED` 时，失败详情里含 PASSED 的行会提前结束失败段落；
- 认不出失败段落的格式时不筛选，超长时保留末尾(报错与统计通常在最后)；
- 日志可能很大：只从文件末尾读给定的字节数，不整体读进内存。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

TRUNCATED = "[...输出截断]"
CONTEXT_LINES = 2
SUMMARY_LINES = 5

_FAILURE_START = re.compile(
    r"^("
    r"FAIL[ED]*[ :]|ERROR[ :]|={3,} FAILURES ={3,}|_{3,} \S+ _{3,}|FAILED |E   |>\s+|AssertionError|assert |"
    r"×|✕|✗|Traceback \(most recent call|  File \"|raise |\w+Error:|\w+Exception:|at \S+\(.+:\d+:\d+\)|"
    r"--- FAIL:|panic:|FAILURES|not ok |Failures: \d+"
    r")"
)
_NOISE = re.compile(r"^(={3,} short test summary|[-=]{20,}$|platform |cachedir:|rootdir:|plugins:|collecting \.\.\.|\.{5,})")
_PASS_LINE = re.compile(r"^(PASS[ED]*[ :]|ok \d+|✓|✔|\s+\.\.\. (ok|passed|PASSED)|\S+::\S+\s+PASSED\b).*$")


def trim(text: str, *, max_chars: int) -> str:
    lines = text.splitlines()
    if not lines:
        return text
    keep: set[int] = set()
    matched = False
    in_failure = False  # 从失败标记开始，到下一条通过用例为止都算失败段落
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or _NOISE.match(stripped):
            continue
        if _PASS_LINE.match(stripped):
            in_failure = False
            continue
        if _FAILURE_START.match(stripped):
            if not in_failure:
                keep.update(before for before in range(max(0, index - CONTEXT_LINES), index)
                            if not _quiet(lines[before].strip()))
            matched = in_failure = True
        if in_failure:
            keep.add(index)
    keep.update(index for index in range(max(0, len(lines) - SUMMARY_LINES), len(lines))
                if lines[index].strip() and not _PASS_LINE.match(lines[index].strip()))
    if not matched:
        return text if len(text) <= max_chars else f"{TRUNCATED}\n{text[-max_chars:]}"
    result = "\n".join(lines[index] for index in sorted(keep))
    return result if len(result) <= max_chars else f"{result[:max_chars]}\n{TRUNCATED}"


def read_tail(path: Path, limit_bytes: int) -> str:
    """日志末尾至多 limit_bytes 字节；文件不存在时为空串。"""
    if not path.is_file():
        return ""
    with path.open("rb") as handle:
        size = handle.seek(0, os.SEEK_END)
        handle.seek(max(0, size - limit_bytes))
        data = handle.read()
    text = data.decode("utf-8", errors="replace")
    # 从中间截断时丢掉不完整的第一行
    return text.split("\n", 1)[-1] if size > limit_bytes else text


def _quiet(line: str) -> bool:
    return bool(_PASS_LINE.match(line) or _NOISE.match(line))
