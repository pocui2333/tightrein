import pytest
from contract_samples import changed, paths, without

from tightrein.contracts import validate

REQUEST = "extension/extension-request.schema.json"
RESPONSE = "extension/extension-response.schema.json"
STACK = "extension/stack-manifest.schema.json"


def point_schema(point, part):
    return f"extension/points/{point}.{part}.schema.json"


def request(point="spec-export", **fields):
    document = {
        "protocol": 1,
        "point": point,
        "workspace": "/w/sample",
        "repo": "/w/sample/worktrees/readonly",
        "commit": "d6f37025",
        "options": {"project": "src/App/App.csproj"},
        "scratchDir": "/tmp/tightrein-ext-1",
        "base": None,
        "input": {"outputFile": "/w/sample/data/specs/d6f37025/openapi.json"},
    }
    document.update(fields)
    return document


SPEC_OUTPUT = {"specFile": "/w/sample/data/specs/d6f37025/openapi.json", "format": "openapi-3.0", "operationCount": 42,
               "tool": {"name": "Spec.Export.Cli", "version": "5.5.1"}, "logFile": None}
ENDPOINTS_OUTPUT = {
    "endpoints": [
        {"method": "POST", "route": "/api/Order/Query", "requires": ["OrderRead"], "anonymous": False,
         "symbol": "OrderController.Query", "sourceFile": "src/Controllers/OrderController.cs"},
        {"method": "POST", "route": "/api/Account/Login", "requires": [], "anonymous": True,
         "symbol": "AccountController.Login", "sourceFile": None},
    ],
    "unresolved": [{"symbol": "LegacyController.Get", "reason": "路由由约定生成"}],
}
ROLES_OUTPUT = {"capabilities": ["OrderRead", "OrderWrite"],
                "roles": {"Company": {"OrderRead": True, "OrderWrite": False}, "Personal": {"OrderRead": False,
                                                                                            "OrderWrite": False}},
                "sourceFiles": ["src/Auth/PermissionMatrix.cs"]}
CHUNK = {"stream": "APP-1.out.log", "text": "02:15:03 fail: A[0]\n", "startPosition": 0, "endPosition": 20,
         "modifiedAt": "2026-09-29T02:15:04Z"}
WINDOW = {"since": "2026-09-29T02:00:00Z", "until": "2026-09-29T02:15:00Z"}
LOG_INPUT = {"query": '{app="api"} | json | level="error"', **WINDOW, "limit": 5000}
LOG_OUTPUT = {"chunks": [CHUNK], "truncated": False, "oldestAvailable": "2026-08-30T02:15:00Z"}
ISSUE = {"group": "sentry:acme/4512", "kind": "frontend", "title": "TypeError: x is undefined", "type": "TypeError",
         "message": "x is undefined", "culprit": "src/app.js in render", "level": "error", "count": 12, "userCount": 3,
         "firstSeen": "2026-09-28T02:00:00Z", "lastSeen": "2026-09-29T02:10:00Z", "release": "web@1.4.0",
         "environment": "production", "permalink": "https://sentry.io/organizations/acme/issues/4512/",
         "frames": [{"file": "src/app.js", "function": "render", "line": 42, "inApp": True},
                    {"file": "node_modules/vue/index.js", "function": None, "line": None, "inApp": False}],
         "breadcrumbs": [{"time": "2026-09-29T02:09:58Z", "category": "ui.click", "message": "button#save"}],
         "url": "https://app.example.test/orders", "browser": "Chrome 128"}
TRACKING_OUTPUT = {"issues": [ISSUE], "truncated": False, "oldestAvailable": None}
ALERT = {"fingerprint": "9f2c0a1b2c3d4e5f", "name": "OrdersStalled", "labels": {"alertname": "OrdersStalled",
         "severity": "critical"}, "annotations": {"summary": "订单 1 小时没有进展"}, "startsAt": "2026-09-29T02:00:00Z",
         "generatorUrl": None}
