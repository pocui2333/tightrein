"""verify 测试共用：local-run 扩展、进程、端口与进程表的替身，以及待验证的修复。"""

from fix_world import BASE, SERVICE, SERVICE_PATH, make_fix_world
from pipeline_world import make_signal

from tightrein.domain.enums import ExtensionLayer, ExtensionPoint, HandoffStatus, IssueEvent, RunStage
from tightrein.extensions.result import PointResult
from tightrein.pipeline.common import stage_runs
from tightrein.pipeline.fix.steps import repro
from tightrein.pipeline.verify.steps.local_run import LocalService
from tightrein.sources.common.http import HttpResponse

PORTS = {"ports": {"api": 5101, "backendForPages": 5102, "frontend": 5103}, "readyTimeoutSeconds": 5}
REQUEST = {"method": "GET", "path": "/api/Order/42", "pathTemplate": "/api/Order/{id}", "query": {}}
FIXED = SERVICE.replace("line 12\n", "line 12 filtered by company\n")


def service(name, port, after=None, ready_url=None):
    return {"name": name, "argv": [f"run-{name}"], "cwd": ".", "env": {"MODE": "test"}, "port": port,
            "readyUrl": ready_url, "readyPatterns": ["listening"], "failPatterns": ["Unhandled"], "after": after}


class Client:
    def __init__(self, services=(), unavailable=(), migration_paths=()):
        self.plan = {"services": list(services), "unavailable": list(unavailable),
                     "migrationPaths": list(migration_paths)}
        self.calls = []

    def local_run(self, worktree, mode, ports):
        self.calls.append((mode, ports))
        return PointResult(ExtensionPoint.LOCAL_RUN, ExtensionLayer.PROJECT, output=self.plan)


class Handle:
    def __init__(self, pid, exit_code=None):
        self.pid = pid
        self.exit_code = exit_code

    def poll(self):
        return self.exit_code

    def wait(self, timeout=None):
        return 0


class Spawner:
    """按服务名写出日志内容；exits 中的服务启动后立即退出。"""

    def __init__(self, logs, exits=()):
        self.logs = logs
        self.exits = set(exits)
        self.started = []
        self.stopped = []

    def start(self, argv, cwd, env, log):
        name = argv[0].removeprefix("run-")
        self.started.append((name, env.get("MODE")))
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(self.logs.get(name, ""), encoding="utf-8")
        return Handle(100 + len(self.started), 1 if name in self.exits else None)

    def stop(self, handle, timeout):
        self.stopped.append(handle.pid)


class Probe:
    def __init__(self, busy=()):
        self.busy = set(busy)

    def in_use(self, port):
        return port in self.busy


class Table:
    def __init__(self, commands=None):
        self.commands = dict(commands or {})
        self.terminated = []

    def command(self, pid):
        return self.commands.get(pid)

    def terminate(self, pid):
        self.terminated.append(pid)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        self.now += 1
        return self.now



def local_factory(world, client, spawner, probe=None, table=None):
    def build(worktree, mode, report_dir):
        return LocalService(worktree, mode, report_dir, client, world.config, world.conn, world.clock, spawner,
                            probe or Probe(), table or Table(), lambda request: HttpResponse(200),
                            sleep=lambda seconds: None, monotonic=Clock())

    return build


def backend(port=5101):
    return service("backend", port)


def fixed_world(tmp_path, **config):
    """修复已完成、待验证：复现检查已写入，worktree 中有改动，修复交接文档为 ok。"""
    signal = make_signal(1, context={"request": REQUEST, "response": {"status": 500}})
    world = make_fix_world(tmp_path, signal=signal, localRun=PORTS, **config)
    found = repro.generate(world.conn, world.layout, world.config, world.issue_id, ["P-0001"],
                           ["a1b2c3d4e5f60718"], {"api": ("backend",)})
    repro.write(world.conn, world.layout.regression_dir(world.issue_id), f"regressions/{world.issue_id}/check.yaml",
                found)
    world.event(IssueEvent.FIX_STARTED, actor="fix")
    return world


def fix_handoff(world, git, **changes):
    outputs = {"issueId": world.issue_id, "branch": "cty/fix-order-500", "worktree": str(world.worktree),
               "baseCommit": BASE, "diffHash": git.diff_hash(world.worktree, BASE),
               "affectedEndpoints": ["GET /api/Order/{id}"], "affectedPages": [], "migration": None}
    outputs.update(changes)
    run = stage_runs.begin(RunStage.FIX, world.layout, world.conn, world.clock, world.events)
    run.handoff(RunStage.FIX, world.issue_id, HandoffStatus.OK, outputs, "fix done")
    return outputs


def to_verify(world, git, **changes):
    (world.worktree / SERVICE_PATH).write_text(FIXED, encoding="utf-8")
    outputs = fix_handoff(world, git, **changes)
    world.event(IssueEvent.FIX_DONE, actor="fix")
    return outputs

