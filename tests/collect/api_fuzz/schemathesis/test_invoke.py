import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import metadata
from pathlib import Path

import pytest

from tightrein.collect.api_fuzz.limits import Restriction
from tightrein.collect.api_fuzz.schemathesis import config_writer, invoke, report_parser
from tightrein.protocol.process import Outcome, SubprocessRunner
from tightrein.protocol.security import Redactor
from tightrein.settings.load import Settings

DEFAULTS = Path(__file__).resolve().parents[4] / "settings" / "defaults.json"


def params(**changes):
    values = {"max_examples": 50, "phases": ("examples", "coverage", "fuzzing"), "include_methods": ("GET",),
              "include_paths": ("/api/Company",), "exclude_regex": "^/api/Auth/", "seed": 42, "workers": 1,
              "sanitize_keys": (), "timeout_s": 60.0}
    values.update(changes)
    return invoke.RunParameters(**values)


def test_command_line_puts_the_config_file_before_run(tmp_path):
    report = tmp_path / "report"
    argv = invoke.build_argv(params(), Path("/venv/bin/schemathesis"), Path("/specs/openapi.json"),
                             Path("/raw/schemathesis.toml"), "https://staging.example.test", report)
    assert argv[:4] == ("/venv/bin/schemathesis", "--config-file", "/raw/schemathesis.toml", "run")
    assert argv[argv.index("--checks") + 1] == "not_a_server_error"
    assert argv[argv.index("--report") + 1] == "junit,vcr,ndjson"
    assert argv[argv.index("--report-ndjson-path") + 1] == str(report / "events.ndjson")
    assert argv[-2:] == ("--seed", "42")
    assert ("--include-method", "GET") == argv[argv.index("--include-method"):argv.index("--include-method") + 2]
    bare = invoke.build_argv(params(include_methods=(), include_paths=(), exclude_regex=None), Path("st"),
                             Path("o.json"), Path("c.toml"), "http://h", report)
    assert "--include-method" not in bare and "--include-path" not in bare and "--exclude-path-regex" not in bare


def test_parameters_come_from_the_settings_and_the_restriction():
    defaults = json.loads(DEFAULTS.read_text(encoding="utf-8"))
    settings = Settings.from_data(defaults, {"controls": {"collect.api_fuzz": {
        "exclude": [r"^/api/Auth/", "Alipay"], "workers": 4, "sanitizeKeys": ["X-Key"], "fuzzTimeout": "5m"}}})
    built = invoke.parameters(settings, Restriction(("GET",), ("/api/items",)), lambda: 7)
    assert built.exclude_regex == "(?:^/api/Auth/)|(?:Alipay)" and built.workers == 4 and built.timeout_s == 300
    assert (built.include_methods, built.include_paths, built.seed) == (("GET",), ("/api/items",), 7)
    assert built.sanitize_keys == ("X-Key",)


def test_the_token_goes_only_through_the_environment():
    environ = {"PATH": "/bin", "GH_TOKEN": "secret", "HOME": "/Users/me"}
    env = invoke.environment(environ, "tok")
    assert env["TIGHTREIN_TOKEN"] == "tok" and "GH_TOKEN" not in env
    assert "TIGHTREIN_TOKEN" not in invoke.environment(environ, "")


class FakeRunner:
    def __init__(self, outcome):
        self.outcome = outcome
        self.commands = []

    def run(self, command):
        self.commands.append(command)
        return self.outcome


@pytest.mark.parametrize(("outcome", "completed", "problem"), [
    (Outcome(0, "", "", 1, None, None), True, None),
    (Outcome(1, "", "", 1, None, None), True, None),
    (Outcome(2, "", "", 1, None, None), False, "Schemathesis 退出码 2(配置或接口描述错误)，见 schemathesis.log"),
    (Outcome(None, "", "", 1, "timeout", None), False, "Schemathesis 被终止(timeout)"),
    (Outcome(None, "", "", 0, None, "FileNotFoundError"), False, "Schemathesis 无法启动：FileNotFoundError"),
])
def test_exit_codes(tmp_path, outcome, completed, problem):
    runner = FakeRunner(outcome)
    result = invoke.run(runner, params(), token="tok", spec_path=tmp_path / "openapi.json",
                        config_file=tmp_path / "c.toml", base_url="http://h", report_dir=tmp_path / "report",
                        environ={"PATH": "/bin"}, command=Path("/venv/bin/schemathesis"))
    command = runner.commands[0]
    assert (command.cwd, command.timeout_s, command.stdout_path) == (tmp_path / "report", 60.0, None)
    assert (tmp_path / "report" / "schemathesis.log").is_file()
    assert "tok" not in " ".join(command.argv) and command.env["TIGHTREIN_TOKEN"] == "tok"
    assert (result.completed, result.problem()) == (completed, problem)


