"""status 与 watch 的样式：前景色、加粗与下划线、按显示宽度对齐。

- 浅色与深色背景都要看得清：普通文字不着色(用终端的缺省前景色)，数值只加粗；状态用 16 色里的绿、青、黄、红，
  跟着终端的配色方案走；辅助文字与框线用中灰(256 色 244)，两种背景上都有对比度。不用 256 色的近白(231、254)：
  浅色背景上几乎看不见；也不用暗蓝(ANSI 4)：深色背景上几乎看不见；
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

DEFAULT_HEIGHT = 24  # 非终端时 watch 的行数
MIN_HEIGHT = 18
MAX_HEIGHT = 60

# 角色 → (前景色的 SGR 参数，空为终端缺省前景色；加粗；下划线)
ROLES: dict[str, tuple[str, bool, bool]] = {
    "frame": ("38;5;244", False, False),  # 框线、连接线、辅助文字：中灰 #808080，浅色与深色背景上都看得清
    "text": ("", False, False),           # 普通文字、标签：终端缺省前景色
    "value": ("", True, False),           # 核心数值、标题：缺省前景色加粗
    "ok": ("32", True, False),            # 就绪、正常、成功：绿
    "active": ("36", True, False),        # 进行中、步骤：青
    "warn": ("33", True, False),          # 待审、警告、余量：黄
    "bad": ("31", True, False),           # 阻塞、报错、急停：红
    "command": ("36", True, True),        # 可执行命令：青加下划线
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
        foreground, bold, underline = ROLES[item.role]
        codes = ([foreground] if foreground else []) + (["1"] if bold else []) + (["4"] if underline else [])
        if item.text.strip() and codes:
            out.append(f"\x1b[{';'.join(codes)}m{item.text}{RESET}")
        else:
            out.append(item.text)  # 只有空白时不加转义(下划线不画在空格上)；普通文字不加转义
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


def terminal_height(stream: TextIO | None = None) -> int:
    """watch 的行数：终端行数夹在 MIN_HEIGHT 与 MAX_HEIGHT 之间；非终端时取 DEFAULT_HEIGHT。"""
    if stream is not None and not (getattr(stream, "isatty", None) and stream.isatty()):
        return DEFAULT_HEIGHT
    rows = shutil.get_terminal_size((DEFAULT_WIDTH, DEFAULT_HEIGHT)).lines
    return max(MIN_HEIGHT, min(MAX_HEIGHT, rows))


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


def bar(ratio: float | None, cells: int = 10, role: str = "active") -> Line:
    """额度条 ━━━━━━──── ；未知时全为细线。过了留量线时调用方传 warn、bad 换色。"""
    filled = 0 if ratio is None else max(0, min(cells, round(ratio * cells)))
    return [Span("━" * filled, role), Span("─" * (cells - filled), "frame")]


def rule(title: Line, width: int, right: Line | None = None) -> Line:
    """分节线 ── 标题 ─────────── 右侧 ─；没有标题时整行细线。"""
    if not title:
        return [Span("─" * width, "frame")]
    head = [Span("── ", "frame"), *title, Span(" ", "frame")]
    tail = [Span(" ", "frame"), *right, Span(" ──", "frame")] if right else [Span("──", "frame")]
    return pad_between(head, tail, width, "─", "frame")


def join(parts: Iterable[Line], separator: Line) -> Line:
    out: Line = []
    for index, part in enumerate(parts):
        if index:
            out += separator
        out += part
    return out


def render(lines: Iterable[Line], width: int, color: bool) -> str:
    return "\n".join(paint(fit(line, width), color) for line in lines)
