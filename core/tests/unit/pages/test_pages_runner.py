import json
import stat
from dataclasses import replace
from pathlib import Path

import pytest
from page_world import ENCODED_TOKEN, results, trace_bytes, write_results
from probe_world import make_redactor, make_target

from tightrein.config.secrets import SecretNotFound
from tightrein.domain.enums import RunStatus
from tightrein.pipeline.checks.pages.runner import PageDependencies, PageRunner
from tightrein.sources.common.procs import ToolRun
from tightrein.sources.common.session import NO_TARGET
from tightrein.store.files.layout import WorkspaceLayout

ROLES = {"Company": {"keychain": "tightrein.demo.company"}, "Admin": {"keychain": "tightrein.demo.admin"}}
LOGIN = {"endpoint": "/api/Auth/Login", "bodyTemplate": {}, "tokenPath": "data.token"}


class Credentials:
    def __init__(self, missing=()):
        self.missing = set(missing)

    def account(self, role):
        if role in self.missing:
            raise SecretNotFound(f"tightrein.demo.{role.lower()}", "条目不存在")
        return f"test-{role.lower()}"

    def password(self, role):
        return f"pw-{role}"


class FakePlaywright:
    """记录命令与计划文件，检查登录态目录，按录制的结果写出 results.ndjson 与一个 trace。"""

    def __init__(self, items="recorded", run=None):
        self.items = items
        self.run = run or ToolRun(1)
        self.commands = []
        self.plans = []
        self.auth_modes = []

    def __call__(self, command):
        self.commands.append(command)
        document = json.loads(Path(command.env["TIGHTREIN_PAGE_PLAN"]).read_text(encoding="utf-8"))
        self.plans.append(document)
        auth_dir = Path(document["projects"][0]["storageState"]).parent
        self.auth_modes.append(stat.S_IMODE(auth_dir.stat().st_mode))
        raw_dir = Path(document["resultsFile"]).parent
        if self.items is not None:
            items = results(raw_dir) if self.items == "recorded" else self.items
            write_results(raw_dir, items)
            trace = raw_dir / "test-results" / "Company-broken-首页有统计卡片-e2e-Company-retry1" / "trace.zip"
            trace.parent.mkdir(parents=True, exist_ok=True)
            trace.write_bytes(trace_bytes())
        return self.run


class World:
    def __init__(self, tmp_path, make_config, *, launcher=None, credentials=None, installed=None):
        self.layout = WorkspaceLayout(tmp_path / "workspace")
        self.launcher = launcher or FakePlaywright()
        config = make_config(accounts={"roles": ROLES, "login": LOGIN}, checks={"pages": {"patrolGrep": "@browse"}})
        self.runner = PageRunner(PageDependencies(
            config, credentials or Credentials(), self.launcher, self.layout, make_redactor(),
            environ={"PATH": "/bin", "HOME": "/Users/me"}, installed_problem=installed or (lambda: None),
            runtime=tmp_path / "runtime", temp_root=tmp_path))
        self.target = make_target(tmp_path, "pages", worktree=tmp_path / "worktree")
        self.tmp_path = tmp_path

    def run(self, **options):
        return self.runner.run(self.target, **options)


def test_full_run(tmp_path, make_config):
    world = World(tmp_path, make_config)
    outcome = world.run()
    assert outcome.status is RunStatus.PARTIAL
    kinds = sorted({item.kind for item in outcome.failures})
    assert kinds == ["case-failure", "console-error", "failed-request"]
    assert all(item.page.startswith("/") for item in outcome.failures)
    assert any(note.startswith("角色 Admin 登录失败：TimeoutError") for note in outcome.notes)
    assert "results.ndjson" in outcome.artifacts and "page-plan.json" in outcome.artifacts


def test_command_plan_and_login_state(tmp_path, make_config):
    world = World(tmp_path, make_config)
    world.run()
    command = world.launcher.commands[0]
    assert command.argv == ("npx", "playwright", "test", "--config", "playwright.config.ts", "--project",
                            "patrol-Company", "--project", "patrol-Admin", "--grep", "@browse")
    assert command.cwd == tmp_path / "runtime" and command.timeout_seconds == 1800
    assert command.env["TIGHTREIN_PASSWORD_Company"] == "pw-Company" and "HOME" in command.env
    document = world.launcher.plans[0]
    assert document["loginModule"] == str(world.layout.e2e_dir() / "login.ts")
    assert "pw-Company" not in json.dumps(document)
    assert world.launcher.auth_modes == [0o700]
    auth_dir = Path(document["projects"][0]["storageState"]).parent
    assert not auth_dir.exists()


