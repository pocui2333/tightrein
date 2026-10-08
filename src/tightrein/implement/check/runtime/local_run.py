"""本机启动服务：同一 commit 的接口、页面、模糊测试共用一次启动。

- 启动什么由项目自己的启动脚本给出(setup.json 的 `implement.check.runtime.script`)：经标准输入收到
  `{"worktree", "mode", "ports"}`，输出启动计划(launch.schema.json：services、unavailable、migrationPaths)；
  tightrein 不含任何项目或技术栈的启动知识，没有脚本时什么都不启动，相关检查记未验证；
- 启动计划中的环境变量只能是非敏感值(名称或值像凭据即判为不合格)：连接配置由被测程序自己读取；
- 同一时间只一个本机服务：进入前取对象锁(心跳续期)，退出时释放；
- 启动前检查端口(ports.py)；按顺序启动，带 `after` 的等依赖就绪；每个服务独立进程组，环境先清除凭证，日志单独
  落盘，进程号与命令行写 services.json(下次据此识别遗留进程)；
- 就绪判断同时匹配成功与失败信号：只等成功信号时进程崩溃会静默等到超时；进程提前退出、出现失败信号或超时都判启动
  失败并附匹配到的失败行；成功信号出现后再请求一次 readyUrl 确认可达；
- 启动失败按 limits.md「测试环境问题就地重试 1 次」重试一次(旧设计不重试，以 limits.md 为准)；
- 退出时(含异常与中断)终止全部进程组，超时强杀，释放锁。
"""

from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol, Self

from tightrein.collect.common.source import SourceError, describe
from tightrein.implement.check.runtime import ports
from tightrein.implement.check.runtime.ports import PortProbe, ProcessTable
from tightrein.protocol import scripts
from tightrein.protocol.handoff import load_schema, schema_errors
from tightrein.protocol.http import HttpRequest, Transport
from tightrein.protocol.naming import parse_duration
from tightrein.protocol.raw import RawDir
from tightrein.protocol.runtime import Runtime
from tightrein.protocol.security import child_env, looks_like_credential, sensitive_name
from tightrein.settings.load import Settings
from tightrein.store.files.atomic import write_text
from tightrein.store.locks import FileLock, Lost

MODULE = "implement.check.runtime"
MODE_API = "api"
MODE_PAGE = "page"
LOCK_NAME = "local-run"
LOG_DIR = "services"
SCHEMA = Path(__file__).with_name("launch.schema.json")
PYTHON_SCRIPT = ".py"
START_ATTEMPTS = 2  # limits.md：测试环境问题(端口、启动失败)就地重试 1 次
NO_SCRIPT = "接入清单没有给出本机启动脚本(implement.check.runtime.script)"


@dataclass(frozen=True)
class LaunchService:
    name: str
    role: str | None  # api(接口检查的目标)、pages(页面检查的目标)；只做依赖的服务为 None
    cwd: str
    argv: tuple[str, ...]
    env: Mapping[str, str]
    port: int
    after: str | None
    ready_patterns: tuple[str, ...]
    fail_patterns: tuple[str, ...]
    ready_url: str | None


@dataclass(frozen=True)
class LaunchPlan:
    mode: str
    services: tuple[LaunchService, ...]
    unavailable: Mapping[str, str]  # 不在本机启动的服务 → 原因
    migration_paths: tuple[str, ...]


@dataclass(frozen=True)
class LocalRunSettings:
    ports: Mapping[str, int | None]
    ready_timeout_s: float
    stop_timeout_s: float
    ready_url_timeout_s: float
    poll_s: float
    port_probe_timeout_s: float
    script_timeout_s: float
    lock_stale_s: float
    heartbeat_s: float

    @classmethod
    def from_settings(cls, settings: Settings) -> LocalRunSettings:
        section = settings.section(MODULE)
        return cls(
            ports=dict(section["ports"]),
            ready_timeout_s=_seconds(section["readyTimeout"]),
            stop_timeout_s=_seconds(section["stopTimeout"]),
            ready_url_timeout_s=_seconds(section["readyUrlTimeout"]),
            poll_s=_seconds(section["poll"]),
            port_probe_timeout_s=_seconds(section["portProbeTimeout"]),
            script_timeout_s=_seconds(section["scriptTimeout"]),
            lock_stale_s=settings.duration("limits.lock.stale"),
            heartbeat_s=settings.duration("limits.lock.heartbeat"),
        )


