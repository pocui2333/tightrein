"""本机启动(architecture/07 第 11 章)：LocalService 在进入时启动并等待就绪，退出时收尾(包括异常与中断)。

- 同一时间只允许一个本机服务：进入前获取对象锁 local-run(时限 thresholds.verify.localRunLockMinutes)，退出时释放。
- 启动什么由 local-run 扩展给出启动计划(services、unavailable、migrationPaths)；没有扩展时不启动任何服务，需要本机服务
  的检查一律记为未验证。核心不含任何项目或技术栈的启动知识。
- 启动前检查端口：本工具上一次遗留的进程先终止；被他人占用的端口上的服务不启动。
- 按 services 的顺序启动，带 after 的等所依赖的服务就绪；在 worktree 的 cwd 中以 argv 启动、不经 shell，环境为清除
  凭证后的进程环境加 services[].env；每个服务在独立的进程组中，输出写入报告目录的 services/<名>.log，进程编号、命令
  与端口写入 services.json。
- 就绪判断：逐行读取日志，同时匹配成功与失败信号，失败信号出现或进程退出即判为启动失败；成功后请求 readyUrl 确认可达；
  超过 localRun.readyTimeoutSeconds 视为失败。启动失败不重试。
- 退出时向每个进程组发终止信号，等待 localRun.stopTimeoutSeconds 后强制结束。
"""

from __future__ import annotations

import json
import os
import re
import signal
import sqlite3
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol

from tightrein.config.project import MissingSetting, ProjectConfig
from tightrein.domain.clock import Clock
from tightrein.extensions.client import local_run_ports
from tightrein.extensions.result import PointResult
from tightrein.guards.credentials import build_env
from tightrein.pipeline.verify.steps import ports as port_step
from tightrein.pipeline.verify.steps.ports import PortProbe, ProcessTable
from tightrein.sources.common.http import HttpRequest, Transport
from tightrein.store import locks
from tightrein.store.files import atomic

SERVICES_FILE = "services.json"
LOG_DIR = "services"
MILLISECONDS_PER_SECOND = 1000
NO_EXTENSION = "未提供 local-run 扩展"


class Handle(Protocol):
    pid: int

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...


class Spawner(Protocol):
    def start(self, argv: Sequence[str], cwd: Path, env: Mapping[str, str], log: Path) -> Handle: ...

    def stop(self, handle: Handle, timeout: float) -> None: ...


class SubprocessSpawner:
    def start(self, argv: Sequence[str], cwd: Path, env: Mapping[str, str], log: Path) -> Handle:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("wb") as output:
            return subprocess.Popen(list(argv), cwd=cwd, env=dict(env), stdin=subprocess.DEVNULL, stdout=output,
                                    stderr=subprocess.STDOUT, start_new_session=True)

    def stop(self, handle: Handle, timeout: float) -> None:
        if handle.poll() is not None:
            return
        try:
            os.killpg(handle.pid, signal.SIGTERM)
            handle.wait(timeout)
        except subprocess.TimeoutExpired:
            os.killpg(handle.pid, signal.SIGKILL)
        except ProcessLookupError:
            return


@dataclass
class ServiceState:
    name: str
    port: int
    log: Path
    ready: bool = False
    reason: str | None = None
    occupied: bool = False
    pid: int | None = None
    argv: tuple[str, ...] = ()
    handle: Any = None


@dataclass
class LocalRunSettings:
    ready_timeout: float
    stop_timeout: float
    url_timeout: float
    poll: float
    lock_minutes: int

    @classmethod
    def from_config(cls, config: ProjectConfig) -> LocalRunSettings:
        return cls(float(config.get("localRun.readyTimeoutSeconds")), float(config.get("localRun.stopTimeoutSeconds")),
                   float(config.get("localRun.readyUrlTimeoutSeconds")),
                   config.get("localRun.pollIntervalMs") / MILLISECONDS_PER_SECOND,
                   config.whole_threshold("verify.localRunLockMinutes"))


class LocalRunClient(Protocol):
    def local_run(self, worktree: Path, mode: str, ports: Mapping[str, int | None]) -> PointResult: ...


