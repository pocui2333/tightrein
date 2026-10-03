import io
import json

import pytest

from tightrein.domain.enums import ExtensionErrorCode
from tightrein.extensions.catalog import read_core
from tightrein.extensions.methods.runtime import (
    MethodContext,
    MethodError,
    MethodResult,
    check_document,
    inside,
    read_yaml,
    respond,
    serve,
)
from tightrein.store.files import yaml_text

MANIFEST = {
    "name": "echo",
    "point": "page-routes",
    "summary": "原样返回",
    "applicability": "测试用",
    "optionsSchema": {"type": "object", "required": ["prefix"], "additionalProperties": False,
                      "properties": {"prefix": {"type": "string"}, "fail": {"type": "string"}}},
}


@pytest.fixture
def manifest(tmp_path):
    folder = tmp_path / "methods" / "page_routes"
    folder.mkdir(parents=True)
    (folder / "echo.py").write_text("", encoding="utf-8")
    path = folder / "echo.yaml"
    path.write_text(yaml_text.dump(MANIFEST), encoding="utf-8")
    return path


def request(**fields):
    document = {"protocol": 1, "point": "page-routes", "workspace": "/w/demo", "repo": "/w/demo/repo",
                "commit": "d6f37025", "options": {"prefix": "/app"}, "scratchDir": "/tmp/scratch", "base": None,
                "input": {}}
    document.update(fields)
    return document


def handler(request, context):
    if "fail" in request.options:
        raise MethodError(ExtensionErrorCode.NOT_APPLICABLE, request.options["fail"], "换一种方法")
    route = {"path": request.options["prefix"], "name": None, "componentFile": None, "meta": None}
    return MethodResult({"routes": [route], "sourceFiles": []}, notes=(f"repo={request.repo}",))


def test_the_result_becomes_an_ok_response(manifest):
    response = respond(request(), handler, read_core(manifest), MethodContext())
    assert response == {"protocol": 1, "status": "ok", "notes": ["repo=/w/demo/repo"], "output": {
        "routes": [{"path": "/app", "name": None, "componentFile": None, "meta": None}], "sourceFiles": []}}
    changed = respond(request(options={"prefix": "/admin"}), handler, read_core(manifest), MethodContext())
    assert changed["output"]["routes"][0]["path"] == "/admin"


@pytest.mark.parametrize(("document", "message"), [
    (request(protocol=2), "不支持的协议版本：2"),
    ({"protocol": 1, "point": "page-routes", "repo": None, "commit": None, "options": {}, "base": None},
     "请求缺少字段：workspace, scratchDir, input"),
    (request(point="log-platform"), "core/echo 属于 page-routes，请求的扩展点为 log-platform"),
    (request(options={"prefix": 3}), "options 不合格：$.prefix: 3 is not of type 'string'"),
    (request(options={}), "options 不合格：$: 'prefix' is a required property"),
])
def test_invalid_requests(manifest, document, message):
    response = respond(document, handler, read_core(manifest), MethodContext())
    assert response["status"] == "error"
    assert (response["error"]["code"], response["error"]["message"]) == ("invalid-input", message)
    assert response["protocol"] == document["protocol"]


def test_method_errors_keep_their_code_and_hint(manifest):
    response = respond(request(options={"prefix": "/app", "fail": "没有路由文件"}), handler, read_core(manifest),
                       MethodContext())
    assert response == {"protocol": 1, "status": "error",
                        "error": {"code": "not-applicable", "message": "没有路由文件", "hint": "换一种方法"}}


def test_program_errors_are_not_swallowed(manifest):
    def broken(request, context):
        raise KeyError("routes")

    with pytest.raises(KeyError):
        respond(request(), broken, read_core(manifest), MethodContext())


def test_serve_reads_stdin_and_writes_one_response(manifest):
    output = io.StringIO()
    assert serve(handler, manifest, MethodContext(), stdin=io.StringIO(json.dumps(request())), stdout=output) == 0
    assert json.loads(output.getvalue())["status"] == "ok"
    garbage = io.StringIO()
    serve(handler, manifest, MethodContext(), stdin=io.StringIO("not json"), stdout=garbage)
    response = json.loads(garbage.getvalue())
    assert (response["protocol"], response["error"]["code"]) == (1, "invalid-input")


def test_paths_files_and_documents(tmp_path):
    assert inside(tmp_path, "a/b.yaml", "file") == (tmp_path / "a" / "b.yaml").resolve()
    with pytest.raises(MethodError) as caught:
        inside(tmp_path, "../outside.yaml", "file")
    assert caught.value.code is ExtensionErrorCode.INVALID_INPUT
    broken = tmp_path / "broken.yaml"
    broken.write_text("a: [1, 2\n", encoding="utf-8")
    with pytest.raises(MethodError) as caught:
        read_yaml(broken)
    assert caught.value.code is ExtensionErrorCode.PARSE_FAILED
    with pytest.raises(MethodError) as caught:
        read_yaml(tmp_path / "missing.yaml")
    assert caught.value.code is ExtensionErrorCode.PARSE_FAILED
    schema = {"type": "object", "required": ["routes"], "properties": {"routes": {"type": "array"}}}
    check_document({"routes": []}, schema, broken)
    with pytest.raises(MethodError) as caught:
        check_document({"routes": {}}, schema, broken)
    assert caught.value.message == f"{broken} 不合格：$.routes: {{}} is not of type 'array'"


def test_the_context_defaults_to_real_processes_and_the_current_time():
    context = MethodContext()
    assert context.now().tzinfo is not None
    assert "PATH" in context.environ
