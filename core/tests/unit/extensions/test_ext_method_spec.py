import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from method_world import call, error_of, output_of, request

from tightrein.domain.enums import ExtensionPoint
from tightrein.extensions.methods.runtime import MethodContext
from tightrein.extensions.methods.spec_export import openapi_file, openapi_url
from tightrein.sources.common.http import HttpResponse

POINT = ExtensionPoint.SPEC_EXPORT
URL = "http://localhost:8000/openapi.json"
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


def spec_request(tmp_path, options):
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    return request(POINT, workspace=tmp_path, repo=repo, options=options,
                   input={"outputFile": str(tmp_path / "specs" / "openapi.json")})


def write_spec(tmp_path, relative, text):
    path = tmp_path / "repo" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class FakeTransport:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        return self.response


def test_a_yaml_file_in_the_repo_is_written_as_json(tmp_path):
    document = spec_request(tmp_path, {"path": "docs/openapi.yaml"})
    write_spec(tmp_path, "docs/openapi.yaml", OPENAPI_YAML)
    assert output_of(openapi_file, document) == {
        "specFile": str(tmp_path / "specs" / "openapi.json"), "format": "openapi-3.0", "operationCount": 3,
        "tool": {"name": "openapi-file", "version": None}, "logFile": None}
    written = json.loads((tmp_path / "specs" / "openapi.json").read_text(encoding="utf-8"))
    assert written["paths"]["/api/orders"]["post"] == {"operationId": "createOrder"}


def test_a_file_in_the_workspace_is_read_when_base_is_workspace(tmp_path):
    (tmp_path / "openapi.yaml").write_text(OPENAPI_YAML, encoding="utf-8")
    write_spec(tmp_path, "openapi.yaml", "openapi: 3.0.1\npaths: {}\n")
    output = output_of(openapi_file, spec_request(tmp_path, {"path": "openapi.yaml", "base": "workspace"}))
    assert output["operationCount"] == 3
    assert error_of(openapi_file, spec_request(tmp_path, {"path": "missing.yaml", "base": "workspace"})) == (
        "not-applicable", "工作区中没有接口描述文件 missing.yaml")
    assert error_of(openapi_file, spec_request(tmp_path, {"path": "../outside.yaml", "base": "workspace"}))[0] == (
        "invalid-input")
    assert error_of(openapi_file, spec_request(tmp_path, {"path": "openapi.yaml", "base": "home"}))[0] == (
        "invalid-input")


@pytest.mark.parametrize(("spec", "format", "count"), [
    ({"openapi": "3.1.0", "paths": {}}, "openapi-3.1", 0),
    ({"swagger": "2.0", "paths": {"/a": {"get": {}, "x-note": {}}, "/b": {"delete": {}, "head": {}}}},
     "swagger-2.0", 3),
])
def test_versions_and_operations_of_json_files(tmp_path, spec, format, count):
    document = spec_request(tmp_path, {"path": "api.json"})
    write_spec(tmp_path, "api.json", json.dumps(spec))
    output = output_of(openapi_file, document)
    assert (output["format"], output["operationCount"]) == (format, count)


@pytest.mark.parametrize(("text", "code", "message"), [
    (None, "not-applicable", "仓库中没有接口描述文件 docs/openapi.yaml"),
    ("openapi: 3.0.1\ninfo: {}\n", "parse-failed", "docs/openapi.yaml 中没有 paths 对象"),
    ("openapi: 2.5\npaths: {}\n", "parse-failed", "docs/openapi.yaml 的版本无法识别：openapi 为 2.5，swagger 为 None"),
])
def test_missing_and_unusable_files(tmp_path, text, code, message):
    document = spec_request(tmp_path, {"path": "docs/openapi.yaml"})
    if text is not None:
        write_spec(tmp_path, "docs/openapi.yaml", text)
    assert error_of(openapi_file, document) == (code, message)


def test_broken_files_and_invalid_options(tmp_path):
    write_spec(tmp_path, "docs/openapi.yaml", "paths: [1, 2\n")
    code, message = error_of(openapi_file, spec_request(tmp_path, {"path": "docs/openapi.yaml"}))
    assert code == "parse-failed" and message.startswith("docs/openapi.yaml 不是 JSON 或 YAML")
    assert error_of(openapi_file, spec_request(tmp_path, {"path": "../outside.yaml"}))[0] == "invalid-input"
    assert error_of(openapi_file, spec_request(tmp_path, {})) == (
        "invalid-input", "options 不合格：$: 'path' is a required property")


def test_the_url_is_read_with_a_get_request(tmp_path):
    transport = FakeTransport(HttpResponse(200, OPENAPI_YAML.encode("utf-8")))
    output = output_of(openapi_url, spec_request(tmp_path, {"url": URL}), MethodContext(transport=transport))
    assert (output["format"], output["operationCount"], output["tool"]) == (
        "openapi-3.0", 3, {"name": "openapi-url", "version": None})
    sent = transport.requests[0]
    assert (sent.method, sent.url, sent.body, sent.timeout_seconds) == ("GET", URL, None, 30.0)
    assert sent.headers["Accept"].startswith("application/json")


@pytest.mark.parametrize(("response", "message"), [
    (HttpResponse(None, error="ConnectionRefusedError: 拒绝连接"), f"无法读取 {URL}：ConnectionRefusedError: 拒绝连接"),
    (HttpResponse(404, b"not found"), f"{URL} 返回 404"),
])
def test_an_unreachable_service_is_a_source_error(tmp_path, response, message):
    result = call(openapi_url, spec_request(tmp_path, {"url": URL, "timeoutSeconds": 5}),
                  MethodContext(transport=FakeTransport(response)))
    assert result["error"] == {"code": "source-unavailable", "message": message,
                               "hint": "确认服务已在本机启动，地址与端口与 options.url 一致"}


def test_only_http_urls_are_accepted(tmp_path):
    code, message = error_of(openapi_url, spec_request(tmp_path, {"url": "file:///etc/hosts"}))
    assert code == "invalid-input" and "$.url" in message


class SpecHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        data = OPENAPI_YAML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/yaml")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):
        pass


@pytest.mark.slow
def test_a_local_server_is_read_over_http(tmp_path, monkeypatch):
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    server = ThreadingHTTPServer(("127.0.0.1", 0), SpecHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/openapi.yaml"
        assert output_of(openapi_url, spec_request(tmp_path, {"url": url}))["operationCount"] == 3
    finally:
        server.shutdown()
        server.server_close()