ENTRY = {"stream": "APP-1.out.log", "position": 0, "occurredAt": "2026-09-28T18:15:03Z", "localTime": "02:15:03",
         "level": "error", "rawLevel": "fail", "category": "OrderService", "eventId": 0, "message": "查询失败",
         "exception": {"type": "System.NullReferenceException", "message": "Object reference not set"},
         "frames": [{"symbol": "App.Services.OrderService.Query", "file": "/src/OrderService.cs", "line": 88,
                     "isProject": True},
                    {"symbol": "System.Linq.Enumerable.First", "file": None, "line": None, "isProject": False}],
         "raw": "02:15:03 fail: OrderService[0] 查询失败"}
PARSE_OUTPUT = {"entries": [ENTRY], "state": {"APP-1.out.log": "2026-09-29T02:15:03"}, "unparsed": 1}
TOOLS_INPUT = {"level": "incremental", "baseCommit": "a1b2c3d", "changedFiles": ["src/Services/OrderService.cs"],
               "rawDir": "/w/sample/data/runs/R-20260929-021503-collect-static/raw/static"}
TOOLS_OUTPUT = {
    "tools": [{"name": "dotnet-build", "status": "ok", "exitCode": 0, "logFile": "dotnet-build.log", "reason": None},
              {"name": "npm-audit", "status": "skipped", "exitCode": None, "logFile": None, "reason": "前端没有改动"}],
    "findings": [
        {"tool": "dotnet-build", "kind": "build-warning", "rule": "CS8602", "file": "src/Services/OrderService.cs",
         "line": 88, "column": 13, "message": "Dereference of a possibly null reference.", "severity": "low",
         "package": None},
        {"tool": "dotnet-vulnerable", "kind": "vulnerability", "rule": "GHSA-5crp-9r3c-p9vr",
         "file": "src/App/App.csproj", "line": None, "column": None, "message": "Newtonsoft.Json 12.0.1",
         "severity": "high", "package": {"name": "Newtonsoft.Json", "version": "12.0.1", "advisoryUrl": None}},
    ],
}
ROUTES_OUTPUT = {"routes": [{"path": "/newhome/CompanyDetail/:id", "name": "CompanyDetail",
                             "componentFile": "src/vue/src/views/CompanyDetail.vue", "meta": {"auth": True}},
                            {"path": "/login", "name": None, "componentFile": None, "meta": None}],
                 "sourceFiles": ["src/vue/src/router/index.js"]}
RUN_INPUT = {"mode": "page", "ports": {"backend": 5000, "frontend": 8080}}
SERVICE = {"name": "backend", "argv": ["dotnet", "run", "--project", "src/App/App.csproj"], "cwd": ".",
           "env": {"APP_ENVIRONMENT": "Development"}, "port": 5000, "readyUrl": "http://localhost:5000/",
           "readyPatterns": ["Now listening on"], "failPatterns": ["Unhandled exception"], "after": None}
RUN_OUTPUT = {"services": [SERVICE, changed(SERVICE, name="frontend", argv=["npm", "run", "serve"], cwd="src/vue",
                                            env={}, port=8080, after="backend")],
              "unavailable": [{"name": "compute", "reason": "计算服务不在本机启动"}],
              "migrationPaths": ["src/**/Migrations/MigrationList.cs"]}
MANIFEST = {
    "name": "webstack",
    "version": "1.0.0",
    "requires": [{"tool": "dotnet", "minVersion": "8.0", "install": "brew install dotnet"}],
    "env": {"DOTNET_CLI_TELEMETRY_OPTOUT": "1", "DOTNET_NOLOGO": "1"},
    "defaults": {"spec-export": "swashbuckle-cli", "log-parse": "dotnet-console"},
}

DEPLOYMENT = {"id": "101", "commit": "a" * 40, "status": "succeeded", "environment": "staging",
              "url": "https://ci.example.test/runs/101", "createdAt": "2026-10-05T03:00:00Z"}