def test_traces_are_cleaned(tmp_path, make_config):
    world = World(tmp_path, make_config)
    world.run()
    trace = next((tmp_path / "raw" / "pages").rglob("trace.zip"))
    assert ENCODED_TOKEN.encode() not in trace.read_bytes()


def test_missing_or_incomplete_results_fail(tmp_path, make_config):
    outcome = World(tmp_path / "a", make_config, launcher=FakePlaywright(items=None)).run()
    assert outcome.status is RunStatus.FAILED and "结果不完整：results.ndjson 缺失或最后一行不完整" in outcome.notes


def test_timeout_keeps_what_was_written(tmp_path, make_config):
    launcher = FakePlaywright(run=ToolRun(None, timed_out=True))
    outcome = World(tmp_path, make_config, launcher=launcher).run()
    assert outcome.status is RunStatus.PARTIAL and outcome.failures
    assert "Playwright 超过 checks.pages.timeoutMinutes，已终止，已写出的结果照常解析" in outcome.notes


def test_unavailable_accounts(tmp_path, make_config):
    world = World(tmp_path / "a", make_config, credentials=Credentials({"Admin"}))
    outcome = world.run()
    assert "patrol-Admin" not in world.launcher.commands[0].argv
    assert outcome.status is RunStatus.PARTIAL
    assert "角色 Admin：钥匙串条目 tightrein.demo.admin：条目不存在" in outcome.notes
    everyone = World(tmp_path / "b", make_config, credentials=Credentials({"Admin", "Company"})).run()
    assert everyone.status is RunStatus.FAILED and "全部角色的账号都不可用" in everyone.notes


def test_skipped_without_a_target(tmp_path, make_config):
    world = World(tmp_path, make_config)
    no_target = world.runner.run(replace(world.target, base_url=None))
    assert (no_target.status, no_target.notes) == (RunStatus.SKIPPED, (NO_TARGET,))


class AnonymousPlaywright:
    """只记录命令与计划文件，写出一条匿名身份的用例结果。"""

    def __init__(self):
        self.commands = []
        self.plans = []

    def __call__(self, command):
        self.commands.append(command)
        document = json.loads(Path(command.env["TIGHTREIN_PAGE_PLAN"]).read_text(encoding="utf-8"))
        self.plans.append(document)
        write_results(Path(document["resultsFile"]).parent, [{
            "title": "首页可以打开", "file": "common/home.spec.ts", "project": "patrol-anonymous", "role": "anonymous",
            "tags": [], "outcome": "expected", "retries": 0, "failedStep": None, "error": None, "attachments": {},
            "startedAt": None, "durationMs": 10}])
        return ToolRun(0)


def test_without_accounts_runs_anonymously(tmp_path, make_config):
    launcher = AnonymousPlaywright()
    world = World(tmp_path, make_config, launcher=launcher, credentials=Credentials({"anonymous"}))
    world.runner.deps = replace(world.runner.deps, config=make_config(accounts=None))
    outcome = world.run()
    command = launcher.commands[0]
    assert command.argv[5:7] == ("--project", "patrol-anonymous") and command.argv.count("--project") == 1
    assert not any(name.startswith("TIGHTREIN_PASSWORD_") for name in command.env)
    assert [project["kind"] for project in launcher.plans[0]["projects"]] == ["patrol"]
    assert "storageState" not in launcher.plans[0]["projects"][0]
    assert outcome.status is RunStatus.OK and outcome.failures == ()


def test_not_installed_and_nothing_ran(tmp_path, make_config):
    missing = World(tmp_path / "a", make_config, installed=lambda: "Playwright 未安装").run()
    assert (missing.status, missing.notes) == (RunStatus.FAILED, ("Playwright 未安装",))
    setups_only = [item for item in results(tmp_path) if item["project"].startswith("setup-")]
    idle = World(tmp_path / "b", make_config, launcher=FakePlaywright(items=setups_only)).run()
    assert idle.status is RunStatus.FAILED and "没有任何角色的用例得到执行" in idle.notes


@pytest.mark.parametrize("spec_dirs", [(Path("/ws/regressions/0007"),)])
def test_regression_directories(tmp_path, make_config, spec_dirs):
    world = World(tmp_path, make_config)
    world.run(roles=("Company",), spec_dirs=spec_dirs, grep="首页")
    command = world.launcher.commands[0]
    assert command.argv[-4:] == ("--project", "regress-Company", "--grep", "首页")
    assert world.launcher.plans[0]["projects"][1]["testDir"] == "/ws/regressions/0007"
