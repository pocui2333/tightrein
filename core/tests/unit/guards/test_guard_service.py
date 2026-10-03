import json
import os
import stat
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from tightrein.domain.clock import FixedClock
from tightrein.domain.enums import Access, Stage, ViolationKind
from tightrein.guards import readonly
from tightrein.guards.policy import GuardSettings
from tightrein.guards.report import GuardBlocked
from tightrein.guards.service import Guards, hidden_reads
from tightrein.observability import events
from tightrein.observability.events import EventLog
from tightrein.observability.redact import Redactor
from tightrein.observability.tracing import Tracer
from tightrein.runner.task import Instructions, RunnerTask, Subject
from tightrein.store.files.layout import ToolLayout
from tightrein.store.locks import Holder
from tightrein.vcs.git_read import GitReader
from tightrein.vcs.process import VcsProcess

NOW = datetime(2026, 10, 5, 3, 0, tzinfo=timezone.utc)
RUN = "R-20261005-030000-fix"
BASE_ENV = {"PATH": "/usr/bin:/bin", "HOME": "/Users/cty", "GH_TOKEN": "ghp_abcdefghijklmnopqrstuvwxyz0123"}


class World:
    def __init__(self, repos, make_config, tmp_path):
        self.repos = repos
        self.clock = FixedClock(NOW)
        self.tool = ToolLayout(tmp_path / "tightrein")
        self.layout = self.tool.workspace("sample")
        for path in ("skills/fix/SKILL.md", "core/tightrein/__init__.py", "core/.venv/lib/site.py"):
            target = self.tool.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("x\n", encoding="utf-8")
        for path in (self.layout.project_config(), self.layout.eval_manifest(),
                     self.layout.regression_dir("0007") / "api-1.http"):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('GET /api/orders?no="ORD-20260929"\n', encoding="utf-8")
        _, self.repo = repos.origin_and_clone()
        repos.commit(self.repo, "chore: protected", {"Migrations/MigrationList.cs": "// changes\n"})
        repos.git(self.repo, "push", "-q", "origin", "main")
        self.base = repos.head(self.repo)
        self.readonly = self.layout.readonly_worktree()
        repos.git(self.repo, "worktree", "add", "-q", "--detach", str(self.readonly), "main")
        self.fix = self.layout.fix_worktree("0007")
        repos.git(self.repo, "worktree", "add", "-q", "-b", "cty/fix-0007", str(self.fix), "main")
        self.git = GitReader(VcsProcess(environ=repos.environ))
        self.log = EventLog(self.layout, Redactor())
        tracer = Tracer(self.log, self.clock, run_id=RUN, stage="fix")
        self.guards = Guards(self.git, GuardSettings.from_config(make_config()), self.layout, self.tool, tracer=tracer)

    def task(self, workdir, access=Access.WORKSPACE_WRITE, **changes):
        task = RunnerTask(RUN, Stage.FIX, "fix-executor", Subject("issue", "0007"), 1, Instructions("修复"), workdir,
                          "runner/roles/fix-executor.schema.json", access)
        return replace(task, **changes)

    def run(self, task, agent=lambda: None, tool_calls=()):
        context = self.guards.before(task, clock=self.clock, base_env=BASE_ENV)
        try:
            agent()
        finally:
            report = self.guards.after(task, context, tool_calls)
        return context, report

    def gates(self):
        return [event for event in events.read(self.layout.events_log(NOW.date())) if event.operation == "gate"]


@pytest.fixture
def world(repos, make_config, tmp_path):
    world = World(repos, make_config, tmp_path)
    yield world
    for directory, _, _ in os.walk(world.readonly):
        os.chmod(directory, 0o755)


def kinds(report):
    return [(item.kind, item.path) for item in report.violations]


