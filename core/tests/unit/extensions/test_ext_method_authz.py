import json

import pytest
from method_world import COMMIT, call, error_of, output_of, request

from tightrein.domain.enums import ExtensionPoint
from tightrein.extensions.methods.authz_endpoints import manual_list, openapi_security
from tightrein.extensions.methods.authz_roles import manual_matrix
from tightrein.store.files.layout import WorkspaceLayout

SPEC = {
    "openapi": "3.0.1",
    "security": [{"bearer": ["OrderRead"]}],
    "paths": {
        "/api/orders": {
            "get": {"operationId": "listOrders"},
            "post": {"operationId": "createOrder", "security": [{"bearer": ["OrderRead", "OrderWrite"]}]},
        },
        "/api/login": {"post": {"security": []}},
        "/api/health": {"get": {"operationId": "health", "security": [{}, {"bearer": []}]}},
        "/api/reports": {"get": {"operationId": "reports", "security": [{"bearer": ["ReportRead"]},
                                                                      {"apiKey": ["ReportExport"]}]}},
        "/api/combined": {"get": {"operationId": "combined", "security": [{"bearer": ["A"], "apiKey": ["B"]}]}},
        "/api/broken": {"get": {"operationId": "broken", "security": [{"bearer": "OrderRead"}]}},
        "relative": {"get": {"operationId": "relative"}},
    },
}


def endpoints_request(tmp_path, options=None):
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    return request(ExtensionPoint.AUTHZ_ENDPOINTS, workspace=tmp_path, repo=repo, options=options)


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_security_requirements_become_required_capabilities(tmp_path):
    write(WorkspaceLayout(tmp_path).openapi(COMMIT), json.dumps(SPEC))
    response = call(openapi_security, endpoints_request(tmp_path))
    output = response["output"]
    assert [(item["method"], item["route"], item["requires"], item["anonymous"], item["symbol"])
            for item in output["endpoints"]] == [
        ("GET", "/api/orders", ["OrderRead"], False, "listOrders"),
        ("POST", "/api/orders", ["OrderRead", "OrderWrite"], False, "createOrder"),
        ("POST", "/api/login", [], True, "POST /api/login"),
        ("GET", "/api/health", [], True, "health"),
        ("GET", "/api/combined", ["A", "B"], False, "combined"),
    ]
    assert output["unresolved"] == [
        {"symbol": "reports", "reason": "security 有多种可选的要求，无法写成同时需要的能力"},
        {"symbol": "broken", "reason": "security 声明的写法无法识别"},
        {"symbol": "relative", "reason": "路径不是以 / 开头"},
    ]
    assert response["notes"] == ["3 个端点无法从 security 声明推出所需能力"]


def test_operations_without_any_security_are_not_guessed(tmp_path):
    write(tmp_path / "repo" / "api.yaml", "openapi: 3.0.1\npaths:\n  /api/items:\n    get: {}\n")
    output = output_of(openapi_security, endpoints_request(tmp_path, {"path": "api.yaml"}))
    assert output == {"endpoints": [], "unresolved": [{"symbol": "GET /api/items",
                                                       "reason": "接口描述中没有 security 声明"}]}


def test_a_missing_spec_is_not_applicable(tmp_path):
    code, message = error_of(openapi_security, endpoints_request(tmp_path))
    assert (code, message) == ("not-applicable", f"没有接口描述 {WorkspaceLayout(tmp_path).openapi(COMMIT)}")
    assert error_of(openapi_security, endpoints_request(tmp_path, {"path": "api.yaml"})) == (
        "not-applicable", "没有接口描述 api.yaml")


ENDPOINTS_YAML = """endpoints:
  - {method: GET, route: /api/orders, requires: [OrderRead]}
  - {method: POST, route: /api/login, anonymous: true, symbol: AuthController.Login,
     sourceFile: src/Auth/AuthController.cs}
"""


def test_the_manual_endpoint_list_fills_defaults(tmp_path):
    write(tmp_path / "authz" / "endpoints.yaml", ENDPOINTS_YAML)
    output = output_of(manual_list, endpoints_request(tmp_path, {"file": "authz/endpoints.yaml"}))
    assert output == {"unresolved": [], "endpoints": [
        {"method": "GET", "route": "/api/orders", "requires": ["OrderRead"], "anonymous": False,
         "symbol": "GET /api/orders", "sourceFile": None},
        {"method": "POST", "route": "/api/login", "requires": [], "anonymous": True,
         "symbol": "AuthController.Login", "sourceFile": "src/Auth/AuthController.cs"},
    ]}


@pytest.mark.parametrize(("text", "message"), [
    ("endpoints:\n  - {method: get, route: /api/orders}\n", "$.endpoints[0].method"),
    ("endpoints:\n  - {method: GET, route: api/orders}\n", "$.endpoints[0].route"),
    ("endpoints:\n  - {method: GET, route: /a}\n  - {method: GET, route: /a}\n", "GET /a 出现了两次"),
    ("routes: []\n", "'endpoints' is a required property"),
])
def test_manual_endpoint_list_problems(tmp_path, text, message):
    write(tmp_path / "authz" / "endpoints.yaml", text)
    code, found = error_of(manual_list, endpoints_request(tmp_path, {"file": "authz/endpoints.yaml"}))
    assert code == "parse-failed" and message in found


def test_a_missing_manual_file_is_a_configuration_problem(tmp_path):
    response = call(manual_list, endpoints_request(tmp_path, {"file": "authz/endpoints.yaml"}))
    assert response["error"] == {"code": "invalid-input", "message": "工作区中没有 authz/endpoints.yaml",
                                 "hint": "在工作区中创建该文件，或检查 extensions.authz-endpoints.options.file"}


MATRIX_YAML = """capabilities: [OrderRead, OrderWrite, ReportRead]
roles:
  Viewer: [OrderRead]
  Editor: [OrderRead, OrderWrite]
  Auditor: [ReportRead]
"""


def roles_request(tmp_path, roles, file="authz/roles.yaml"):
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    return request(ExtensionPoint.AUTHZ_ROLES, workspace=tmp_path, repo=repo, options={"file": file},
                   input={"roles": roles})


def test_the_matrix_answers_every_requested_role(tmp_path):
    write(tmp_path / "authz" / "roles.yaml", MATRIX_YAML)
    response = call(manual_matrix, roles_request(tmp_path, ["Viewer", "Editor", "Guest"]))
    assert response["output"] == {
        "capabilities": ["OrderRead", "OrderWrite", "ReportRead"],
        "roles": {"Viewer": {"OrderRead": True, "OrderWrite": False, "ReportRead": False},
                  "Editor": {"OrderRead": True, "OrderWrite": True, "ReportRead": False}},
        "sourceFiles": [],
    }
    assert response["notes"] == ["矩阵中没有角色 Guest，该角色不做越权检查"]


def test_unknown_capabilities_in_the_matrix_are_rejected(tmp_path):
    write(tmp_path / "authz" / "roles.yaml", "capabilities: [OrderRead]\nroles:\n  Viewer: [OrderRead, Export]\n")
    code, message = error_of(manual_matrix, roles_request(tmp_path, ["Viewer"]))
    assert (code, message) == ("parse-failed",
                               f"{tmp_path / 'authz' / 'roles.yaml'} 中角色 Viewer 的能力不在 capabilities 中：Export")
    assert error_of(manual_matrix, roles_request(tmp_path, ["Viewer"], file="../roles.yaml"))[0] == "invalid-input"