VALID = [
    (REQUEST, request()),
    (REQUEST, request(point="log-platform", repo=None, commit=None, input=LOG_INPUT)),
    (REQUEST, request(point="error-tracking", repo=None, commit=None, input=WINDOW)),
    (REQUEST, request(point="alert-source", repo=None, commit=None, input={})),
    (REQUEST, request(point="log-parse", repo=None, commit=None, base=PARSE_OUTPUT,
                      input={"chunks": [CHUNK], "state": None})),
    (RESPONSE, {"protocol": 1, "status": "ok", "output": SPEC_OUTPUT, "notes": ["3 个端点无法解析路由"]}),
    (RESPONSE, {"protocol": 1, "status": "error",
                "error": {"code": "tool-missing", "message": "未找到 dotnet", "hint": "安装 .NET SDK 8"}}),
    (STACK, MANIFEST),
    (STACK, without(MANIFEST, "requires", "env", "defaults")),
    (point_schema("spec-export", "input"), {"outputFile": "/w/sample/data/specs/d6f37025/openapi.json"}),
    (point_schema("spec-export", "output"), SPEC_OUTPUT),
    (point_schema("authz-endpoints", "input"), {}),
    (point_schema("authz-endpoints", "output"), ENDPOINTS_OUTPUT),
    (point_schema("authz-roles", "input"), {"roles": ["Company", "Personal"]}),
    (point_schema("authz-roles", "output"), ROLES_OUTPUT),
    (point_schema("error-tracking", "input"), WINDOW),
    (point_schema("error-tracking", "output"), TRACKING_OUTPUT),
    (point_schema("error-tracking", "output"), {"issues": [changed(ISSUE, kind="backend", frames=[], breadcrumbs=[],
                                                                   url=None, browser=None)],
                                                "truncated": True, "oldestAvailable": "2026-07-01T00:00:00Z"}),
    (point_schema("log-platform", "input"), LOG_INPUT),
    (point_schema("log-platform", "output"), LOG_OUTPUT),
    (point_schema("log-platform", "output"), {"chunks": [], "truncated": True, "oldestAvailable": None}),
    (point_schema("alert-source", "input"), {}),
    (point_schema("alert-source", "output"), {"alerts": [ALERT]}),
    (point_schema("log-parse", "input"), {"chunks": [CHUNK], "state": {"APP-1.out.log": "2026-09-29T02:15:03"}}),
    (point_schema("log-parse", "output"), PARSE_OUTPUT),
    (point_schema("log-parse", "output"), changed(PARSE_OUTPUT, entries=[changed(ENTRY, exception=None, frames=[],
                                                                                  category=None, eventId=None)])),
    (point_schema("static-tools", "input"), TOOLS_INPUT),
    (point_schema("static-tools", "input"), changed(TOOLS_INPUT, level="full", baseCommit=None, changedFiles=[])),
    (point_schema("static-tools", "output"), TOOLS_OUTPUT),
    (point_schema("page-routes", "input"), {}),
    (point_schema("page-routes", "output"), ROUTES_OUTPUT),
    (point_schema("local-run", "input"), RUN_INPUT),
    (point_schema("local-run", "input"), {"mode": "api", "ports": {"backend": 5100, "frontend": None}}),
    (point_schema("local-run", "output"), RUN_OUTPUT),
    (point_schema("deploy-source", "input"), {"branch": "main"}),
    (point_schema("deploy-source", "output"), {"deployments": [DEPLOYMENT]}),
    (point_schema("deploy-source", "output"), {"deployments": []}),
]

