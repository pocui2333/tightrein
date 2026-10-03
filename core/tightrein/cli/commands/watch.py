"""watch：在终端中原地刷新当前运行的状态(tightrein/monitor)。只读数据库与工作区文件，不调用模型、不写记录；
按 q 退出、按 r 立即刷新；--once 只输出一帧后退出。"""

from __future__ import annotations

import argparse
import os
import select
import socket
import termios
import time
import tty
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, TextIO

from rich.console import Console
from rich.live import Live

from tightrein.cli import exit_codes
from tightrein.cli.commands.common import leaf
from tightrein.cli.exit_codes import UsageError
from tightrein.cli.output import Outcome
from tightrein.monitor import snapshot, view
from tightrein.orchestrator import pause, recovery
from tightrein.store.files.layout import UserLayout

QUIT = "q"


def _watch(invocation: Any) -> Outcome:
    args = invocation.args
    if args.json:
        raise UsageError("watch 是终端界面，不支持 --json；需要机器可读的状态时用 status --json")
    app = invocation.app
    interval = float(args.interval or app.config.get("loop.watchIntervalSeconds"))
    if interval <= 0:
        raise UsageError("--interval 须大于 0")
    console = Console(file=invocation.stdout)

    flag = UserLayout(app.home).pause_flag()

    def frame() -> Any:
        gone = recovery.interrupted(app.conn, alive=app.externals.alive, host=socket.gethostname())
        taken = snapshot.take(app.conn, app.layout, app.config.name, app.clock.now(), app.zone, gone,
                              pause.reason(app.conn, flag))
        return view.render(taken, console.width, app.zone, interval)

    if args.once:
        console.print(frame())
        return Outcome("watch", exit_codes.OK, [])
    try:
        with _keys(invocation.stdin) as read_key, Live(frame(), console=console, screen=True,
                                                         auto_refresh=False) as live:
            while read_key(interval) != QUIT:
                live.update(frame(), refresh=True)  # 到时或按 r 立即刷新
    except KeyboardInterrupt:
        pass  # Ctrl+C 与 q 一样是正常的退出方式
    return Outcome("watch", exit_codes.OK, [])


@contextmanager
def _keys(stdin: TextIO) -> Iterator[Callable[[float], str | None]]:
    """终端输入切到逐键读取(不回显、不等回车)，退出时恢复；返回的函数最多等 timeout 秒，返回按下的键，超时为空。
    标准输入不是终端(管道、测试)时只按间隔等待。"""
    if not stdin.isatty():
        yield lambda timeout: time.sleep(timeout)
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


def register(commands: Any, common: argparse.ArgumentParser) -> None:
    parser = leaf(commands, common, "watch", _watch, "在终端中实时查看运行状态、正在进行的修复与模型调用")
    parser.add_argument("--interval", type=float, help="刷新间隔(秒)；缺省取 loop.watchIntervalSeconds")
    parser.add_argument("--once", action="store_true", help="只输出一帧后退出")
