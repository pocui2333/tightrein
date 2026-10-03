import json

import pytest
from fix_world import make_fix_world
from verify_world import PORTS, Client, Clock, Probe, Spawner, Table, service

from tightrein.domain.enums import OperationKind
from tightrein.pipeline.verify.steps import migration
from tightrein.pipeline.verify.steps.local_run import LocalService
from tightrein.sources.common.http import HttpResponse
from tightrein.store import locks


def local(world, tmp_path, client, spawner, probe=None, table=None, previous=None, transport=None):
    return LocalService(world.worktree, "page", tmp_path / "report", client, world.config, world.conn, world.clock,
                        spawner, probe or Probe(), table or Table(), transport or (lambda request: HttpResponse(200)),
                        previous=previous, sleep=lambda seconds: None, monotonic=Clock())


@pytest.fixture
def world(tmp_path):
    return make_fix_world(tmp_path, localRun=PORTS)


def test_without_an_extension_nothing_starts(world, tmp_path):
    with local(world, tmp_path, None, Spawner({})) as running:
        assert running.unverified(["backend"]) == "未提供 local-run 扩展"
    assert locks.get(world.conn, "local-run") is None


def test_services_start_in_order_and_become_ready(world, tmp_path):
    client = Client([service("backend", 5102, ready_url="http://localhost:5102/health"),
                     service("frontend", 5103, after="backend")], [{"name": "compute", "reason": "外部节点"}])
    spawner = Spawner({"backend": "boot\nlistening on 5102\n", "frontend": "listening\n"})
    with local(world, tmp_path, client, spawner) as running:
        assert locks.get(world.conn, "local-run") is not None
        assert spawner.started == [("backend", "test"), ("frontend", "test")]
        assert running.unverified(["backend", "frontend"]) is None
        assert running.unverified(["compute"]) == "compute 不在本机启动：外部节点，部署后确认时再验证"
        assert running.base_url("backend") == "http://localhost:5102"
        recorded = json.loads((tmp_path / "report" / "services.json").read_text(encoding="utf-8"))
        assert [item["pid"] for item in recorded] == [101, 102]
    assert spawner.stopped == [102, 101] and locks.get(world.conn, "local-run") is None
    assert client.calls == [("page", {"backend": 5102, "frontend": 5103})]


def test_failures_timeouts_and_dependencies(world, tmp_path):
    client = Client([service("backend", 5102), service("frontend", 5103, after="backend")])
    spawner = Spawner({"backend": "Unhandled exception: config\n"})
    with local(world, tmp_path, client, spawner) as running:
        assert running.unverified(["backend"]) == "backend 未就绪：启动失败：Unhandled exception: config"
        assert running.unverified(["frontend"]) == "frontend 未就绪：所依赖的服务 backend 没有就绪"
    with local(world, tmp_path / "slow", Client([service("backend", 5102)]), Spawner({"backend": "boot\n"})) as running:
        assert running.unverified(["backend"]) == "backend 未就绪：超过 5 秒没有就绪"
    exited = Spawner({"backend": "boot\n"}, exits={"backend"})
    with local(world, tmp_path / "exit", Client([service("backend", 5102)]), exited) as running:
        assert "进程已退出" in running.unverified(["backend"])


def test_ports_held_by_leftovers_are_freed_and_others_are_left_alone(world, tmp_path):
    previous = tmp_path / "old-services.json"
    previous.write_text(json.dumps([{"name": "backend", "pid": 77, "argv": ["run-backend"], "port": 5102}]))
    table = Table({77: "run-backend", 88: "someone-else"})
    client = Client([service("backend", 5102), service("frontend", 5103)])
    spawner = Spawner({"backend": "listening\n", "frontend": "listening\n"})
    with local(world, tmp_path, client, spawner, Probe({5102, 5103}), table, previous) as running:
        assert table.terminated == [77] and "终止了本工具上一次遗留的进程 77(端口 5102)" in running.notes
        assert running.unverified(["frontend"]) == "frontend 未就绪：端口 5103 被其他程序占用，空出端口后重跑"
        assert [name for name, _ in spawner.started] == ["backend"]


def test_errors_inside_still_stop_the_services(world, tmp_path):
    spawner = Spawner({"backend": "listening\n"})
    with pytest.raises(RuntimeError):
        with local(world, tmp_path, Client([service("backend", 5102)]), spawner):
            raise RuntimeError("中断")
    assert spawner.stopped == [101] and locks.get(world.conn, "local-run") is None


def test_changed_migrations_need_a_confirmation(world, tmp_path):
    files = migration.changed(["db/migrations/0042.sql", "src/app.src"], ["db/migrations/*"])
    assert files == ["db/migrations/0042.sql"]
    assert migration.entries({"db/migrations/0042.sql": ["ALTER TABLE orders ADD company", " "]}, files) == [
        "ALTER TABLE orders ADD company"]
    sha = migration.files_hash(world.worktree, files)
    operation = migration.request(world.conn, world.clock, repo="/repo", issue_id=world.issue_id, files=files,
                                  added=["ALTER TABLE orders ADD company"],
                                  migration={"entries": ["0042"], "reversible": True, "revertMethod": "执行 0042 的回滚"},
                                  sha=sha)
    assert operation.kind is OperationKind.LOCAL_MIGRATION and "可以撤销：执行 0042 的回滚" in operation.description.text
    assert not migration.confirmed(world.conn, world.issue_id, sha)
    from tightrein.vcs.executor import OperationRunner

    runner = OperationRunner(world.conn, None, None, None, world.layout, "R-1")
    runner.confirm(operation.id, confirmed_by="cty", clock=world.clock)
    runner.execute(operation.id, clock=world.clock)
    assert migration.confirmed(world.conn, world.issue_id, sha)
    assert not migration.confirmed(world.conn, world.issue_id, "0" * 64)
