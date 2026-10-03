"""api-fuzz 的端到端集成测试：在本机启动带 OpenAPI 描述的桩服务(api_fuzz_stub.py)，运行真实的 Schemathesis。

锁定版本的 Schemathesis 未安装时跳过。越权模型由假扩展客户端给出：Company 缺少 CanManageUsers 与
CanViewCompany，GET /api/User/List 返回 200 判为越权，GET /api/Company/{id} 的 403 是预期的拒绝。
"""

import json
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest

from tightrein.domain.clock import SystemClock
from tightrein.domain.enums import ExtensionLayer, ExtensionPoint, ProbeLevel, RunStatus
from tightrein.extensions.result import PointResult
from tightrein.observability.redact import Redactor
from tightrein.sources.api_fuzz import invoke
from tightrein.sources.api_fuzz.probe import ApiFuzzDependencies, ApiFuzzProbe
from tightrein.sources.base import ProbeOptions, ProbeTarget
from tightrein.sources.common.http import UrllibTransport
from tightrein.sources.common.procs import SubprocessLauncher
from tightrein.sources.common.redact import ProbeRedactor
from tightrein.sources.common.session import LoginSettings, Session
from tightrein.store.files.layout import WorkspaceLayout

STUB = Path(__file__).parent / "api_fuzz_stub.py"
RELEASE = "d6f37025a1b2c3d4e5f60718293a4b5c6d7e8f90"

pytestmark = pytest.mark.skipif(invoke.installed_problem() is not None, reason=str(invoke.installed_problem()))


@pytest.fixture
def stub(monkeypatch):
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    process = subprocess.Popen([sys.executable, str(STUB), "0"], stdout=subprocess.PIPE, text=True)
    port = process.stdout.readline().strip()
    yield f"http://127.0.0.1:{port}"
    process.terminate()
    process.wait(timeout=10)


class Credentials:
    def account(self, role):
        return "test-company"

    def password(self, role):
        return "stub-password"


class Client:
    def __init__(self, spec_path):
        self.spec_path = spec_path

    def method(self, point):
        return "core/openapi-url"

    def spec_export(self, repo, commit):
        return PointResult(ExtensionPoint.SPEC_EXPORT, ExtensionLayer.STACK, output={
            "specFile": str(self.spec_path), "format": "openapi-3.0", "operationCount": 7,
            "tool": {"name": "stub", "version": None}, "logFile": None})

    def authz_endpoints(self, repo, commit):
        rules = [("GET", "/api/Order/{id}", ["CanViewOrders"]), ("POST", "/api/Order/Query", ["CanViewOrders"]),
                 ("GET", "/api/User/List", ["CanManageUsers"]), ("GET", "/api/Company/{id}", ["CanViewCompany"])]
        return PointResult(ExtensionPoint.AUTHZ_ENDPOINTS, ExtensionLayer.STACK, output={"endpoints": [
            {"method": method, "route": route, "requires": requires, "anonymous": False, "symbol": "Stub.Action",
             "sourceFile": None} for method, route, requires in rules], "unresolved": []})

    def authz_roles(self, repo, commit, roles):
        return PointResult(ExtensionPoint.AUTHZ_ROLES, ExtensionLayer.PROJECT, output={
            "capabilities": ["CanViewOrders", "CanManageUsers", "CanViewCompany"],
            "roles": {"Company": {"CanViewOrders": True, "CanManageUsers": False, "CanViewCompany": False}},
            "sourceFiles": ["roles.json"]})


def test_real_schemathesis_run(tmp_path, stub, make_config):
    layout = WorkspaceLayout(tmp_path / "workspace")
    spec_path = layout.openapi(RELEASE)
    spec_path.parent.mkdir(parents=True)
    spec_path.write_bytes(urllib.request.urlopen(f"{stub}/openapi.json").read())
    config = make_config(sources={"api-fuzz": {"exclude": ["^/api/Auth/"], "checks": {
        "serverError": True, "authorization": True, "statusCode": True, "responseSchema": "auto", "responseTime": True,
        "unsupportedMethod": False}}},
                         thresholds={"suppressionDays": {"value": 30, "min": 7, "max": 90},
                                     "triage": {"deferredReopenOccurrences": {"value": 3, "min": 1, "max": 10}},
                                     "slowResponseSeconds": {"value": 1, "min": 1, "max": 30}})
    redactor = Redactor()
    probe_redactor = ProbeRedactor(redactor)
    session = Session(stub, LoginSettings.from_config(config), Credentials(), UrllibTransport(), probe_redactor)
    probe = ApiFuzzProbe(ApiFuzzDependencies(config, Client(spec_path), session, SubprocessLauncher(redactor=redactor),
                                             layout, probe_redactor))
    raw_dir = tmp_path / "raw" / "api-fuzz"
    target = ProbeTarget("staging", "R-20261005-030000-collect-api-fuzz", raw_dir, SystemClock(), base_url=stub,
                         release=RELEASE, worktree=tmp_path)
    outcome = probe.run(target, ProbeLevel.DEEP, ProbeOptions(max_examples=20, seed=20261005))
    assert outcome.status is RunStatus.OK, outcome.notes
    found = {(signal.check, signal.location) for signal in outcome.signals}
    assert ("unauthorized_role_access", "GET /api/User/List") in found
    assert ("response_schema_conformance", "GET /api/Order/List") in found
    assert ("max_response_time", "GET /api/Report/Export") in found
    assert ("status_code_conformance", "GET /api/Company/{id}") not in found
    assert outcome.stats["expectedDenials"] >= 1 and outcome.coverage.endpoints_total == 6
    token = session.tokens()[0]
    for path in raw_dir.rglob("*"):
        if path.is_file():
            assert token not in path.read_text(encoding="utf-8", errors="replace")
    assert all(token not in json.dumps(signal.context) for signal in outcome.signals)