def test_readonly_worktree_cannot_be_written(world):
    task = world.task(world.readonly, Access.READ_ONLY, role="claim-verifier", subject=Subject("problem", "P-0042"))

    def agent():
        with pytest.raises(PermissionError):
            (world.readonly / "README.md").write_text("changed\n", encoding="utf-8")
        os.chmod(world.readonly / "README.md", 0o644)
        (world.readonly / "README.md").write_text("changed\n", encoding="utf-8")

    context, report = world.run(task, agent)
    assert context.env["GIT_OPTIONAL_LOCKS"] == "0"
    assert kinds(report) == [(ViolationKind.READONLY_MODIFIED, "README.md")]
    assert (report.lines_added, report.lines_removed) == (1, 1)
    assert os.stat(world.readonly / "src" / "OrderService.cs").st_mode & stat.S_IWUSR
    assert not world.layout.readonly_guard("readonly").exists()
    saved = json.loads(world.layout.guard_report(RUN, "claim-verifier", "P-0042").read_text(encoding="utf-8"))
    assert saved["report"]["ok"] is False
    assert [gate.decision for gate in world.gates()] == ["violation"]


def test_readonly_run_without_changes_passes(world):
    task = world.task(world.readonly, Access.READ_ONLY)
    context, report = world.run(task, lambda: None)
    assert report.ok and report.changed_files == ()
    assert context.removed_env_names == ("GH_TOKEN",)
    assert "GH_TOKEN" not in context.env
    assert report.removed_env_names == ("GH_TOKEN",)


def test_existing_uncommitted_changes_are_not_violations(world):
    world.repos.write(world.fix, "src/OrderService.cs", "上一轮的改动\n")
    _, report = world.run(world.task(world.fix))
    assert report.ok


def test_changes_inside_the_fix_worktree(world):
    def agent():
        world.repos.write(world.fix, "src/OrderService.cs", "class OrderService\n{\n    [AllowAnonymous]\n}\n")
        world.repos.write(world.fix, "src/New.cs", "class New {}\n")

    _, report = world.run(world.task(world.fix), agent)
    assert report.ok is False
    assert kinds(report) == [(ViolationKind.PROTECTED_MODIFIED, "src/OrderService.cs")]
    assert report.changed_files == ("src/New.cs", "src/OrderService.cs")
    assert (report.lines_added, report.lines_removed) == (2, 1)


def test_protected_files_and_tests(world):
    def agent():
        world.repos.write(world.fix, "Migrations/MigrationList.cs", "// new change set\n")
        world.repos.write(world.fix, "tests/OrderTests.cs", "class OrderTests { }\n")

    _, report = world.run(world.task(world.fix), agent)
    assert kinds(report) == [(ViolationKind.PROTECTED_MODIFIED, "Migrations/MigrationList.cs"),
                             (ViolationKind.TEST_MODIFIED, "tests/OrderTests.cs")]
    approved = world.task(world.fix, approved_protected_paths=("Migrations/MigrationList.cs",))
    _, report = world.run(approved, lambda: world.repos.write(world.fix, "Migrations/MigrationList.cs", "// again\n"))
    assert report.ok


def test_tasks_that_only_write_tests_may_touch_tests_and_nothing_else(world):
    def agent():
        world.repos.write(world.fix, "tests/OrderTests.cs", "class OrderTests { void Owner() {} }\n")
        world.repos.write(world.fix, "src/New.cs", "class New {}\n")

    _, report = world.run(world.task(world.fix, tests_only=True), agent)
    assert kinds(report) == [(ViolationKind.OUTSIDE_PLAN, "src/New.cs")]
    _, report = world.run(world.task(world.fix, tests_only=True),
                          lambda: world.repos.write(world.fix, "tests/OrderTests.cs", "class OrderTests { }\n// x\n"))
    assert report.ok


def test_forbidden_paths_outside_the_workdir(world):
    def agent():
        world.layout.eval_manifest().write_text("{}\n", encoding="utf-8")
        (world.tool.root / "core" / "tightrein" / "__init__.py").write_text("changed\n", encoding="utf-8")
        (world.tool.root / "core" / ".venv" / "lib" / "site.py").write_text("ignored\n", encoding="utf-8")

    _, report = world.run(world.task(world.fix), agent)
    assert kinds(report) == [
        (ViolationKind.FORBIDDEN_PATH_MODIFIED, str(world.tool.root / "core" / "tightrein" / "__init__.py")),
        (ViolationKind.FORBIDDEN_PATH_MODIFIED, str(world.layout.eval_manifest())),
    ]


