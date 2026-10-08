from __future__ import annotations

import json
import os
import socket
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tightrein.implement.check.runtime import local_run, ports
from tightrein.implement.check.runtime.local_run import LaunchPlan, LocalRunSettings, LocalService, PlanInvalid
from tightrein.protocol.http import HttpRequest, HttpResponse
from tightrein.protocol.naming import FixedClock, format_iso
from tightrein.store.locks import Busy, FileLock

NOW = datetime(2026, 10, 8, 3, 0, tzinfo=UTC)
SETTINGS = LocalRunSettings(ports={"api": 18080}, ready_timeout_s=5, stop_timeout_s=1, ready_url_timeout_s=1,
                            poll_s=1, port_probe_timeout_s=1, script_timeout_s=10, lock_stale_s=90,
                            heartbeat_s=3600)


def service(name: str, port: int, **extra: Any) -> dict[str, Any]:
    return {"name": name, "role": extra.pop("role", None), "cwd": ".", "argv": [name, "--port", str(port)], "env": {},
            "port": port, "after": None, "readyPatterns": ["listening"], "failPatterns": ["Traceback"],
            "readyUrl": None, **extra}


def plan(*services: dict[str, Any], migrations: Sequence[str] = ()) -> LaunchPlan:
    return local_run.parse_plan("api", {"services": list(services), "unavailable": [],
                                        "migrationPaths": list(migrations)})


@dataclass
class Handle:
    pid: int
    code: int | None = None

    def poll(self) -> int | None:
        return self.code

    def wait(self, timeout: float | None = None) -> int:
        return self.code or 0


@dataclass
class Spawner:
    """按服务名写出日志：outputs[名] 为每次启动依次写的日志内容，exits[名] 为该次进程是否已退出。"""

    outputs: dict[str, list[str]]
    exits: dict[str, list[int | None]] = field(default_factory=dict)
    started: list[str] = field(default_factory=list)
    stopped: list[int] = field(default_factory=list)

    def start(self, argv: Sequence[str], cwd: Path, env: Mapping[str, str], log: Path) -> Handle:
        name = argv[0]
        attempt = self.started.count(name)
        self.started.append(name)
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(self.outputs[name][min(attempt, len(self.outputs[name]) - 1)])
        codes = self.exits.get(name, [None])
        return Handle(1000 + len(self.started), codes[min(attempt, len(codes) - 1)])

    def stop(self, handle: Any, timeout: float) -> None:
        self.stopped.append(handle.pid)


@dataclass
class Probe:
    busy: set[int] = field(default_factory=set)

    def in_use(self, port: int) -> bool:
        return port in self.busy


@dataclass
class Table:
    commands: dict[int, str] = field(default_factory=dict)
    terminated: list[int] = field(default_factory=list)

    def command(self, pid: int) -> str | None:
        return self.commands.get(pid)

    def terminate(self, pid: int) -> None:
        self.terminated.append(pid)


class Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def now(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


def make(tmp_path: Path, launch: LaunchPlan, spawner: Spawner, *, probe: Probe | None = None,
         table: Table | None = None, transport: Any = None) -> LocalService:
    clock = Clock()
    lock = FileLock(tmp_path / "locks" / "local-run.lock", FixedClock(NOW), stale_s=90)
    return LocalService(plan=launch, worktree=tmp_path, report_dir=tmp_path / "raw", settings=SETTINGS, lock=lock,
                        spawner=spawner, probe=probe or Probe(), table=table or Table(),
                        transport=transport or (lambda request: HttpResponse(200)), environ={"PATH": "/usr/bin"},
                        services_file=tmp_path / "data" / "services.json", sleep=clock.sleep, monotonic=clock.now)


def test_services_start_in_order_become_ready_and_are_stopped(tmp_path: Path) -> None:
    spawner = Spawner({"api": ["listening on 18080\n"], "web": ["listening\n"]})
    launch = plan(service("api", 18080, role="api", readyUrl="http://localhost:18080/health"),
                  service("web", 13000, role="pages", after="api"))
    requests: list[HttpRequest] = []
    with make(tmp_path, launch, spawner, transport=lambda request: requests.append(request) or HttpResponse(200)) as run:
        assert run.base_url("api") == "http://localhost:18080" and run.base_url("pages") == "http://localhost:13000"
        assert run.unverified("api") is None
        recorded = json.loads((tmp_path / "data" / "services.json").read_text())
        assert [item["name"] for item in recorded] == ["api", "web"] and recorded[0]["argv"] == ["api", "--port",
                                                                                                "18080"]
    assert spawner.started == ["api", "web"] and spawner.stopped == [1002, 1001]
    assert [request.url for request in requests] == ["http://localhost:18080/health"]
    # 锁在退出时释放
    assert FileLock(tmp_path / "locks" / "local-run.lock", FixedClock(NOW), stale_s=90).holder() is None


def test_an_error_during_the_checks_still_stops_every_service(tmp_path: Path) -> None:
    spawner = Spawner({"api": ["listening on 18080\n"], "web": ["listening\n"]})
    launch = plan(service("api", 18080, role="api"), service("web", 13000, role="pages", after="api"))
    with pytest.raises(RuntimeError, match="页面检查出错"), make(tmp_path, launch, spawner):
        raise RuntimeError("页面检查出错")
    assert spawner.stopped == [1002, 1001]
    assert FileLock(tmp_path / "locks" / "local-run.lock", FixedClock(NOW), stale_s=90).holder() is None
    with pytest.raises(KeyboardInterrupt), make(tmp_path, launch, spawner):
        raise KeyboardInterrupt
    assert spawner.stopped == [1002, 1001, 1004, 1003]


def test_failures_timeouts_and_dependencies(tmp_path: Path) -> None:
    # 失败信号先于成功信号：附上匹配到的失败行；启动失败重试一次仍失败；依赖它的服务不启动
    spawner = Spawner({"api": ["Traceback: boom\nlistening\n"], "web": ["listening\n"]})
    launch = plan(service("api", 18080, role="api"), service("web", 13000, role="pages", after="api"))
    with make(tmp_path, launch, spawner) as run:
        assert "Traceback: boom" in (run.unverified("api") or "")
        assert "所依赖的服务 api 没有就绪" in (run.unverified("pages") or "")
    assert spawner.started == ["api", "api"] and any("重试一次" in note for note in run.notes)
    # 进程提前退出、超时
    exited = Spawner({"api": ["starting\n"]}, exits={"api": [3]})
    with make(tmp_path / "b", plan(service("api", 18080, role="api")), exited) as run:
        assert "进程已退出(退出码 3)" in (run.unverified("api") or "")
    silent = Spawner({"api": ["starting\n"]})
    with make(tmp_path / "c", plan(service("api", 18080, role="api")), silent) as run:
        assert "没有就绪" in (run.unverified("api") or "")


def test_a_failed_start_is_retried_once(tmp_path: Path) -> None:
    spawner = Spawner({"api": ["Traceback: port\n", "listening\n"]})
    with make(tmp_path, plan(service("api", 18080, role="api")), spawner) as run:
        assert run.unverified("api") is None and spawner.started == ["api", "api"]


def test_ready_url_must_answer(tmp_path: Path) -> None:
    spawner = Spawner({"api": ["listening\n"]})
    launch = plan(service("api", 18080, role="api", readyUrl="http://localhost:18080/health"))
    with make(tmp_path, launch, spawner, transport=lambda request: HttpResponse(None, error="refused")) as run:
        assert "不可达" in (run.unverified("api") or "")


def test_ports_held_by_leftovers_are_freed_and_others_are_left_alone(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir(parents=True)
    (tmp_path / "data" / "services.json").write_text(json.dumps([
        {"name": "api", "pid": 41, "argv": ["api", "--port", "18080"], "port": 18080},
        {"name": "web", "pid": 42, "argv": ["web", "--port", "13000"], "port": 13000}]))
    table = Table({41: "api --port 18080", 42: "someone else's server"})
    spawner = Spawner({"api": ["listening\n"], "web": ["listening\n"]})
    launch = plan(service("api", 18080, role="api"), service("web", 13000, role="pages"))
    with make(tmp_path, launch, spawner, probe=Probe({18080, 13000}), table=table) as run:
        assert table.terminated == [41] and run.occupied
        assert "被其他程序占用" in (run.unverified("pages") or "") and run.unverified("api") is None
    assert spawner.started == ["api"]
    found = ports.check([1, 2], [], Probe({2}), Table())
    assert found.free == (1,) and found.occupied == (2,)


def test_another_local_service_holds_the_lock(tmp_path: Path) -> None:
    # 另一个还活着的进程(这里用父进程)持有锁且心跳新鲜
    lock = tmp_path / "locks" / "local-run.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text(json.dumps({"pid": os.getppid(), "host": socket.gethostname(), "acquiredAt": format_iso(NOW),
                                "heartbeatAt": format_iso(NOW)}))
    spawner = Spawner({"api": ["listening\n"]})
    with pytest.raises(Busy), make(tmp_path, plan(service("api", 18080, role="api")), spawner):
        pass
    assert spawner.started == []


def test_launch_plans_are_validated_and_must_not_carry_credentials() -> None:
    with pytest.raises(PlanInvalid):
        local_run.parse_plan("api", {"services": [], "unavailable": []})
    with pytest.raises(PlanInvalid, match="像凭据"):
        plan(service("api", 18080, env={"DB_PASSWORD": "x"}))
    launch = local_run.parse_plan("page", {"services": [service("api", 1, role="api", env={"PORT": "1"})],
                                           "unavailable": [{"name": "queue", "reason": "只在测试环境"}],
                                           "migrationPaths": ["migrations/"]})
    assert launch.unavailable == {"queue": "只在测试环境"} and launch.migration_paths == ("migrations/",)


def test_without_a_script_nothing_starts(world: Any, tmp_path: Path) -> None:
    from tightrein.protocol.raw import RawDir

    reason = local_run.launch_plan(world.runtime, worktree=tmp_path, mode="api", raw=RawDir(tmp_path / "raw"),
                                   settings=SETTINGS)
    assert reason == local_run.NO_SCRIPT
