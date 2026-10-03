"""端口检查与本工具遗留进程的识别(architecture/07 11.2)。

启动前检查启动计划中每个服务的端口是否在监听。被占用时查看占用进程是否为本工具上一次遗留的进程：上一次的
services.json 中记录了进程编号、命令行与端口，进程编号与命令行都一致才算遗留，终止它后继续；否则不强行启动，
不终止任何不属于本工具的进程。
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

LOCALHOST = "127.0.0.1"


class PortProbe(Protocol):
    def in_use(self, port: int) -> bool: ...


class ProcessTable(Protocol):
    def command(self, pid: int) -> str | None: ...

    def terminate(self, pid: int) -> None: ...


class SocketProbe:
    def __init__(self, timeout_seconds: float) -> None:
        self.timeout_seconds = timeout_seconds

    def in_use(self, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
            connection.settimeout(self.timeout_seconds)
            return connection.connect_ex((LOCALHOST, port)) == 0


class PsTable:
    def command(self, pid: int) -> str | None:
        result = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, check=False)
        return result.stdout.strip() or None if result.returncode == 0 else None

    def terminate(self, pid: int) -> None:
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except ProcessLookupError:
            return


@dataclass(frozen=True)
class Leftover:
    pid: int
    command: str
    port: int


@dataclass(frozen=True)
class PortCheck:
    free: tuple[int, ...] = ()
    leftovers: tuple[Leftover, ...] = ()
    occupied: tuple[int, ...] = ()


def check(ports: Sequence[int], previous: Sequence[Mapping[str, Any]], probe: PortProbe,
          table: ProcessTable) -> PortCheck:
    free: list[int] = []
    leftovers: list[Leftover] = []
    occupied: list[int] = []
    for port in ports:
        if not probe.in_use(port):
            free.append(port)
            continue
        recorded = next((item for item in previous if item.get("port") == port and item.get("pid")), None)
        command = table.command(recorded["pid"]) if recorded is not None else None
        if recorded is not None and command is not None and command == " ".join(recorded["argv"]):
            leftovers.append(Leftover(recorded["pid"], command, port))
        else:
            occupied.append(port)
    return PortCheck(tuple(free), tuple(leftovers), tuple(occupied))