class Handle(Protocol):
    pid: int

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...


class Spawner(Protocol):
    def start(self, argv: Sequence[str], cwd: Path, env: Mapping[str, str], log: Path) -> Handle: ...

    def stop(self, handle: Handle, timeout: float) -> None: ...


class SubprocessSpawner:
    """长驻服务的启停。protocol/process.py 只有「跑完再返回」的接口，后台服务只能在这里用 Popen。"""

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
    service: LaunchService
    log: Path
    ready: bool = False
    reason: str | None = None
    occupied: bool = False
    handle: Handle | None = None


class PlanInvalid(Exception):
    """启动脚本的输出不合格式，或环境变量里带了凭据。"""


def launch_plan(runtime: Runtime, *, worktree: Path, mode: str, raw: RawDir,
                settings: LocalRunSettings) -> LaunchPlan | str:
    """项目启动脚本给出的启动计划；取不到时返回原因(相关检查据此记未验证)。"""
    script = runtime.setup.module(MODULE).script
    if not script:
        return NO_SCRIPT
    command = ["{python}", script] if script.endswith(PYTHON_SCRIPT) else shlex.split(script)
    document = {"worktree": str(worktree), "mode": mode, "ports": dict(settings.ports)}
    try:
        output = scripts.run(name=LOCK_NAME, command=command, document=document, workspace=runtime.workspace.root,
                             runner=runtime.runner, environ=runtime.environ, secrets=runtime.secrets,
                             secret_names=runtime.setup.module(MODULE).secrets, redactor=runtime.redactor, raw=raw,
                             timeout_s=settings.script_timeout_s)
        return parse_plan(mode, json.loads(output))
    except SourceError as error:
        return f"启动脚本没有给出启动计划：{describe(error)}"
    except (ValueError, PlanInvalid) as error:
        return f"启动脚本的输出不合格：{error}"


def parse_plan(mode: str, data: Any) -> LaunchPlan:
    errors = schema_errors(data, _schema())
    if errors:
        raise PlanInvalid("；".join(errors))
    services = []
    for item in data["services"]:
        bad = [name for name, value in item["env"].items() if sensitive_name(name) or looks_like_credential(value)]
        if bad:
            raise PlanInvalid(f"服务 {item['name']} 的环境变量像凭据：{'、'.join(bad)}；连接配置由被测程序自己读取")
        services.append(LaunchService(item["name"], item.get("role"), item["cwd"], tuple(item["argv"]),
                                      dict(item["env"]), item["port"], item.get("after"),
                                      tuple(item["readyPatterns"]), tuple(item["failPatterns"]), item.get("readyUrl")))
    unavailable = {item["name"]: item["reason"] for item in data["unavailable"]}
    return LaunchPlan(mode, tuple(services), unavailable, tuple(data["migrationPaths"]))


