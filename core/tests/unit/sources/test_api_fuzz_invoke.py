import tomllib
from importlib import metadata
from pathlib import Path

import pytest

from tightrein.domain.enums import ProbeLevel
from tightrein.sources.api_fuzz import config_writer, invoke
from tightrein.sources.base import ProbeOptions
from tightrein.sources.common.procs import ToolRun

THRESHOLDS = {
    "suppressionDays": {"value": 30, "min": 7, "max": 90},
    "triage": {"deferredReopenOccurrences": {"value": 3, "min": 1, "max": 10}},
    "slowResponseSeconds": {"value": 3, "min": 1, "max": 30},
}


def test_config_file_has_no_token_and_keeps_default_sanitization(tmp_path):
    path = config_writer.write(tmp_path / "raw" / "schemathesis.toml", 2, ["X-Company-Key", "authorization"])
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    assert data["hooks"] == "tightrein.sources.api_fuzz.hooks" and data["workers"] == 2
    assert data["headers"] == {"Authorization": "Bearer ${TIGHTREIN_TOKEN}"}
    keys = data["output"]["sanitization"]["keys-to-sanitize"]
    assert data["output"]["sanitization"]["enabled"] is True
    assert "cookie" in keys and keys[-1] == "x-company-key" and keys.count("authorization") == 1


def test_shallow_and_deep_defaults(make_config):
    config = make_config()
    shallow = invoke.parameters(ProbeLevel.SHALLOW, config, ProbeOptions(), lambda: 42)
    assert (shallow.max_examples, shallow.phases, shallow.include_methods) == (
        50, ("examples", "coverage", "fuzzing"), ("GET",))
    assert (shallow.seed, shallow.workers, shallow.exclude_regex, shallow.max_response_time) == (42, 1, None, None)
    assert shallow.timeout_seconds == 1800 and shallow.methods == "GET"
    deep = invoke.parameters(ProbeLevel.DEEP, config, ProbeOptions(), lambda: 7)
    assert (deep.max_examples, deep.phases[-1], deep.include_methods, deep.methods) == (200, "stateful", (), "all")


def test_configuration_and_options_override(make_config):
    config = make_config(sources={"api-fuzz": {
        "exclude": [r"^/api/Auth/", "Alipay"], "workers": 4, "sanitizeKeys": ["X-Key"], "timeoutMinutes": 5,
        "levels": {"shallow": {"maxExamples": 20, "includeMethod": None}}}}, thresholds=THRESHOLDS)
    params = invoke.parameters(ProbeLevel.SHALLOW, config, ProbeOptions(), lambda: 1)
    assert (params.max_examples, params.include_methods, params.workers, params.timeout_seconds) == (20, (), 4, 300)
    assert params.exclude_regex == "(?:^/api/Auth/)|(?:Alipay)" and params.max_response_time == 3
    assert params.sanitize_keys == ("X-Key",)
    options = ProbeOptions(max_examples=5, phases=("examples",), include_methods=("post",),
                           include_paths=("/api/Order/Query",), exclude_path_regex="x", max_response_time=1.5,
                           seed=99)
    overridden = invoke.parameters(ProbeLevel.SHALLOW, config, options, lambda: 1)
    assert (overridden.max_examples, overridden.phases, overridden.include_methods) == (5, ("examples",), ("POST",))
    assert (overridden.include_paths, overridden.exclude_regex) == (("/api/Order/Query",), "x")
    assert (overridden.max_response_time, overridden.seed) == (1.5, 99)


def params(**changes):
    values = dict(max_examples=50, phases=("examples", "coverage", "fuzzing"), include_methods=("GET",),
                  include_paths=("/api/Company",), exclude_regex="^/api/Auth/", max_response_time=2.0, seed=42,
                  workers=1, sanitize_keys=(), timeout_seconds=60)
    values.update(changes)
    return invoke.RunParameters(**values)


