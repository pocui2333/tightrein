import json

import pytest
from method_world import error_of, output_of, request

from tightrein.domain.enums import ExtensionPoint
from tightrein.extensions.catalog import core_methods
from tightrein.extensions.invoke import ProcessOutcome
from tightrein.extensions.methods.local_run import command_sequence
from tightrein.extensions.methods.page_routes import manual_list
from tightrein.extensions.methods.runtime import MethodContext
from tightrein.extensions.methods.static_tools import semgrep

SEMGREP_OUTPUT = {
    "version": "1.178.0",
    "results": [
        {"check_id": "rules.empty-catch", "path": "./src/orders/service.py", "start": {"line": 9, "col": 5},
         "extra": {"message": "异常被吞掉", "severity": "WARNING"}},
        {"check_id": "rules.no-eval", "path": "web/main.js", "start": {"line": 2, "col": 10},
         "extra": {"message": "不要使用 eval", "severity": "ERROR"}},
    ],
    "errors": [],
}


class FakeRunner:
    def __init__(self, outcome):
        self.outcome = outcome
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        return self.outcome


def tools_request(tmp_path, level="incremental", changed=("src/orders/service.py", "src/removed.py")):
    repo = tmp_path / "repo"
    (repo / "src" / "orders").mkdir(parents=True, exist_ok=True)
    (repo / "src" / "orders" / "service.py").write_text("pass\n", encoding="utf-8")
    base = "a1b2c3d" if level == "incremental" else None
    return request(ExtensionPoint.STATIC_TOOLS, workspace=tmp_path, repo=repo,
                   options={"configs": ["rules/local.yaml", "p/python"]},
                   input={"level": level, "baseCommit": base, "changedFiles": list(changed),
                          "rawDir": str(tmp_path / "raw" / "static")})


def test_semgrep_scans_the_changed_files_that_still_exist(tmp_path):
    runner = FakeRunner(ProcessOutcome(0, json.dumps(SEMGREP_OUTPUT).encode("utf-8"), b""))
    output = output_of(semgrep, tools_request(tmp_path), MethodContext(runner=runner, environ={"PATH": "/usr/bin"}))
    sent = runner.requests[0]
    assert sent.argv == ("semgrep", "scan", "--config", "rules/local.yaml", "--config", "p/python", "--json",
                         "--metrics=off", "src/orders/service.py")
    assert (sent.cwd, sent.timeout_seconds) == (tmp_path / "repo", 1500)
    assert sent.env["SEMGREP_SEND_METRICS"] == "off"
    assert output["tools"] == [{"name": "semgrep", "status": "ok", "exitCode": 0,
                                "logFile": "core-semgrep/semgrep.log", "reason": None}]
    assert [(item["file"], item["line"], item["severity"], item["kind"]) for item in output["findings"]] == [
        ("src/orders/service.py", 9, "medium", "lint"), ("web/main.js", 2, "high", "lint")]
    assert (tmp_path / "raw" / "static" / "core-semgrep" / "semgrep.json").is_file()


def test_semgrep_command_comes_from_the_extension_environment(tmp_path):
    runner = FakeRunner(ProcessOutcome(0, json.dumps({"results": [], "errors": []}).encode("utf-8"), b""))
    context = MethodContext(runner=runner, environ={"TIGHTREIN_SEMGREP": "/opt/local/semgrep/bin/semgrep"})
    output_of(semgrep, tools_request(tmp_path), context)
    assert runner.requests[0].argv[:2] == ("/opt/local/semgrep/bin/semgrep", "scan")


def test_full_scans_cover_the_whole_repository(tmp_path):
    runner = FakeRunner(ProcessOutcome(0, json.dumps({"results": [], "errors": []}).encode("utf-8"), b""))
    output_of(semgrep, tools_request(tmp_path, level="full"), MethodContext(runner=runner))
    assert runner.requests[0].argv[-1] == "."


def test_failed_and_skipped_semgrep_runs_are_reported_per_tool(tmp_path):
    runner = FakeRunner(ProcessOutcome(2, b"", b"invalid rule"))
    failed = output_of(semgrep, tools_request(tmp_path), MethodContext(runner=runner))
    assert (failed["tools"][0]["status"], failed["tools"][0]["exitCode"], failed["findings"]) == ("failed", 2, [])
    assert failed["tools"][0]["reason"] == "Semgrep 失败：退出码 2"
    skipped = output_of(semgrep, tools_request(tmp_path, changed=("src/removed.py",)),
                        MethodContext(runner=FakeRunner(ProcessOutcome(0, b"", b""))))
    assert skipped["tools"] == [{"name": "semgrep", "status": "skipped", "exitCode": None, "logFile": None,
                                 "reason": "扫描范围内没有文件，不运行 Semgrep"}]


ROUTES_YAML = """routes:
  - {path: /orders, name: orders, componentFile: src/views/Orders.vue, meta: {title: 订单}}
  - {path: '/orders/:id'}
"""


def routes_request(tmp_path, file="e2e/routes.yaml"):
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    return request(ExtensionPoint.PAGE_ROUTES, workspace=tmp_path, repo=repo, options={"file": file})