@dataclass
class LocalService:
    """进入时启动并等待就绪，退出时收尾(含异常与中断)。"""

    plan: LaunchPlan
    worktree: Path
    report_dir: Path
    settings: LocalRunSettings
    lock: FileLock
    spawner: Spawner
    probe: PortProbe
    table: ProcessTable
    transport: Transport
    environ: Mapping[str, str]
    services_file: Path  # 固定位置：下次启动据此识别本工具遗留的进程
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    services: dict[str, ServiceState] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    _stop_beat: threading.Event = field(default_factory=threading.Event)

    def __enter__(self) -> Self:
        self.lock.acquire(wait=False)
        threading.Thread(target=self._heartbeat, daemon=True).start()
        try:
            self._start()
        except BaseException:
            self._stop()
            raise
        return self

    def __exit__(self, kind: type[BaseException] | None, error: BaseException | None,
                 trace: TracebackType | None) -> None:
        self._stop()

    @property
    def occupied(self) -> bool:
        return any(state.occupied for state in self.services.values())

    def base_url(self, role: str) -> str | None:
        state = next((item for item in self.services.values() if item.service.role == role), None)
        return f"http://localhost:{state.service.port}" if state is not None and state.ready else None

    def unverified(self, role: str) -> str | None:
        """需要该角色服务的检查执行前调用：不可用时返回原因，已就绪时为 None。"""
        state = next((item for item in self.services.values() if item.service.role == role), None)
        if state is None:
            reason = next(iter(self.plan.unavailable.values()), None)
            return f"不在本机启动：{reason}，部署后再确认" if reason else f"启动计划中没有 {role} 服务"
        if not state.ready:
            return f"{state.service.name} 未就绪：{state.reason}"
        if state.handle is not None and state.handle.poll() is not None:
            return f"{state.service.name} 已退出，日志 {state.log}"
        return None

    def _start(self) -> None:
        checked = ports.check([item.port for item in self.plan.services], self._previous_services(), self.probe,
                              self.table)
        for leftover in checked.leftovers:
            self.table.terminate(leftover.pid)
            self.notes.append(f"终止了本工具上次遗留的进程 {leftover.pid}(端口 {leftover.port})")
        env = child_env(self.environ)
        for service in self.plan.services:
            state = ServiceState(service, self.report_dir / LOG_DIR / f"{service.name}.log")
            self.services[service.name] = state
            dependency = self.services.get(service.after) if service.after else None
            if service.port in checked.occupied:
                state.occupied = True
                state.reason = f"端口 {service.port} 被其他程序占用，空出端口后重跑"
            elif service.after is not None and (dependency is None or not dependency.ready):
                state.reason = f"所依赖的服务 {service.after} 没有就绪"
            else:
                self._launch(state, {**env, **service.env})
        self._write_services()

    def _launch(self, state: ServiceState, env: Mapping[str, str]) -> None:
        for attempt in range(1, START_ATTEMPTS + 1):
            state.handle = self.spawner.start(state.service.argv, self.worktree / state.service.cwd, env, state.log)
            self._write_services()
            state.reason = self._wait(state)
            state.ready = state.reason is None
            if state.ready:
                return
            self.spawner.stop(state.handle, self.settings.stop_timeout_s)
            if attempt < START_ATTEMPTS:
                self.notes.append(f"{state.service.name} 第 {attempt} 次启动失败({state.reason})，重试一次")

    def _wait(self, state: ServiceState) -> str | None:
        service = state.service
        ready = [re.compile(pattern) for pattern in service.ready_patterns]
        failed = [re.compile(pattern) for pattern in service.fail_patterns]
        deadline = self.monotonic() + self.settings.ready_timeout_s
        handle = state.handle
        assert handle is not None
        while True:
            lines = state.log.read_text(encoding="utf-8", errors="replace").splitlines() if state.log.is_file() else []
            failure = next((line for line in lines if any(pattern.search(line) for pattern in failed)), None)
            if failure is not None:
                return f"启动失败：{failure.strip()}"
            if any(pattern.search(line) for line in lines for pattern in ready):
                break
            if handle.poll() is not None:
                return f"进程已退出(退出码 {handle.poll()})，日志 {state.log}"
            if self.monotonic() >= deadline:
                return f"超过 {self.settings.ready_timeout_s:g} 秒没有就绪，日志 {state.log}"
            self.sleep(self.settings.poll_s)
        if service.ready_url:
            response = self.transport(HttpRequest("GET", service.ready_url, self.settings.ready_url_timeout_s))
            if response.status is None:
                return f"就绪信号已出现，但 {service.ready_url} 不可达：{response.error}"
        return None

    def _previous_services(self) -> list[Mapping[str, Any]]:
        if not self.services_file.is_file():
            return []
        return list(json.loads(self.services_file.read_text(encoding="utf-8")))

    def _write_services(self) -> None:
        records = [{"name": state.service.name, "pid": state.handle.pid, "argv": list(state.service.argv),
                    "port": state.service.port}
                   for state in self.services.values() if state.handle is not None]
        write_text(self.services_file, json.dumps(records, ensure_ascii=False, indent=2) + "\n")

    def _heartbeat(self) -> None:
        while not self._stop_beat.wait(self.settings.heartbeat_s):
            try:
                self.lock.beat()
            except Lost:  # 锁已被接管：不再续期，服务照常在退出时停止
                return

    def _stop(self) -> None:
        self._stop_beat.set()
        try:
            for state in reversed(list(self.services.values())):
                if state.handle is not None:
                    self.spawner.stop(state.handle, self.settings.stop_timeout_s)
        finally:
            self.lock.release()


def _schema() -> dict[str, Any]:
    return load_schema(SCHEMA)


def _seconds(value: str) -> float:
    return parse_duration(value)