def test_the_log_is_redacted_before_it_is_written(tmp_path):
    redactor = Redactor()
    redactor.register("db-password")
    stdout = "GET /api/items Authorization: Bearer tok-123\nconnect db-password\n"
    invoke.run(FakeRunner(Outcome(0, stdout, "", 1, None, None)), params(), token="tok-123",
               spec_path=tmp_path / "openapi.json", config_file=tmp_path / "c.toml", base_url="http://h",
               report_dir=tmp_path / "report", environ={"PATH": "/bin"}, command=Path("/venv/bin/schemathesis"),
               redactor=redactor)
    log = (tmp_path / "report" / invoke.LOG_FILE).read_text(encoding="utf-8")
    assert "tok-123" not in log and "db-password" not in log and log.startswith("GET /api/items")


def test_installed_version_is_checked(tmp_path):
    command = tmp_path / "schemathesis"
    command.write_text("", encoding="utf-8")

    def missing(name):
        raise metadata.PackageNotFoundError(name)

    assert invoke.installed_problem(lambda name: "4.28.0", command) is None
    assert invoke.installed_problem(lambda name: "4.1.0", command).startswith("Schemathesis 版本为 4.1.0")
    assert invoke.installed_problem(missing, command).startswith("Schemathesis 未安装")
    assert "找不到 schemathesis 命令" in invoke.installed_problem(lambda name: "4.28.0", tmp_path / "none")


@pytest.mark.external
def test_a_real_schemathesis_run_reports_server_errors(tmp_path):
    """锁定版本的 Schemathesis 对本机桩服务跑一次：配置文件被接受、凭证从环境变量展开、5xx 进了 NDJSON 报告。"""
    if invoke.installed_problem() is not None:
        pytest.skip("Schemathesis 没有按锁定版本安装")
    seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.headers.get("Authorization", ""))
            status = 500 if self.path.startswith("/api/boom") else 200
            body = b'{"error": "boom"}' if status == 500 else b"[]"
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def not_allowed(self):
            self.send_response(405)
            self.send_header("Allow", "GET")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def __getattr__(self, name):
            # Schemathesis 的 coverage 阶段会发 TRACE、QUERY 等未声明的方法，期望 405；http.server 对没有 do_<方法>
            # 的请求回 501，会被当成服务端报错
            if name.startswith("do_"):
                return self.not_allowed
            raise AttributeError(name)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        spec = tmp_path / "openapi.json"
        spec.write_text(json.dumps({"openapi": "3.0.1", "info": {"title": "stub", "version": "1"}, "paths": {
            "/api/boom": {"get": {"responses": {"200": {"description": "ok"}}}},
            "/api/fine": {"get": {"responses": {"200": {"description": "ok"}}}}}}), encoding="utf-8")
        config = config_writer.write(tmp_path / "schemathesis.toml", 1, [], ("Authorization", "Bearer "))
        result = invoke.run(SubprocessRunner(), params(include_methods=(), include_paths=(), exclude_regex=None,
                                                       max_examples=5, timeout_s=120.0),
                            token="tok-123", spec_path=spec, config_file=config,
                            base_url=f"http://127.0.0.1:{server.server_port}", report_dir=tmp_path / "report",
                            environ={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})
    finally:
        server.shutdown()
    assert result.completed, result.problem()
    assert "Bearer tok-123" in seen
    report = report_parser.parse(tmp_path / "report" / invoke.EVENTS_FILE, 200)
    assert report.complete
    assert {(failure.check, failure.operation) for failure in report.failures} == {
        ("not_a_server_error", ("GET", "/api/boom"))}
