import json

import pytest
from api_fuzz_world import ENDPOINTS, ROLES, SPEC, FakeClient, default, error, ok, spec_result, write_spec
from probe_world import NOW, RELEASE

from tightrein.contracts import validate
from tightrein.domain.clock import FixedClock
from tightrein.domain.enums import ExtensionLayer, ExtensionPoint, RunStatus
from tightrein.extensions.client import WorktreeNotAtCommit
from tightrein.sources.api_fuzz import spec
from tightrein.sources.api_fuzz.authz import model


def test_spec_is_read_from_the_extension_output(tmp_path):
    path = write_spec(tmp_path / "specs")
    client = FakeClient(spec_export=spec_result(path))
    outcome = spec.ensure(client, tmp_path / "worktree", RELEASE)
    assert outcome.status is RunStatus.OK and outcome.available and outcome.path == path
    assert outcome.document["paths"].keys() == SPEC["paths"].keys()
    assert outcome.extensions == {"spec-export": {"implementation": "stack", "cached": False}}
    assert client.calls == [("spec_export", tmp_path / "worktree", RELEASE)]


def test_spec_skipped_without_an_extension(tmp_path):
    outcome = spec.ensure(FakeClient(spec_export=default(ExtensionPoint.SPEC_EXPORT, ())), tmp_path, RELEASE)
    assert (outcome.status, outcome.notes) == (RunStatus.SKIPPED, ("未提供 spec-export 扩展",))


def test_spec_failures(tmp_path):
    failed = spec.ensure(FakeClient(spec_export=error(ExtensionPoint.SPEC_EXPORT)), tmp_path, RELEASE)
    assert failed.status is RunStatus.FAILED
    assert failed.notes == ("spec-export 失败：build-failed：构建失败；查看 spec-export.log",)
    moved = spec.ensure(FakeClient(spec_export=WorktreeNotAtCommit), tmp_path, RELEASE)
    assert moved.status is RunStatus.FAILED and f"tightrein project worktree sync --commit {RELEASE}" in moved.notes[0]
    assert spec.ensure(FakeClient(), tmp_path, None).status is RunStatus.FAILED
    assert spec.ensure(FakeClient(), None, RELEASE).notes == ("没有只读 worktree，无法取得接口描述",)


def test_a_spec_not_from_the_repo_needs_no_worktree(tmp_path):
    path = write_spec(tmp_path / "specs")
    client = FakeClient(needs_repo=False, spec_export=spec_result(path))
    assert spec.ensure(client, None, RELEASE).status is RunStatus.OK
    assert client.calls == [("spec_export", None, RELEASE)]


def test_operations_and_exclusions():
    assert spec.operations(SPEC)[:3] == [("POST", "/api/Auth/Login"), ("GET", "/api/Company"), ("POST", "/api/Company")]
    regex = spec.exclude_regex([r"^/api/Auth/", r"/Order/Query$"])
    assert regex == r"(?:^/api/Auth/)|(?:/Order/Query$)"
    assert spec.exclude_regex([]) is None
    assert len(spec.selected_operations(SPEC, regex)) == 4


def test_merge_aligns_routes_and_reports_gaps():
    built, notes = model.merge(RELEASE, ENDPOINTS, ROLES, ["Company", "Admin", "Personal"], SPEC)
    assert sorted(built.endpoints) == [("GET", "/api/Company"), ("GET", "/api/Order/{id}"), ("POST", "/api/Auth/Login"),
                                       ("POST", "/api/Company"), ("POST", "/api/Order/Query")]
    assert notes == [
        "1 个端点在接口描述中找不到，不做越权检查：DELETE /api/Legacy/{id}",
        "能力 CanSeeEverything 不在 authz-roles 的能力清单中，涉及的端点不做越权检查：GET /api/User/List",
        "authz-endpoints 无法解析 ReportController.Export：路由由约定生成",
        "authz-roles 的输出中没有角色 Personal，该角色不做越权检查",
    ]
    assert sorted(built.roles) == ["Admin", "Company"]


def test_missing_capabilities():
    built, _ = model.merge(RELEASE, ENDPOINTS, ROLES, ["Company", "Admin"], SPEC)
    assert built.missing_capabilities("Company", "POST", "/api/Company") == ("CanEditCompanies",)
    assert built.missing_capabilities("Admin", "post", "/api/Company") == ()
    assert built.missing_capabilities("Company", "POST", "/api/Auth/Login") == ()
    assert built.missing_capabilities("Company", "GET", "/api/Company") == ()
    assert built.missing_capabilities("Company", "GET", "/api/User/List") is None
    assert built.missing_capabilities("Personal", "GET", "/api/Order/{id}") is None
    assert built.role_capabilities("Company") == ("CanViewOrders",)


def roles_result():
    return ok(ExtensionPoint.AUTHZ_ROLES, ROLES, layer=ExtensionLayer.PROJECT)


def test_ensure_writes_a_valid_model_file(tmp_path):
    client = FakeClient(authz_endpoints=ok(ExtensionPoint.AUTHZ_ENDPOINTS, ENDPOINTS, cached=True),
                        authz_roles=roles_result())
    output = tmp_path / "specs" / "authz-model.json"
    outcome = model.ensure(client, tmp_path, RELEASE, ["Company", "Admin"], SPEC, output, FixedClock(NOW))
    document = json.loads(output.read_text(encoding="utf-8"))
    assert validate.validate("data/authz-model.schema.json", document) == []
    assert document["generatedAt"] == "2026-10-05T03:00:00Z"
    assert model.load(output) == outcome.model
    assert not outcome.degraded
    assert outcome.extensions == {"authz-endpoints": {"implementation": "stack", "cached": True},
                                  "authz-roles": {"implementation": "project", "cached": False}}
    assert client.calls[1] == ("authz_roles", tmp_path, RELEASE, ("Company", "Admin"))


@pytest.mark.parametrize(("endpoints", "roles", "degraded"), [
    (default(ExtensionPoint.AUTHZ_ENDPOINTS, ("未提供 authz-endpoints 扩展，不做越权检查",)), None, False),
    (error(ExtensionPoint.AUTHZ_ENDPOINTS), None, True),
    (None, error(ExtensionPoint.AUTHZ_ROLES, message="矩阵无法解析"), True),
    (WorktreeNotAtCommit, None, True),
])
def test_no_model_without_both_extensions(tmp_path, endpoints, roles, degraded):
    client = FakeClient(authz_endpoints=endpoints or ok(ExtensionPoint.AUTHZ_ENDPOINTS, ENDPOINTS),
                        authz_roles=roles or roles_result())
    output = tmp_path / "authz-model.json"
    outcome = model.ensure(client, tmp_path, RELEASE, ["Company"], SPEC, output, FixedClock(NOW))
    assert outcome.model is None and outcome.degraded is degraded and outcome.notes
    assert not output.exists()
