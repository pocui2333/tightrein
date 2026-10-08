from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tightrein.implement.check.changes import collect
from tightrein.implement.check.runtime import session
from tightrein.implement.check.runtime.session import SessionDeps
from tightrein.implement.check.runtime.verdict import Result
from tightrein.onboard.setup import ModuleSetup, ModuleStatus
from tightrein.protocol.http import HttpResponse

SCRIPT = "scripts/local_run.py"


@dataclass
class Handle:
    pid: int

    def poll(self) -> int | None:
        return None

    def wait(self, timeout: float | None = None) -> int:
        return 0


@dataclass
class Spawner:
    started: list[str] = field(default_factory=list)

    def start(self, argv: Sequence[str], cwd: Path, env: Mapping[str, str], log: Path) -> Handle:
        self.started.append(argv[0])
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("listening\n")
        return Handle(2000 + len(self.started))

    def stop(self, handle: Any, timeout: float) -> None:
        return None


@dataclass
class Probe:
    busy: set[int] = field(default_factory=set)

    def in_use(self, port: int) -> bool:
        return port in self.busy


class Table:
    def command(self, pid: int) -> str | None:
        return None

    def terminate(self, pid: int) -> None:
        return None


def _plan(mode: str, migrations: list[str] | None = None) -> str:
    services = [{"name": "api", "role": "api", "cwd": ".", "argv": ["api"], "env": {}, "port": 18080, "after": None,
                 "readyPatterns": ["listening"], "failPatterns": [], "readyUrl": None}]
    if mode == "page":
        services.append({"name": "web", "role": "pages", "cwd": ".", "argv": ["web"], "env": {}, "port": 13000,
                         "after": "api", "readyPatterns": ["listening"], "failPatterns": [], "readyUrl": None})
    return json.dumps({"services": services, "unavailable": [], "migrationPaths": migrations or []})


def _world(new_world: Any, setup_with: Any) -> Any:
    module = ModuleSetup("implement.check.runtime", ModuleStatus.CUSTOM, None, SCRIPT, "guide.md", "项目自己启动",
                          None)
    world = new_world(setup=setup_with(implement__check__runtime=module))
    return world


def _deps(spawner: Spawner, busy: set[int] | None = None) -> SessionDeps:
    return SessionDeps(spawner, lambda timeout: Probe(busy or set()), lambda runtime: Table(),
                       lambda request: HttpResponse(200))


def _design(world: Any, **facts: Any) -> dict[str, Any]:
    return {"implement.design": world.handoff("implement.design", {"affectedEndpoints": [], "affectedPages": [],
                                                                    **facts})}


def test_disabled_or_untouched_endpoints_start_nothing(world: Any, new_world: Any, setup_with: Any,
                                                       tmp_path: Path) -> None:
    world.repo.write({"src/orders.py": "X = 1\n"})
    changes = collect(world.git(), world.repo.base)
    assert session.run(world.runtime, world.context(), changes, raw_dir=tmp_path).items == []
    enabled = _world(new_world, setup_with)
    enabled.repo.write({"src/orders.py": "X = 1\n"})
    outcome = session.run(enabled.runtime, enabled.context(latest=_design(enabled)),
                          collect(enabled.git(), enabled.repo.base), raw_dir=tmp_path, deps=_deps(Spawner()))
    assert outcome.items == [] and "不涉及接口与页面" in outcome.notes[0]


def test_migrations_wait_for_a_confirmation(new_world: Any, setup_with: Any, tmp_path: Path) -> None:
    world = _world(new_world, setup_with)
    world.respond((world.runtime.settings.project and "python",), {"stdout": ""})
    world.runner.replies.clear()
    world.respond((__import__("sys").executable,), {"stdout": _plan("api", ["migrations/"])})
    world.repo.write({"migrations/0002.sql": "ALTER TABLE orders ADD x int;\n"})
    spawner = Spawner()
    outcome = session.run(world.runtime, world.context(latest=_design(world, affectedEndpoints=["GET /api/orders"])),
                          collect(world.git(), world.repo.base), raw_dir=tmp_path, deps=_deps(spawner))
    assert outcome.pending and outcome.migration is not None and spawner.started == []
    assert outcome.migration.entries == ("ALTER TABLE orders ADD x int;",)


def test_occupied_page_ports_fall_back_to_api_mode(new_world: Any, setup_with: Any, tmp_path: Path,
                                                    monkeypatch: Any) -> None:
    import sys

    world = _world(new_world, setup_with)
    world.respond((sys.executable,), {"stdout": _plan("page")}, {"stdout": _plan("api")})
    world.repo.write({"src/orders.py": "X = 1\n"})
    shallow_calls: list[Any] = []

    def shallow(endpoints: Any, **options: Any) -> Any:
        shallow_calls.append(options["base_url"])
        return session.api.Item("api:shallow", "api", Result.PASSED)

    monkeypatch.setattr(session.api, "shallow", shallow)
    spawner = Spawner()
    design = _design(world, affectedEndpoints=["GET /api/orders"], affectedPages=["/orders"])
    outcome = session.run(world.runtime, world.context(latest=design), collect(world.git(), world.repo.base),
                          raw_dir=tmp_path, deps=_deps(spawner, {13000}))
    page = next(item for item in outcome.items if item.id == "page:/orders")
    assert page.result is Result.UNVERIFIED and "空出端口后重跑" in (page.reason or "")
    # 第一次 page 模式只启动了 api(web 端口被占)，退回 api 模式后再启动一次 api 做接口检查
    assert spawner.started == ["api", "api"] and shallow_calls == ["http://localhost:18080"]
    assert any(item.id == "api:shallow" and item.result is Result.PASSED for item in outcome.items)


def test_without_a_launch_plan_everything_is_unverified(new_world: Any, setup_with: Any, tmp_path: Path) -> None:
    import sys

    world = _world(new_world, setup_with)
    world.respond((sys.executable,), {"exit_code": 1, "stderr": "boom"})
    world.repo.write({"src/orders.py": "X = 1\n"})
    design = _design(world, affectedEndpoints=["GET /api/orders"], affectedPages=["/orders"])
    outcome = session.run(world.runtime, world.context(latest=design), collect(world.git(), world.repo.base),
                          raw_dir=tmp_path, deps=_deps(Spawner()))
    assert {item.id for item in outcome.items} == {"api:GET /api/orders", "page:/orders"}
    assert all(item.result is Result.UNVERIFIED and "启动脚本" in (item.reason or "") for item in outcome.items)