def test_command_line(tmp_path):
    role_dir = tmp_path / "Company"
    argv = invoke.build_argv(params(), Path("/venv/bin/schemathesis"), Path("/specs/openapi.json"),
                             Path("/raw/schemathesis.toml"), "https://staging.example.test", role_dir)
    assert argv == (
        "/venv/bin/schemathesis", "--config-file", "/raw/schemathesis.toml", "run", "/specs/openapi.json",
        "--url", "https://staging.example.test", "--max-examples", "50", "--phases", "examples,coverage,fuzzing",
        "--include-method", "GET", "--include-path", "/api/Company", "--exclude-path-regex", "^/api/Auth/",
        "--max-response-time", "2", "--report", "junit,vcr,ndjson", "--report-dir", str(role_dir),
        "--report-ndjson-path", str(role_dir / "events.ndjson"), "--report-vcr-path", str(role_dir / "cassette.yaml"),
        "--report-junit-path", str(role_dir / "junit.xml"), "--seed", "42")
    bare = invoke.build_argv(params(include_methods=(), include_paths=(), exclude_regex=None, max_response_time=None),
                             Path("st"), Path("o.json"), Path("c.toml"), "http://h", role_dir)
    assert "--include-method" not in bare and "--exclude-path-regex" not in bare and "--max-response-time" not in bare


def test_token_and_model_only_through_the_environment():
    environ = {"PATH": "/bin", "GH_TOKEN": "secret", "HOME": "/Users/me"}
    with_model = invoke.role_env(environ, "tok", "Company", Path("/specs/authz-model.json"))
    assert with_model["TIGHTREIN_TOKEN"] == "tok" and with_model["TIGHTREIN_ROLE"] == "Company"
    assert with_model["TIGHTREIN_AUTHZ_MODEL"] == "/specs/authz-model.json" and "GH_TOKEN" not in with_model
    without = invoke.role_env(environ, "tok", "Company", None)
    assert "TIGHTREIN_ROLE" not in without and "TIGHTREIN_AUTHZ_MODEL" not in without
    assert "TIGHTREIN_TOKEN" not in invoke.role_env(environ, "", "anonymous", None)


class FakeLauncher:
    def __init__(self, run):
        self.run = run
        self.commands = []

    def __call__(self, command):
        self.commands.append(command)
        return self.run


@pytest.mark.parametrize(("run", "completed", "problem"), [
    (ToolRun(0), True, None),
    (ToolRun(1), True, None),
    (ToolRun(2), False, "角色 Company：Schemathesis 退出码 2(配置或接口描述错误)，见 schemathesis.log"),
    (ToolRun(None, timed_out=True), False, "角色 Company：Schemathesis 超时，进程组已终止"),
])
def test_run_role(tmp_path, run, completed, problem):
    launcher = FakeLauncher(run)
    result = invoke.run_role(launcher, params(), "Company", "tok", tmp_path / "openapi.json", tmp_path / "c.toml",
                             "http://h", tmp_path / "Company", None, {"PATH": "/bin"}, Path("/venv/bin/schemathesis"))
    command = launcher.commands[0]
    assert (command.cwd, command.timeout_seconds, command.log_file) == (
        tmp_path / "Company", 60, tmp_path / "Company" / "schemathesis.log")
    assert "tok" not in " ".join(command.argv) and command.env["TIGHTREIN_TOKEN"] == "tok"
    assert (result.completed, result.problem()) == (completed, problem)


def test_installed_version_is_checked(tmp_path):
    command = tmp_path / "schemathesis"
    command.write_text("", encoding="utf-8")

    def missing(name):
        raise metadata.PackageNotFoundError(name)

    assert invoke.installed_problem(lambda name: "4.28.0", command) is None
    assert invoke.installed_problem(lambda name: "4.1.0", command).startswith("Schemathesis 版本为 4.1.0")
    assert invoke.installed_problem(missing, command).startswith("Schemathesis 未安装")
    assert "找不到 schemathesis 命令" in invoke.installed_problem(lambda name: "4.28.0", tmp_path / "none")