@dataclass
class LocalService:
    worktree: Path
    mode: str
    report_dir: Path
    client: LocalRunClient | None
    config: ProjectConfig
    conn: sqlite3.Connection
    clock: Clock
    spawner: Spawner
    probe: PortProbe
    table: ProcessTable
    transport: Transport
    environ: Mapping[str, str] = field(default_factory=dict)
    previous: Path | None = None
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    services: dict[str, ServiceState] = field(default_factory=dict)
    unavailable: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    plan: Mapping[str, Any] | None = None
    reason: str | None = None

    def launch_plan(self) -> Mapping[str, Any] | None:
        """取得启动计划并缓存；没有扩展、端口没有配置或扩展失败时为空，reason 写明原因。"""
        if self.plan is not None or self.reason is not None:
            return self.plan
        if self.client is None:
            self.reason = NO_EXTENSION
            return None
        try:
            ports = local_run_ports(self.config, self.mode)
        except MissingSetting as error:
            self.reason = f"没有配置本机端口 {error.key}"
            return None
        result = self.client.local_run(self.worktree, self.mode, ports)
        if result.output is None:
            self.reason = result.failure.describe() if result.failure is not None else "；".join(result.notes) \
                or NO_EXTENSION
            return None
        self.plan = result.output
        self.unavailable = {item["name"]: item["reason"] for item in self.plan["unavailable"]}
        return self.plan

    @property
    def migration_paths(self) -> tuple[str, ...]:
        plan = self.launch_plan()
        return tuple(plan["migrationPaths"]) if plan is not None else ()

    def __enter__(self) -> LocalService:
        settings = LocalRunSettings.from_config(self.config)
        plan = self.launch_plan()
        locks.acquire(self.conn, locks.LOCAL_RUN, self.clock, timedelta(minutes=settings.lock_minutes))
        try:
            if plan is not None:
                self._start(plan["services"], settings)
        except BaseException:
            self._stop(settings)
            raise
        return self

    def __exit__(self, kind: type[BaseException] | None, error: BaseException | None,
                 trace: TracebackType | None) -> None:
        self._stop(LocalRunSettings.from_config(self.config))

    def _previous_services(self) -> list[Mapping[str, Any]]:
        if self.previous is None or not self.previous.is_file():
            return []
        return json.loads(self.previous.read_text(encoding="utf-8"))

    def _start(self, services: Sequence[Mapping[str, Any]], settings: LocalRunSettings) -> None:
        checked = port_step.check([item["port"] for item in services], self._previous_services(), self.probe,
                                  self.table)
        for leftover in checked.leftovers:
            self.table.terminate(leftover.pid)
            self.notes.append(f"终止了本工具上一次遗留的进程 {leftover.pid}(端口 {leftover.port})")
        env = build_env(self.environ).env
        for item in services:
            state = ServiceState(item["name"], item["port"], self.report_dir / LOG_DIR / f"{item['name']}.log",
                                 argv=tuple(item["argv"]))
            self.services[item["name"]] = state
            after = item.get("after")
            if item["port"] in checked.occupied:
                state.occupied = True
                state.reason = f"端口 {item['port']} 被其他程序占用，空出端口后重跑"
            elif after is not None and not (after in self.services and self.services[after].ready):
                state.reason = f"所依赖的服务 {after} 没有就绪"
            else:
                state.handle = self.spawner.start(item["argv"], self.worktree / item["cwd"], {**env, **item["env"]},
                                                  state.log)
                state.pid = state.handle.pid
                self._write_services()
                self._wait(state, item, settings)
        self._write_services()

    def _wait(self, state: ServiceState, item: Mapping[str, Any], settings: LocalRunSettings) -> None:
        ready = [re.compile(pattern) for pattern in item["readyPatterns"]]
        failed = [re.compile(pattern) for pattern in item["failPatterns"]]
        deadline = self.monotonic() + settings.ready_timeout
        while True:
            lines = state.log.read_text(encoding="utf-8", errors="replace").splitlines() if state.log.is_file() else []
            failure = next((line for line in lines if any(pattern.search(line) for pattern in failed)), None)
            if failure is not None:
                state.reason = f"启动失败：{failure.strip()}"
                return
            if any(pattern.search(line) for line in lines for pattern in ready):
                break
            if state.handle.poll() is not None:
                state.reason = f"进程已退出(退出码 {state.handle.poll()})，日志 {state.log}"
                return
            if self.monotonic() >= deadline:
                state.reason = f"超过 {settings.ready_timeout:g} 秒没有就绪"
                return
            self.sleep(settings.poll)
        url = item.get("readyUrl")
        if url:
            response = self.transport(HttpRequest("GET", url, timeout_seconds=settings.url_timeout))
            if response.status is None:
                state.reason = f"就绪信号已出现，但 {url} 不可达：{response.error}"
                return
        state.ready = True

    def _write_services(self) -> None:
        atomic.write_text(self.report_dir / SERVICES_FILE, json.dumps(
            [{"name": item.name, "pid": item.pid, "argv": list(item.argv), "port": item.port}
             for item in self.services.values() if item.pid is not None], ensure_ascii=False, indent=2) + "\n")

    def _stop(self, settings: LocalRunSettings) -> None:
        try:
            for state in reversed(list(self.services.values())):
                if state.handle is not None:
                    self.spawner.stop(state.handle, settings.stop_timeout)
        finally:
            locks.release(self.conn, locks.LOCAL_RUN)

    def base_url(self, name: str) -> str | None:
        state = self.services.get(name)
        return f"http://localhost:{state.port}" if state is not None and state.ready else None

    def unverified(self, requires: Sequence[str]) -> str | None:
        """执行需要这些服务的检查前调用：有服务不可用时返回原因，都已就绪时为空。"""
        if self.plan is None:
            return self.reason or NO_EXTENSION
        for name in requires:
            if name in self.unavailable:
                return f"{name} 不在本机启动：{self.unavailable[name]}，部署后确认时再验证"
            state = self.services.get(name)
            if state is None:
                return f"启动计划中没有服务 {name}"
            if not state.ready:
                return f"{name} 未就绪：{state.reason}"
            if state.handle is not None and state.handle.poll() is not None:
                return f"{name} 已退出"
        return None

    @property
    def occupied(self) -> bool:
        return any(state.occupied for state in self.services.values())
