"""测试与检查命令的输出精简(38-external-techniques.md 第 2 项)。

交给模型前只保留失败段落与报错行，去掉通过用例的打印与无关的框架堆栈；完整输出照常存为日志文件，
交接文档中只给引用路径。
"""

from __future__ import annotations

import re

# 失败段落开始的标记(匹配常见测试框架的输出格式)
_FAILURE_STARTS = re.compile(
    r"^("
    r"FAIL[ED]*[ :]|"                    # pytest, go test, jest
    r"ERROR[ :]|"                        # pytest collection error
    r"={3,} FAILURES ={3,}|"             # pytest section header
    r"_{3,} \S+ _{3,}|"                  # pytest individual test header
    r"FAILED |"                          # pytest summary line
    r"E   |"                             # pytest assertion detail
    r">\s+|"                             # pytest source line
    r"AssertionError|"                   # assertion errors
    r"assert |"                          # assertion statements
    r"×|✕|✗|"                            # unicode failure markers
    r"Traceback \(most recent call|"     # Python traceback
    r"  File \"|"                        # Python stack frame
    r"raise |"                           # raise statements
    r"\w+Error:|"                        # Python exception types
    r"\w+Exception:|"                    # Java/C# exceptions
    r"at \S+\(.+:\d+:\d+\)|"            # JS stack frames
    r"--- FAIL:|"                        # go test
    r"panic:|"                           # go panic
    r"FAILURES|"                         # junit
    r"not ok |"                          # TAP
    r"Failures: \d+"                     # NUnit
    r")",
    re.MULTILINE,
)

# 完全无关的行(可以安全丢弃)
_NOISE = re.compile(
    r"^("
    r"={3,} short test summary|"         # pytest short summary header (keep content after it)
    r"[-=]{20,}$|"                       # separators
    r"platform |"                        # pytest platform info
    r"cachedir:|"                        # pytest cache info
    r"rootdir:|"                         # pytest rootdir
    r"plugins:|"                         # pytest plugins
    r"collecting \.\.\.|"               # pytest collecting
    r"\.{5,}"                            # long dots (many passing tests)
    r")",
    re.MULTILINE,
)

# 通过测试的行
_PASS_LINE = re.compile(
    r"^("
    r"PASS[ED]*[ :]|"
    r"ok \d+|"                           # TAP pass
    r"✓|✔|"                              # unicode pass markers
    r"\s+\.\.\. (ok|passed|PASSED)|"     # verbose pass markers
    r"\S+::\S+\s+PASSED\b"              # pytest -v 的通过行；不能写成 .*PASSED，否则失败详情中含 PASSED 的行会结束失败段落
    r").*$",
    re.MULTILINE,
)

# 失败段落之前保留的上下文行数
_CONTEXT_LINES = 2


def trim(text: str, *, max_chars: int = 8000) -> str:
    """保留失败段落与报错行，去掉通过用例的打印与无关的框架堆栈；认不出失败段落时不筛选。

    结果不超过 max_chars：筛选后的超长时截掉末尾，未筛选的超长时保留末尾。
    """
    lines = text.splitlines()
    if not lines:
        return text

    keep: set[int] = set()
    matched = False
    in_failure = False  # 从失败标记开始，到下一条通过用例为止都算失败段落
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or _NOISE.match(stripped):
            continue
        if _PASS_LINE.match(stripped):
            in_failure = False
            continue
        if _FAILURE_STARTS.match(stripped):
            if not in_failure:
                keep.update(j for j in range(max(0, i - _CONTEXT_LINES), i)
                            if not _PASS_LINE.match(lines[j].strip()) and not _NOISE.match(lines[j].strip()))
            matched = in_failure = True
        if in_failure:
            keep.add(i)

    # 总是保留最后的统计摘要(最后 5 行中非空的)
    for i in range(max(0, len(lines) - 5), len(lines)):
        if lines[i].strip() and not _PASS_LINE.match(lines[i].strip()):
            keep.add(i)

    if not matched:
        # 不认识的输出格式：不做筛选，超长时保留末尾(报错与统计通常在最后)
        return text if len(text) <= max_chars else "[...输出截断]\n" + text[-max_chars:]
    result = "\n".join(lines[i] for i in sorted(keep))
    if len(result) > max_chars:
        result = result[:max_chars] + "\n[...输出截断]"
    return result