INVALID = [
    (point_schema("deploy-source", "input"), {}, "$"),
    (point_schema("deploy-source", "output"), {"deployments": [changed(DEPLOYMENT, status="done")]},
     "$.deployments[0].status"),
    (REQUEST, request(protocol=2), "$.protocol"),
    (REQUEST, request(point="swagger"), "$.point"),
    (REQUEST, request(repo="worktrees/readonly"), "$.repo"),
    (REQUEST, request(commit=None), "$.commit"),
    (REQUEST, request(point="log-platform", input=LOG_INPUT), "$.repo"),
    (REQUEST, without(request(), "scratchDir"), "$"),
    (RESPONSE, {"protocol": 1, "status": "ok"}, "$"),
    (RESPONSE, {"protocol": 1, "status": "error", "error": {"code": "timeout", "message": "超时"}}, "$.error.code"),
    (RESPONSE, {"protocol": 1, "status": "done", "output": {}}, "$.status"),
    (STACK, without(MANIFEST, "version"), "$"),
    (STACK, changed(MANIFEST, points={"log-parse": {"command": ["x"]}}), "$"),
    (STACK, changed(MANIFEST, defaults={"swagger": "swashbuckle-cli"}), "$.defaults"),
    (STACK, changed(MANIFEST, defaults={"log-parse": "Dotnet Console"}), "$.defaults['log-parse']"),
    (point_schema("spec-export", "input"), {"outputFile": "openapi.json"}, "$.outputFile"),
    (point_schema("spec-export", "output"), changed(SPEC_OUTPUT, format="raml"), "$.format"),
    (point_schema("authz-endpoints", "input"), {"project": "a"}, "$"),
    (point_schema("authz-endpoints", "output"),
     changed(ENDPOINTS_OUTPUT, endpoints=[without(ENDPOINTS_OUTPUT["endpoints"][0], "requires")]), "$.endpoints[0]"),
    (point_schema("authz-endpoints", "output"),
     changed(ENDPOINTS_OUTPUT, endpoints=[changed(ENDPOINTS_OUTPUT["endpoints"][0], sourceFile="C:\\src\\A.cs")]),
     "$.endpoints[0].sourceFile"),
    (point_schema("authz-roles", "input"), {"roles": []}, "$.roles"),
    (point_schema("authz-roles", "output"), changed(ROLES_OUTPUT, roles={"Company": {"OrderRead": "yes"}}),
     "$.roles.Company.OrderRead"),
    (point_schema("error-tracking", "input"), changed(WINDOW, since="2026-09-29 02:00"), "$.since"),
    (point_schema("error-tracking", "output"), changed(TRACKING_OUTPUT, issues=[changed(ISSUE, group="4512")]),
     "$.issues[0].group"),
    (point_schema("log-platform", "input"), changed(LOG_INPUT, limit=0), "$.limit"),
    (point_schema("log-platform", "output"), changed(LOG_OUTPUT, chunks=[without(CHUNK, "modifiedAt")]),
     "$.chunks[0]"),
    (point_schema("alert-source", "input"), {"since": "x"}, "$"),
    (point_schema("alert-source", "output"), {"alerts": [without(ALERT, "fingerprint")]}, "$.alerts[0]"),
    (point_schema("log-parse", "input"), {"chunks": [CHUNK]}, "$"),
    (point_schema("log-parse", "output"), changed(PARSE_OUTPUT, entries=[changed(ENTRY, level="fail")]),
     "$.entries[0].level"),
    (point_schema("static-tools", "input"), changed(TOOLS_INPUT, baseCommit=None), "$.baseCommit"),
    (point_schema("static-tools", "output"),
     changed(TOOLS_OUTPUT, tools=[changed(TOOLS_OUTPUT["tools"][1], reason=None)]), "$.tools[0].reason"),
    (point_schema("static-tools", "output"),
     changed(TOOLS_OUTPUT, findings=[changed(TOOLS_OUTPUT["findings"][1], package=None)]), "$.findings[0].package"),
    (point_schema("page-routes", "input"), {"routerFile": "a"}, "$"),
    (point_schema("page-routes", "output"), changed(ROUTES_OUTPUT, routes=[changed(ROUTES_OUTPUT["routes"][0],
                                                                                   path="newhome")]),
     "$.routes[0].path"),
    (point_schema("local-run", "input"), {"mode": "page", "ports": {"backend": 5000, "frontend": None}},
     "$.ports.frontend"),
    (point_schema("local-run", "input"), {"mode": "api", "ports": {"backend": 5100, "frontend": 8080}},
     "$.ports.frontend"),
    (point_schema("local-run", "output"), changed(RUN_OUTPUT, services=[changed(SERVICE, argv=[])]),
     "$.services[0].argv"),
    (point_schema("local-run", "output"), changed(RUN_OUTPUT, services=[changed(SERVICE, env={"PORT": 5000})]),
     "$.services[0].env.PORT"),
]


@pytest.mark.parametrize("name,instance", VALID)
def test_valid_samples(name, instance):
    assert paths(name, instance) == set()


@pytest.mark.parametrize("name,instance,path", INVALID)
def test_invalid_samples(name, instance, path):
    assert path in paths(name, instance)


def test_every_extension_schema_has_samples():
    names = {name for name in validate.names() if name.startswith("extension/points/")}
    assert names == {name for name, _ in VALID if name.startswith("extension/points/")}
    assert names == {name for name, _, _ in INVALID if name.startswith("extension/points/")}
