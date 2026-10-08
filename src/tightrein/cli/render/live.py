"""watch 的实时刷新：按间隔重新取数、清屏重绘；按键 [q] 退出、[p] 暂停、[s] 急停、[c] 切换采集看板。

- 只用标准库：终端切到逐键读取(cbreak：不回显、不等回车，Ctrl-C 仍能中断)，退出时一定恢复；
- 用备用屏幕(像 less、top)，退出后回到原来的终端内容；重绘时光标回到左上角整屏覆盖，不先清屏，避免闪烁；
- 标准输入不是终端(管道、测试)时不读键，只按间隔刷新；
- p、s 交给调用方(命令壳写运行控制)，watch 自己只读不写。
"""

from __future__ import annotations

import os
import select
import sys
import termios
import time
import tty
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TextIO

from tightrein.cli.render.snapshot import WatchSnapshot
from tightrein.cli.render.style import terminal_width, use_color
from tightrein.cli.render.watch import render_watch

INTERVAL_S = 2.0
QUIT, PAUSE, STOP, COLLECT = "q", "p", "s", "c"
_ENTER = "\x1b[?1049h\x1b[?25l"  # 备用屏幕、隐藏光标
_LEAVE = "\x1b[?25h\x1b[?1049l"
_HOME = "\x1b[H"
_CLEAR_BELOW = "\x1b[J"


def run(fetch: Callable[[], WatchSnapshot], language: str, *, collect_view: bool, on_key: Callable[[str], None],
        interval_s: float = INTERVAL_S, stdin: TextIO | None = None, stdout: TextIO | None = None,
        read_key: Callable[[float], str | None] | None = None) -> None:
    """read_key 给出时不碰终端设置，由它读键(测试用)。"""
    source = sys.stdin if stdin is None else stdin
    out = sys.stdout if stdout is None else stdout
    color = use_color(out)
    screen = bool(getattr(out, "isatty", None) and out.isatty())
    if screen:
        out.write(_ENTER)
    try:
        with _keys(source, read_key) as next_key:
            while True:
                frame = render_watch(fetch(), language, collect_view=collect_view, width=terminal_width(out),
                                     color=color)
                out.write((_HOME + frame + _CLEAR_BELOW) if screen else frame + "\n\n")
                out.flush()
                key = next_key(interval_s)
                if key == QUIT:
                    return
                if key == COLLECT:
                    collect_view = not collect_view
                elif key in (PAUSE, STOP):
                    on_key(key)
    except KeyboardInterrupt:
        return  # Ctrl-C 与 q 一样是正常的退出方式
    finally:
        if screen:
            out.write(_LEAVE)
            out.flush()


@contextmanager
def _keys(stdin: TextIO, given: Callable[[float], str | None] | None) -> Iterator[Callable[[float], str | None]]:
    """返回的函数最多等 timeout 秒，返回按下的键(小写)，超时为 None。"""
    if given is not None:
        yield given
        return
    isatty = getattr(stdin, "isatty", None)
    if not (isatty and isatty()):
        yield _sleep
        return
    descriptor = stdin.fileno()
    saved = termios.tcgetattr(descriptor)
    tty.setcbreak(descriptor)

    def read(timeout: float) -> str | None:
        ready, _, _ = select.select([descriptor], [], [], timeout)
        return os.read(descriptor, 1).decode(errors="ignore").lower() if ready else None

    try:
        yield read
    finally:
        termios.tcsetattr(descriptor, termios.TCSADRAIN, saved)


def _sleep(timeout: float) -> str | None:
    time.sleep(timeout)
    return None