def test_the_manual_route_list_fills_defaults(tmp_path):
    (tmp_path / "e2e").mkdir()
    (tmp_path / "e2e" / "routes.yaml").write_text(ROUTES_YAML, encoding="utf-8")
    assert output_of(manual_list, routes_request(tmp_path)) == {"sourceFiles": [], "routes": [
        {"path": "/orders", "name": "orders", "componentFile": "src/views/Orders.vue", "meta": {"title": "订单"}},
        {"path": "/orders/:id", "name": None, "componentFile": None, "meta": None},
    ]}


@pytest.mark.parametrize(("text", "message"), [
    ("routes:\n  - {path: orders}\n", "$.routes[0].path"),
    ("routes:\n  - {path: /a}\n  - {path: /a}\n", "路由 /a 出现了两次"),
])
def test_manual_route_list_problems(tmp_path, text, message):
    (tmp_path / "e2e").mkdir()
    (tmp_path / "e2e" / "routes.yaml").write_text(text, encoding="utf-8")
    code, found = error_of(manual_list, routes_request(tmp_path))
    assert code == "parse-failed" and message in found
    assert error_of(manual_list, routes_request(tmp_path, "e2e/missing.yaml"))[0] == "invalid-input"


SERVICES = [
    {"name": "api", "argv": ["python", "-m", "demo.api", "--port", "{port}"], "port": "backend",
     "env": {"APP_MODE": "local"}, "readyPath": "/health", "readyPatterns": ["listening on"],
     "failPatterns": ["Traceback"]},
    {"name": "web", "argv": ["npm", "run", "dev", "--", "--port", "{port}"], "cwd": "web", "port": "frontend",
     "env": {"API_PORT": "{backendPort}"}},
]
OPTIONS = {"services": SERVICES, "unavailable": [{"name": "search", "reason": "搜索服务不在本机启动"}],
           "migrationPaths": ["db/migrations/**"]}


def plan_request(tmp_path, mode, ports, options=None):
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    return request(ExtensionPoint.LOCAL_RUN, workspace=tmp_path, repo=repo, options=options or OPTIONS,
                   input={"mode": mode, "ports": ports})


def test_api_mode_starts_only_the_backend_services(tmp_path):
    output = output_of(command_sequence, plan_request(tmp_path, "api", {"backend": 7100, "frontend": None}))
    assert output == {
        "services": [{"name": "api", "argv": ["python", "-m", "demo.api", "--port", "7100"], "cwd": ".",
                      "env": {"APP_MODE": "local"}, "port": 7100, "readyUrl": "http://localhost:7100/health",
                      "readyPatterns": ["listening on"], "failPatterns": ["Traceback"], "after": None}],
        "unavailable": [{"name": "search", "reason": "搜索服务不在本机启动"}],
        "migrationPaths": ["db/migrations/**"],
    }


def test_page_mode_starts_the_services_one_after_another(tmp_path):
    output = output_of(command_sequence, plan_request(tmp_path, "page", {"backend": 7000, "frontend": 7080}))
    assert [(item["name"], item["port"], item["after"], item["readyUrl"]) for item in output["services"]] == [
        ("api", 7000, None, "http://localhost:7000/health"), ("web", 7080, "api", None)]
    web = output["services"][1]
    assert (web["argv"][-1], web["cwd"], web["env"]) == ("7080", "web", {"API_PORT": "7000"})


@pytest.mark.parametrize(("services", "mode", "message"), [
    ([dict(SERVICES[0]), dict(SERVICES[0])], "api", "服务名重复：api"),
    ([dict(SERVICES[0], argv=["serve", "--web", "{frontendPort}"])], "api",
     "服务 api 用到了 {frontendPort}，本模式没有这个端口"),
    ([dict(SERVICES[0], after="web"), SERVICES[1]], "api", "服务 api 的 after 为 web，api 模式中没有这个服务"),
])
def test_plans_that_cannot_be_built(tmp_path, services, mode, message):
    ports = {"backend": 7100, "frontend": None}
    document = plan_request(tmp_path, mode, ports, dict(OPTIONS, services=services))
    assert error_of(command_sequence, document) == ("invalid-input", message)


def test_the_core_catalog_lists_every_core_method():
    assert [(method.point.value, method.id) for method in core_methods()] == [
        ("spec-export", "core/openapi-file"), ("spec-export", "core/openapi-url"),
        ("authz-endpoints", "core/manual-list"), ("authz-endpoints", "core/openapi-security"),
        ("authz-roles", "core/manual-matrix"),
        ("error-tracking", "core/sentry"), ("log-platform", "core/loki"),
        ("log-parse", "core/json-lines"), ("log-parse", "core/regex"),
        ("alert-source", "core/alertmanager"),
        ("static-tools", "core/semgrep"),
        ("page-routes", "core/manual-list"),
        ("local-run", "core/command-sequence"),
        ("deploy-source", "core/github-actions"), ("deploy-source", "core/github-deployments"),
        ("deploy-source", "core/vercel"),
    ]