def test_git_changes_are_reported(world):
    _, report = world.run(world.task(world.fix), lambda: world.repos.commit(world.fix, "agent commit", {"x.txt": "x"}))
    assert [item.kind for item in report.violations] == [ViolationKind.GIT_COMMIT_CREATED]


def test_credentials_block_the_run(world):
    world.repos.write(world.fix, ".env", "DB_PASSWORD=x\n")
    with pytest.raises(GuardBlocked) as caught:
        world.guards.before(world.task(world.fix), clock=world.clock, base_env=BASE_ENV)
    assert [item.kind for item in caught.value.violations] == [ViolationKind.CREDENTIAL_PRESENT]
    saved = json.loads(world.layout.guard_report(RUN, "fix-executor", "0007").read_text(encoding="utf-8"))
    assert saved["phase"] == "before"


def test_unreadable_git_state_blocks_the_run(world, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(GuardBlocked) as caught:
        world.guards.before(world.task(plain), clock=world.clock, base_env=BASE_ENV)
    assert [item.kind for item in caught.value.violations] == [ViolationKind.GIT_UNREADABLE]


def test_a_failed_lock_blocks_the_run(world):
    def refuse(path, mode):
        if not mode & stat.S_IWUSR:
            raise PermissionError(1, "Operation not permitted")
        os.chmod(path, mode, follow_symlinks=False)

    guards = Guards(world.git, world.guards.settings, world.layout, world.tool, chmod=refuse)
    with pytest.raises(GuardBlocked) as caught:
        guards.before(world.task(world.readonly, Access.READ_ONLY), clock=world.clock, base_env=BASE_ENV)
    assert [item.kind for item in caught.value.violations] == [ViolationKind.READONLY_MODIFIED]
    assert not world.layout.readonly_guard("readonly").exists()


def test_stale_locks_are_restored_before_locking_again(world):
    finished = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
    readonly.lock(world.readonly, world.layout.readonly_guard("readonly"), world.clock,
                  holder=Holder(int(finished.stdout), "host"))
    _, report = world.run(world.task(world.readonly, Access.READ_ONLY))
    assert report.ok
    assert os.stat(world.readonly / "README.md").st_mode & stat.S_IWUSR
    readonly.lock(world.readonly, world.layout.readonly_guard("readonly"), world.clock)
    with pytest.raises(GuardBlocked) as caught:
        world.guards.before(world.task(world.readonly, Access.READ_ONLY), clock=world.clock, base_env=BASE_ENV)
    assert "正被进程" in caught.value.violations[0].detail
    readonly.restore(world.layout.readonly_guard("readonly"))


def test_a_failed_restore_is_reported_and_keeps_the_marker(world):
    state = {"fail": False}

    def chmod(path, mode):
        if state["fail"] and mode & stat.S_IWUSR:
            raise PermissionError(1, "Operation not permitted")
        os.chmod(path, mode, follow_symlinks=False)

    guards = Guards(world.git, world.guards.settings, world.layout, world.tool, tracer=world.guards.tracer, chmod=chmod)
    task = world.task(world.readonly, Access.READ_ONLY)
    context = guards.before(task, clock=world.clock, base_env=BASE_ENV)
    state["fail"] = True
    report = guards.after(task, context)
    assert "chmod -R u+w" in report.restore_error
    assert world.layout.readonly_guard("readonly").exists()
    assert "readonly-restore-failed" in [gate.decision for gate in world.gates()]
    readonly.restore(world.layout.readonly_guard("readonly"))


def test_hidden_path_reads(world):
    calls = [
        {"toolName": "Read", "toolInput": {"file_path": str(world.layout.regression_dir("0007") / "api-1.http")}},
        {"toolName": "Bash", "toolInput": {"command": "cat ../../evals/manifest.json | head"}},
        {"toolName": "Bash", "toolInput": {"command": "cat config/.env"}},
        {"toolName": "Read", "toolInput": {"file_path": "src/OrderService.cs"}},
        {"toolName": "Bash", "toolInput": {"command": "ls regressions/"}},
    ]
    (world.fix / "config").mkdir(exist_ok=True)
    (world.fix / "config" / ".env").write_text("X=1\n", encoding="utf-8")
    found = hidden_reads(calls, world.fix, [world.layout.regressions_dir(), world.layout.evals_dir()], [".env"])
    assert [item.path for item in found] == [
        str(world.layout.regression_dir("0007") / "api-1.http"), "../../evals/manifest.json", "config/.env",
    ]
    (world.fix / "config" / ".env").unlink()
    _, report = world.run(world.task(world.fix), tool_calls=calls[:1])
    assert [item.kind for item in report.violations] == [ViolationKind.HIDDEN_PATH_READ]


def test_credential_like_tokens_that_are_not_files_are_not_reads(world):
    calls = [
        {"toolName": "StructuredOutput", "toolInput": {"claims": [{"statement": "items.map((it) => it.key)"}]}},
        {"toolName": "Bash", "toolInput": {"command": 'rg "it.key" api/'}},
        {"toolName": "Read", "toolInput": {"file_path": "certs/server.key"}},
    ]
    assert hidden_reads(calls, world.fix, [], ["*.key"]) == []
    (world.fix / "certs").mkdir()
    (world.fix / "certs" / "server.key").write_text("k\n", encoding="utf-8")
    assert [item.path for item in hidden_reads(calls, world.fix, [], ["*.key"])] == ["certs/server.key"]


def test_interactive_sessions_only_check_git_state_and_hidden_reads(world):
    task = world.task(world.fix, Access.READ_ONLY, role="fix-session", interactive=True, output_schema=None)
    context, report = world.run(task, lambda: world.repos.write(world.fix, "src/OrderService.cs", "改动\n"))
    assert context.marker_path is None
    assert report.ok and report.changed_files == ()


def test_check_diff(world):
    world.repos.write(world.fix, "src/OrderService.cs", 'if (no == "ORD-20260929") return null;\n')
    world.repos.write(world.fix, "tests/OrderTests.cs", "  [Fact(Skip = \"flaky\")]\n")
    world.repos.write(world.fix, "src/A.cs", "a\n")
    world.repos.write(world.fix, "src/B.cs", "b\n")
    world.repos.write(world.fix, "src/C.cs", "c\n")
    report = world.guards.check_diff(world.fix, world.base, issue_id="0007")
    assert sorted({item.kind for item in report.violations}) == sorted({
        ViolationKind.SIZE_EXCEEDED, ViolationKind.TEST_MODIFIED, ViolationKind.SKIP_MARKER_ADDED,
        ViolationKind.SUSPECTED_HARDCODE,
    })
    assert report.changed_files == ("src/A.cs", "src/B.cs", "src/C.cs", "src/OrderService.cs", "tests/OrderTests.cs")
    assert not report.ok


def test_check_diff_hardcode_alone_passes_and_forbidden_baseline(world):
    baseline = world.guards.snapshot_forbidden()
    world.repos.write(world.fix, "src/OrderService.cs", 'if (no == "ORD-20260929") return null;\n')
    report = world.guards.check_diff(world.fix, world.base, issue_id="0007", forbidden_baseline=baseline)
    assert [item.kind for item in report.violations] == [ViolationKind.SUSPECTED_HARDCODE]
    assert report.ok
    world.layout.project_config().write_text("changed\n", encoding="utf-8")
    report = world.guards.check_diff(world.fix, world.base, issue_id="0007", forbidden_baseline=baseline)
    assert ViolationKind.FORBIDDEN_PATH_MODIFIED in [item.kind for item in report.violations]


def test_recover_restores_leftover_locks(world):
    finished = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
    readonly.lock(world.readonly, world.layout.readonly_guard("readonly"), world.clock,
                  holder=Holder(int(finished.stdout), "host"))
    assert world.guards.recover() == [world.readonly]
    assert os.stat(world.readonly / "README.md").st_mode & stat.S_IWUSR
    assert [gate.decision for gate in world.gates()] == ["readonly-recovered"]
