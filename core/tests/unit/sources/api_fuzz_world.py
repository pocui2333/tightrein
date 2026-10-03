"""api-fuzz 测试共用的接口描述、扩展输出与假扩展客户端。"""

import json

from tightrein.domain.enums import ExtensionErrorCode, ExtensionLayer, ExtensionPoint
from tightrein.extensions.client import WorktreeNotAtCommit
from tightrein.extensions.result import ExtensionFailure, PointResult

SPEC = {
    "openapi": "3.0.1",
    "info": {"title": "demo", "version": "V1"},
    "paths": {
        "/api/Order/{id}": {"get": {"responses": {"200": {"description": "ok"}}}},
        "/api/Order/Query": {"post": {"responses": {"200": {"description": "ok"}}}},
        "/api/Company": {"get": {"responses": {"200": {"description": "ok"}}},
                         "post": {"responses": {"200": {"description": "ok"}}}},
        "/api/Auth/Login": {"post": {"responses": {"200": {"description": "ok"}}}},
        "/api/User/List": {"get": {"responses": {"200": {"description": "ok"}}}},
    },
}

ENDPOINTS = {
    "endpoints": [
        {"method": "GET", "route": "/api/Order/{id}", "requires": ["CanViewOrders"], "anonymous": False,
         "symbol": "OrderController.Get", "sourceFile": "src/Controllers/OrderController.cs"},
        {"method": "POST", "route": "/api/Order/Query", "requires": ["CanViewOrders"], "anonymous": False,
         "symbol": "OrderController.Query", "sourceFile": "src/Controllers/OrderController.cs"},
        {"method": "GET", "route": "/api/company", "requires": [], "anonymous": False,
         "symbol": "CompanyController.List", "sourceFile": None},
        {"method": "POST", "route": "/api/Company", "requires": ["CanEditCompanies"], "anonymous": False,
         "symbol": "CompanyController.Create", "sourceFile": None},
        {"method": "POST", "route": "/api/Auth/Login", "requires": [], "anonymous": True,
         "symbol": "AuthController.Login", "sourceFile": None},
        {"method": "GET", "route": "/api/User/List", "requires": ["CanManageUsers", "CanSeeEverything"],
         "anonymous": False, "symbol": "UserController.List", "sourceFile": None},
        {"method": "DELETE", "route": "/api/Legacy/{id}", "requires": [], "anonymous": False,
         "symbol": "LegacyController.Delete", "sourceFile": None},
    ],
    "unresolved": [{"symbol": "ReportController.Export", "reason": "路由由约定生成"}],
}

ROLES = {
    "capabilities": ["CanViewOrders", "CanEditCompanies", "CanManageUsers"],
    "roles": {
        "Company": {"CanViewOrders": True, "CanEditCompanies": False, "CanManageUsers": False},
        "Admin": {"CanViewOrders": False, "CanEditCompanies": True, "CanManageUsers": True},
    },
    "sourceFiles": ["src/auth/roles.json"],
}


def ok(point, output, layer=ExtensionLayer.STACK, notes=(), cached=False):
    return PointResult(point, layer, output=output, notes=tuple(notes), cached=cached)


def default(point, notes=("核心默认",)):
    return PointResult(point, ExtensionLayer.DEFAULT, notes=tuple(notes))


def error(point, code=ExtensionErrorCode.BUILD_FAILED, message="构建失败", hint="查看 spec-export.log"):
    return PointResult(point, ExtensionLayer.STACK, failure=ExtensionFailure(code, message, hint))


def write_spec(directory, document=SPEC):
    path = directory / "openapi.json"
    directory.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def spec_result(path):
    return ok(ExtensionPoint.SPEC_EXPORT, {"specFile": str(path), "format": "openapi-3.0", "operationCount": 6,
                                          "tool": {"name": "swashbuckle", "version": "5.5.1"}, "logFile": None})


class FakeClient:
    """按扩展点返回预设结果；预设为 WorktreeNotAtCommit 时抛出。"""

    def __init__(self, needs_repo=True, spec_method="core/openapi-url", **results):
        self.results = results
        self.calls = []
        self.repo_needed = needs_repo
        self.spec_method = spec_method

    def needs_repo(self, point):
        return self.repo_needed

    def method(self, point):
        return self.spec_method

    def _answer(self, name, *args):
        self.calls.append((name, *args))
        result = self.results[name]
        if result is WorktreeNotAtCommit:
            raise WorktreeNotAtCommit(args[0], args[1], "0" * 40)
        return result

    def spec_export(self, repo, commit):
        return self._answer("spec_export", repo, commit)

    def authz_endpoints(self, repo, commit):
        return self._answer("authz_endpoints", repo, commit)

    def authz_roles(self, repo, commit, roles):
        return self._answer("authz_roles", repo, commit, tuple(roles))

    def page_routes(self, repo, commit):
        return self._answer("page_routes", repo, commit)
