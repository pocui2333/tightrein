import json
from datetime import date, timedelta

import pytest
from cli_world import NOW, make_cli_world

from tightrein.cli.assemble import App, Options, parse_now
from tightrein.cli.exit_codes import UsageError
from tightrein.domain.clock import parse_iso
from tightrein.domain.enums import Probe as ProbeKind
from tightrein.domain.enums import Stage
from tightrein.pipeline.checks.pages.runner import PageRunner
from tightrein.pipeline.learn.steps.third_party import RepoFacts
from tightrein.sources.base import ProbeTarget
from tightrein.store.files.layout import WorkspaceLayout


def test_every_service_is_assembled(tmp_path):
    app = make_cli_world(tmp_path).app()
    for name in ("collect", "aggregate", "problems", "triage", "issue", "fix", "verify", "release", "learn"):
        assert getattr(app, name)() is not None
    assert app.fix() is app.fix()
    assert app.fix().deps.operations is not None
    assert set(app.operations().follow_ups) == {Stage.FIX, Stage.RELEASE}
    assert app.fix().deps.branch_prefix is None and app.triage().deps.sync == app.sync_readonly


def test_the_github_mirror_is_assembled_only_for_the_github_tracker(tmp_path):
    assert make_cli_world(tmp_path / "local").app().issue().deps.mirror is None
    app = make_cli_world(tmp_path / "github", issues={"tracker": "github", "github": {"repo": "owner/name"}}).app()
    mirror = app.issue().deps.mirror
    assert mirror is app.github_mirror() and mirror.process is app.process and mirror.reader.limit == 1000
    assert set(app.operations().follow_ups) == {Stage.FIX, Stage.RELEASE, Stage.ISSUE}


def test_output_mode_keeps_the_real_database(tmp_path):
    world = make_cli_world(tmp_path)
    app = world.app(output_dir=tmp_path / "out")
    assert app.layout.output_dir == tmp_path / "out" and app.output_mode
    assert WorkspaceLayout(world.root).database().is_file()
    assert not (tmp_path / "out" / "data").exists()


def test_regression_executor_has_every_kind(tmp_path):
    executor = make_cli_world(tmp_path).app().regression_executor("http://127.0.0.1:5101")
    assert executor.api is not None and executor.page is not None and executor.static is not None
    assert executor.api.session.base_url == "http://127.0.0.1:5101"
    assert executor.static.timeout_seconds == 600


def test_semgrep_command_comes_from_the_user_config_then_the_project(tmp_path):
    app = make_cli_world(tmp_path / "default").app()
    assert app.semgrep == "semgrep" and app.regression_executor().static.command == "semgrep"
    world = make_cli_world(tmp_path / "project", runtime={"tools": {"semgrep": "/opt/semgrep/bin/semgrep"}})
    assert world.app().semgrep == "/opt/semgrep/bin/semgrep"
    config = world.home / ".config" / "tightrein" / "config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("tools:\n  semgrep: {path: local/semgrep/bin/semgrep}\n", encoding="utf-8")
    app = world.app()
    expected = str(app.tool.root / "local/semgrep/bin/semgrep")
    assert app.semgrep == expected
    assert app.regression_executor().static.command == expected
    assert app.invoker().tools == {"TIGHTREIN_SEMGREP": expected}


def test_verify_probes_log_in_at_the_local_address(tmp_path):
    app = make_cli_world(tmp_path).app()
    target = ProbeTarget("local", "R-20261005-030000-verify", tmp_path / "raw", app.clock,
                         base_url="http://localhost:5101")
    probes = app.verify().deps.probes(target)
    assert set(probes) == {"api-fuzz", "pages"}
    assert probes["api-fuzz"].deps.session.base_url == "http://localhost:5101"
    assert isinstance(probes["pages"], PageRunner)


def test_collect_sources_and_disabled_methods(tmp_path):
    app = make_cli_world(tmp_path).app()
    for kind in (ProbeKind.PLATFORM_ERRORS, ProbeKind.ACCESS_LOG, ProbeKind.ALERTS, ProbeKind.PROJECT_PROBE):
        assert app.probe(kind).name is kind
    disabled = app.disabled_sources()
    assert set(disabled) >= {"platform-errors", "access-log", "alerts", "project-probe"}
    assert "static" not in disabled and "incidental" not in disabled
    assert app.collect().deps.disabled() == disabled


def test_repo_query_reads_gh_api(tmp_path):
    world = make_cli_world(tmp_path)
    world.vcs.add(("api", "repos/owner/skills"), json.dumps(
        {"stargazers_count": 6200, "pushed_at": "2026-09-01T10:00:00Z", "archived": False}))
    app = world.app()
    assert app.repo_query("https://github.com/owner/skills") == RepoFacts(6200, date(2026, 9, 1), False)
    world.vcs.add(("api", "repos/owner/gone"), (1, ""))
    with pytest.raises(LookupError, match="owner/gone"):
        app.repo_query("https://github.com/owner/gone")


def test_workspace_must_be_given_and_exist(tmp_path):
    world = make_cli_world(tmp_path)
    with pytest.raises(UsageError, match="defaultWorkspace"):
        App(Options(), world.externals())
    with pytest.raises(UsageError, match="不存在"):
        App(Options(workspace=tmp_path / "missing"), world.externals())


def test_now_fixes_the_clock_and_waiting_advances_it(tmp_path):
    app = make_cli_world(tmp_path).app()
    assert app.clock.now() == parse_iso(NOW)
    app.wait_until(parse_iso(NOW) + timedelta(seconds=1))
    assert app.clock.now() == parse_iso(NOW) + timedelta(seconds=1)
    assert parse_now("2026-10-05", app.zone).isoformat() == "2026-10-05T00:00:00+09:00"
    with pytest.raises(UsageError):
        parse_now("昨天", app.zone)


def test_purge_rotates_the_launchd_logs(tmp_path):
    world = make_cli_world(tmp_path)
    app = world.app()
    log = app.layout.launchd_out_log()
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_bytes(b"x" * 10 * 1024 * 1024)
    app.purge()
    assert not log.exists() and log.with_name(log.name + ".1").is_file()


def test_the_user_proxy_reaches_git_and_every_subprocess(tmp_path):
    world = make_cli_world(tmp_path)
    config = world.home / ".config" / "tightrein" / "config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("network:\n  proxy: http://user:pa55word@127.0.0.1:8118\n"
                      "  noProxy: [github.com, api.github.com]\n", encoding="utf-8")
    commands = []

    def execute(command):
        commands.append(command)
        return world.vcs(command)

    app = App(Options(workspace=world.root, now=NOW), world.externals(vcs_execute=execute))
    app.git.head(world.root)
    environment = commands[-1].env
    assert environment["https_proxy"] == "http://user:pa55word@127.0.0.1:8118"
    assert environment["no_proxy"] == environment["NO_PROXY"] == "github.com,api.github.com,localhost,127.0.0.1,::1"
    assert app.verify().deps.environ["no_proxy"] == environment["no_proxy"]
    assert app.invoker().environ["NO_PROXY"] == environment["no_proxy"]
    assert app.redactor.text("pa55word") != "pa55word"


def test_without_a_user_proxy_the_process_environment_is_kept(tmp_path):
    world = make_cli_world(tmp_path)
    app = App(Options(workspace=world.root, now=NOW),
              world.externals(environ={"PATH": "/usr/bin", "https_proxy": "http://proxy.test:1", "no_proxy": "x"}))
    assert app.process.environment()["https_proxy"] == "http://proxy.test:1"
    assert app.process.environment()["no_proxy"] == "x"
    assert app.transport is world.transport
