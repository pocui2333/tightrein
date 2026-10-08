"""启动前的端口检查与本工具遗留进程的识别。

端口在监听时，只有进程号与命令行都和本工具上次记录(services.json)的一致才算遗留进程，终止后继续；其他程序占用的
端口一律不动、不强行启动：不能误杀用户自己在跑的服务。检查一律用本工具启动的服务，不复用用户在跑的。
"""

from __future__ import annotations

import os
import signal
import socket
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from tightrein.protocol.process import Command, ProcessRunner

LOCALHOST = "127.0.0.1"
PS_TIMEOUT_S = 10.0


class PortProbe(Protocol):
    def in_use(self, port: int) -> bool: ...


class ProcessTable(Protocol):
    def command(self, pid: int) -> str | None: ...

    def terminate(self, pid: int) -> None: ...


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


class SocketProbe:
    def __init__(self, timeout_s: float) -> None:
        self.timeout_s = timeout_s

    def in_use(self, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
            connection.settimeout(self.timeout_s)
            return connection.connect_ex((LOCALHOST, port)) == 0


class PsTable:
    """经 protocol.process 调 ps 读命令行；终止整个进程组(服务由本工具以独立进程组启动)。"""

    def __init__(self, runner: ProcessRunner, environ: Mapping[str, str]) -> None:
        self.runner = runner
        self.environ = environ

    def command(self, pid: int) -> str | None:
        outcome = self.runner.run(Command(("ps", "-o", "command=", "-p", str(pid)), Path("/"), dict(self.environ),
                                          timeout_s=PS_TIMEOUT_S))
        return (outcome.stdout.strip() or None) if outcome.exit_code == 0 else None

    def terminate(self, pid: int) -> None:
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except ProcessLookupError:
            return


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
