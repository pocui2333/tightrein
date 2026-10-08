import json

import pytest

from tightrein.collect.api_fuzz import spec
from tightrein.collect.api_fuzz.spec import SpecNotApplicable, SpecRequest
from tightrein.collect.common.source import SourceInvalid, SourceMisconfigured, SourceUnavailable
from tightrein.protocol.http import HttpResponse

OPENAPI_YAML = """openapi: 3.0.1
info: {title: Demo, version: "1"}
paths:
  /api/orders:
    parameters: []
    get: {operationId: listOrders}
    post: {operationId: createOrder}
  /api/orders/{id}:
    get: {operationId: getOrder}
"""


class FakeGit:
    main_branch = "main"

    def __init__(self, files):
        self.files = files
        self.shown = []

    def show(self, rev, path):
        self.shown.append((rev, path))
        return self.files.get((rev, path))


class Transport:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        return self.response


def ensure(runtime, method, *, git=None, commit=None, transport=None):
    request = SpecRequest({}, git or FakeGit({}), runtime.workspace.root, commit,
                          transport or Transport(HttpResponse(404)))
    return spec.ensure(method=method, settings=runtime.settings, secrets={}, request=request,
                       cache_dir=runtime.workspace.cache_dir, raw_dir=runtime.workspace.root / "raw")


def test_a_yaml_file_in_the_repo_is_read_at_the_deployed_commit_and_cached(source_runtime):
    runtime = source_runtime(controls={"collect.api_fuzz": {"openapi_file": {"path": "docs/openapi.yaml",
                                                                             "base": "repo"}}})
    git = FakeGit({("c0ffee", "docs/openapi.yaml"): OPENAPI_YAML})
    found = ensure(runtime, "openapi_file", git=git, commit="c0ffee")
    assert found.path == runtime.workspace.cache_dir / "openapi" / "c0ffee.json" and not found.cached
    assert json.loads(found.path.read_text(encoding="utf-8"))["paths"]["/api/orders"]["post"] == {
        "operationId": "createOrder"}
    assert spec.operations(found.document) == [("GET", "/api/orders"), ("POST", "/api/orders"),
                                               ("GET", "/api/orders/{id}")]
    again = ensure(runtime, "openapi_file", git=git, commit="c0ffee")
    assert again.cached and again.hash == found.hash and len(git.shown) == 1


def test_without_a_deployment_the_main_branch_is_read(source_runtime):
    runtime = source_runtime(controls={"collect.api_fuzz": {"openapi_file": {"path": "openapi.json",
                                                                             "base": "repo"}}})
    git = FakeGit({("origin/main", "openapi.json"): json.dumps({"swagger": "2.0", "paths": {}})})
    assert not ensure(runtime, "openapi_file", git=git).cached
    assert git.shown == [("origin/main", "openapi.json")]


def test_a_workspace_file_needs_no_repo_and_missing_files_are_not_applicable(source_runtime):
    runtime = source_runtime(controls={"collect.api_fuzz": {"openapi_file": {"path": "specs/openapi.yaml",
                                                                             "base": "workspace"}}})
    with pytest.raises(SpecNotApplicable):
        ensure(runtime, "openapi_file")
    target = runtime.workspace.root / "specs" / "openapi.yaml"
    target.parent.mkdir(parents=True)
    target.write_text(OPENAPI_YAML, encoding="utf-8")
    found = ensure(runtime, "openapi_file")
    assert found.path == runtime.workspace.root / "raw" / "openapi.json" and found.label == "工作区的 specs/openapi.yaml"


@pytest.mark.parametrize("path", ["../outside.yaml", "a/../../b.yaml"])
def test_paths_cannot_leave_the_base(source_runtime, path):
    runtime = source_runtime(controls={"collect.api_fuzz": {"openapi_file": {"path": path, "base": "workspace"}}})
    with pytest.raises(SourceMisconfigured):
        ensure(runtime, "openapi_file")


@pytest.mark.parametrize(("text", "message"), [
    ("key: [unclosed", "不是 JSON 或 YAML"),
    ('{"openapi": "3.0.1"}', "没有 paths 对象"),
    ('{"openapi": "2.5", "paths": {}}', "版本无法识别"),
])
def test_unusable_documents(text, message):
    with pytest.raises(SourceInvalid, match=message):
        spec.parse(text, "x")


def test_urls_must_be_http_and_answer(source_runtime):
    runtime = source_runtime(controls={"collect.api_fuzz": {"openapi_url": {"url": "ftp://h/openapi.json"}}})
    with pytest.raises(SourceMisconfigured):
        ensure(runtime, "openapi_url")
    runtime = source_runtime(controls={"collect.api_fuzz": {"openapi_url": {"url": "http://h/openapi.json"}}})
    with pytest.raises(SourceUnavailable, match="返回 503"):
        ensure(runtime, "openapi_url", transport=Transport(HttpResponse(503)))
    transport = Transport(HttpResponse(200, OPENAPI_YAML.encode("utf-8")))
    found = ensure(runtime, "openapi_url", transport=transport)
    assert transport.requests[0].timeout_s == 30 and found.label == "http://h/openapi.json"


def test_excluded_routes_do_not_count_as_operations():
    document = spec.parse(OPENAPI_YAML, "x")
    assert spec.operations(document, "(?:^/api/orders/)") == [("GET", "/api/orders"), ("POST", "/api/orders")]
