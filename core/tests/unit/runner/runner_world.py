"""执行器流程测试的环境：真实的 git 仓库与只读、修复 worktree，临时数据库，假启动器返回录制的工具输出。"""

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from tightrein.config.user import UserConfig
from tightrein.domain.clock import FixedClock
from tightrein.guards.policy import GuardSettings
from tightrein.guards.service import Guards
from tightrein.observability import events
from tightrein.observability.events import EventLog
from tightrein.observability.redact import Redactor
from tightrein.observability.tracing import Tracer
from tightrein.runner.process import ProcessOutcome
from tightrein.runner.registry import Registry, default_adapters
from tightrein.runner.service import Runner
from tightrein.store.files.layout import ToolLayout
from tightrein.store.migrations.runner import open_database
from tightrein.vcs.git_read import GitReader
from tightrein.vcs.process import VcsProcess

NOW = datetime(2026, 10, 5, 3, 0, tzinfo=timezone.utc)
TOKYO = timezone(timedelta(hours=9))
SCHEMA = "runner/tasks/triage-dedup.schema.json"
VALID = {"sameRootCause": False, "target": None, "evidence": [], "reason": "根因位置不同"}
INVALID = {"sameRootCause": "不是布尔值"}
SESSION = "5f0c2a1e-8d3b-4c6e-9a41-7b2d9e0f1c3a"


def claude_lines(structured, session=SESSION, subtype="success", tools=()):
    """Claude Code stream-json 的一次运行：init、可选的工具调用、result。"""
    lines = [{"type": "system", "subtype": "init", "session_id": session, "model": "claude-opus-4-1"}]
    for number, (name, arguments) in enumerate(tools, start=1):
        lines.append({"type": "assistant", "session_id": session, "message": {"id": f"m{number}", "content": [
            {"type": "tool_use", "id": f"toolu_{number}", "name": name, "input": arguments}]}})
    lines.append({"type": "result", "subtype": subtype, "is_error": subtype != "success", "num_turns": len(tools) + 1,
                  "result": json.dumps(structured, ensure_ascii=False), "session_id": session,
                  "total_cost_usd": 0.5, "usage": {"input_tokens": 1000, "output_tokens": 100},
                  "structured_output": structured})
    return [json.dumps(line, ensure_ascii=False) for line in lines]


@dataclass
class FakeRun:
    """一次录制的进程运行：lines 为标准输出的各行；action 在输出之前执行，模拟 agent 的行为。"""

    lines: list = field(default_factory=list)
    exit_code: int = 0
    stderr: str = ""
    timeout: bool = False
    action: object = None


class FakeLauncher:
    def __init__(self, *runs):
        self.runs = list(runs)
        self.invocations = []
        self.interactive = []

    def run(self, invocation, on_line, timeout_ms):
        self.invocations.append(invocation)
        run = self.runs.pop(0)
        if isinstance(run, BaseException):
            raise run
        if run.action is not None:
            run.action(invocation)
        stopped = "timeout" if run.timeout else None
        for line in run.lines:
            reason = on_line(line)
            if reason is not None:
                stopped = reason
                break
        return ProcessOutcome(-2 if stopped else run.exit_code, run.stderr, stopped, 10)

    def run_interactive(self, invocation):
        self.interactive.append(invocation)
        run = self.runs.pop(0)
        if isinstance(run, BaseException):
            raise run
        if run.action is not None:
            run.action(invocation)
        return run.exit_code


class RunnerWorld:
    def __init__(self, repos, make_config, tmp_path, *runs, **config_changes):
        self.repos = repos
        self.clock = FixedClock(NOW)
        self.home = tmp_path / "home"
        self.tool = ToolLayout(tmp_path / "tightrein")
        skill = self.tool.skill("triage")
        skill.parent.mkdir(parents=True)
        skill.write_text("# triage\n分诊的做法。\n", encoding="utf-8")
        (skill.parent / "roles").mkdir()
        (skill.parent / "roles" / "claim-verifier.md").write_text("取证角色说明\n", encoding="utf-8")
        self.layout = self.tool.workspace("sample")
        _, self.repo = repos.origin_and_clone()
        self.readonly = self.layout.readonly_worktree()
        repos.git(self.repo, "worktree", "add", "-q", "--detach", str(self.readonly), "main")
        self.fix = self.layout.fix_worktree("0007")
        repos.git(self.repo, "worktree", "add", "-q", "-b", "cty/fix-0007", str(self.fix), "main")
        self.config = make_config(**config_changes)
        self.conn = open_database(self.layout.database(), self.clock)
        self.redactor = Redactor()
        self.tracer = Tracer(EventLog(self.layout, self.redactor), self.clock, run_id="R-20261005-030000-triage",
                             stage="triage")
        git = GitReader(VcsProcess(environ=repos.environ))
        self.guards = Guards(git, GuardSettings.from_config(self.config), self.layout, self.tool, tracer=self.tracer)
        self.registry = Registry(default_adapters(self.home), UserConfig(tmp_path / "config.yaml"),
                                 which=lambda name: f"/usr/local/bin/{name}")
        self.launcher = FakeLauncher(*runs)
        self.environ = {**repos.environ, "GH_TOKEN": "ghp_abcdefghijklmnopqrstuvwxyz0123"}

    def runner(self, **options):
        return Runner(conn=self.conn, layout=self.layout, tool_layout=self.tool, config=self.config,
                      registry=self.registry, guards=self.guards, launcher=self.launcher, tracer=self.tracer,
                      redactor=self.redactor, environ=self.environ, zone=TOKYO, monotonic=lambda: 0.0, **options)

    def spans(self):
        return [event for event in events.read(self.layout.events_log(NOW.date())) if event.operation == "invoke_agent"]
