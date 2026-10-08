"""status 与 watch 的样式：ANSI 256 色、加粗与下划线、按显示宽度对齐。

- 颜色照 44 号计划「样式参考」的颜色表，只用 256 色，不用终端暗蓝(ANSI 4)：深色背景上几乎看不见；
- 状态、数值、命令加粗；命令另加下划线，提示可以直接复制；
- 非终端(管道、重定向)或设了 NO_COLOR(https://no-color.org)时不着色，只输出文字；
- 一行由若干段(Span)组成，每段带一个语义角色；先按显示宽度截断、补齐，再着色，ANSI 转义不计入宽度；
- 中文等宽字符占两格(unicodedata.east_asian_width 为 W、F)；●、━ 等「宽度不定」(A)的字符按一格算，
  与 macOS 终端、iTerm2 的缺省设置一致。
"""

from __future__ import annotations

import os
import shutil
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TextIO

DEFAULT_WIDTH = 96
MIN_WIDTH = 72
MAX_WIDTH = 120
ELLIPSIS = "…"

# 角色 → (256 色编号, 加粗, 下划线)
ROLES: dict[str, tuple[int, bool, bool]] = {
    "frame": (248, False, False),    # 框线、连接线、辅助文字 #94A3B8
    "text": (254, False, False),     # 普通文字、标签 #E2E8F0
    "value": (231, True, False),     # 核心数值、标题 #FFFFFF
    "ok": (78, True, False),         # 就绪、正常、成功 #34D399
    "active": (81, True, False),     # 进行中、步骤 #38BDF8
    "warn": (214, True, False),      # 待审、警告、余量 #FBBF24
    "bad": (203, True, False),       # 阻塞、报错、急停 #F87171
    "command": (80, True, True),     # 可执行命令 #22D3EE
}
RESET = "\x1b[0m"


@dataclass(frozen=True)
class Span:
    text: str
    role: str = "text"


Line = list[Span]


def span(text: object, role: str = "text") -> Span:
    if role not in ROLES:
        raise ValueError(f"未知的样式角色：{role}")
    return Span(str(text), role)


def char_width(char: str) -> int:
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def display_width(text: str) -> int:
    return sum(char_width(char) for char in text)


def line_width(line: Iterable[Span]) -> int:
    return sum(display_width(item.text) for item in line)


def plain(line: Iterable[Span]) -> str:
    return "".join(item.text for item in line)


def fit(line: Line, width: int) -> Line:
    """截断到 width(超出时末尾换成省略号)并用空格补齐，使每行显示宽度相同：刷新时不留残字、框线对齐。"""
    current = line_width(line)
    if current <= width:
        return line + [Span(" " * (width - current), "text")] if current < width else line
    kept: Line = []
    used = 0
    limit = width - display_width(ELLIPSIS)
    for item in line:
        piece = ""
        for char in item.text:
            size = char_width(char)
            if used + size > limit:
                break
            piece += char
            used += size
        else:
            kept.append(item)
            continue
        if piece:
            kept.append(Span(piece, item.role))
        break
    kept.append(Span(ELLIPSIS + " " * (limit - used), "frame"))
    return kept


def pad_between(left: Line, right: Line, width: int, filler: str = " ", role: str = "text") -> Line:
    """左右两段之间用 filler 填满到 width；放不下时先截左段，右段保留。"""
    room = width - line_width(right)
    if line_width(left) > room - 1:
        left = fit(left, max(room - 1, 0))
    gap = room - line_width(left)
    return left + [Span(filler * gap, role)] + right


def paint(line: Iterable[Span], color: bool) -> str:
    if not color:
        return plain(line)
    out = []
    for item in line:
        if not item.text:
            continue
        number, bold, underline = ROLES[item.role]
        codes = [f"38;5;{number}"] + (["1"] if bold else []) + (["4"] if underline else [])
        if item.text.strip():
            out.append(f"\x1b[{';'.join(codes)}m{item.text}{RESET}")
        else:
            out.append(item.text)  # 只有空白时不加转义：下划线不画在空格上
    return "".join(out)


def use_color(stream: TextIO, environ: Mapping[str, str] | None = None) -> bool:
    environ = os.environ if environ is None else environ
    if environ.get("NO_COLOR"):
        return False
    isatty = getattr(stream, "isatty", None)
    return bool(isatty and isatty())


def terminal_width(stream: TextIO | None = None) -> int:
    """渲染宽度：终端宽度夹在 MIN_WIDTH 与 MAX_WIDTH 之间；非终端时取 DEFAULT_WIDTH。"""
    if stream is not None and not (getattr(stream, "isatty", None) and stream.isatty()):
        return DEFAULT_WIDTH
    columns = shutil.get_terminal_size((DEFAULT_WIDTH, 24)).columns
    return max(MIN_WIDTH, min(MAX_WIDTH, columns))


# 框


def box_top(title: Line, right: Line, width: int) -> Line:
    """┌─ 标题 ──────── 右侧 ─┐"""
    head = [Span("┌─ ", "frame"), *title, Span(" ", "frame")]
    tail = [Span(" ", "frame"), *right, Span(" ─┐", "frame")] if right else [Span("┐", "frame")]
    return pad_between(head, tail, width, "─", "frame")


def box_row(content: Line, width: int) -> Line:
    return [Span("│ ", "frame"), *fit(content, width - 4), Span(" │", "frame")]


def box_bottom(width: int) -> Line:
    return [Span("└" + "─" * (width - 2) + "┘", "frame")]


def bar(ratio: float | None, cells: int = 10) -> Line:
    """额度条 ━━━━━━──── ；未知时全为细线。用到留余量门槛以上的部分由调用方换色。"""
    filled = 0 if ratio is None else max(0, min(cells, round(ratio * cells)))
    return [Span("━" * filled, "active"), Span("─" * (cells - filled), "frame")]


def join(parts: Iterable[Line], separator: Line) -> Line:
    out: Line = []
    for index, part in enumerate(parts):
        if index:
            out += separator
        out += part
    return out


def render(lines: Iterable[Line], width: int, color: bool) -> str:
    return "\n".join(paint(fit(line, width), color) for line in lines)
