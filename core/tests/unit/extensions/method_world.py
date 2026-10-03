"""核心方法测试共用的请求构造与调用：直接调用 runtime.respond，不启动子进程；options 与解析扩展点时一样先取
config/defaults.yaml 中该方法的默认值；成功的输出按该扩展点的 schema 校验。"""

from tightrein.config.layers import core_defaults
from tightrein.contracts import validate
from tightrein.domain.enums import ExtensionPoint
from tightrein.extensions import points
from tightrein.extensions.catalog import read_core
from tightrein.extensions.methods.runtime import MethodContext, respond

COMMIT = "d6f37025a1b2c3d4e5f60718293a4b5c6d7e8f90"


def request(point, *, workspace, repo=None, options=None, input=None, base=None):
    uses_repo = points.SPECS[point].uses_repo
    document = {
        "protocol": 1,
        "point": point.value,
        "workspace": str(workspace),
        "repo": str(repo) if uses_repo else None,
        "commit": COMMIT if uses_repo else None,
        "options": options or {},
        "scratchDir": str(workspace / "scratch"),
        "base": base,
        "input": input or {},
    }
    assert validate.validate(points.REQUEST_SCHEMA, document) == []
    return document


def call(module, document, context=None):
    method = read_core(module.MANIFEST)
    options = {**core_defaults()["methods"].get(method.id, {}), **document["options"]}
    response = respond({**document, "options": options}, module.run, method, context or MethodContext())
    assert validate.validate(points.RESPONSE_SCHEMA, response) == []
    if response["status"] == "ok":
        schema = points.SPECS[ExtensionPoint(document["point"])].output_schema
        assert validate.validate(schema, response["output"]) == []
    return response


def output_of(module, document, context=None):
    response = call(module, document, context)
    assert response["status"] == "ok", response
    return response["output"]


def error_of(module, document, context=None):
    """(错误码, 消息)。"""
    response = call(module, document, context)
    assert response["status"] == "error", response
    return response["error"]["code"], response["error"]["message"]
