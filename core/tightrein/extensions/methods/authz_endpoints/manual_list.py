"""core/manual-list(authz-endpoints)：读取工作区中手工维护的端点权限清单(architecture/10 1.4、3.2)。

清单为 YAML，位置由 options.file 给出(相对工作区根目录)：

    endpoints:
      - {method: GET, route: /api/orders, requires: [OrderRead]}
      - {method: POST, route: /api/login, anonymous: true, symbol: AuthController.Login}

requires 缺省为空，anonymous 缺省为 false，symbol 缺省为「方法 路径」，sourceFile 缺省为空；同一方法与路径出现两次
时以 parse-failed 结束。清单不是代码的一部分，文件不存在属于配置问题，以 invalid-input 结束。
"""

from __future__ import annotations

import sys
from pathlib import Path

from tightrein.domain.enums import ExtensionErrorCode
from tightrein.extensions.methods import runtime
from tightrein.extensions.methods.runtime import MethodContext, MethodError, MethodRequest, MethodResult

MANIFEST = Path(__file__).with_suffix(".yaml")
HTTP_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
FILE_SCHEMA = {
    "type": "object",
    "required": ["endpoints"],
    "additionalProperties": False,
    "properties": {
        "endpoints": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["method", "route"],
                "additionalProperties": False,
                "properties": {
                    "method": {"enum": HTTP_METHODS},
                    "route": {"type": "string", "pattern": "^/"},
                    "requires": {"type": "array", "items": {"type": "string", "minLength": 1}, "uniqueItems": True},
                    "anonymous": {"type": "boolean"},
                    "symbol": {"type": "string", "minLength": 1},
                    "sourceFile": {"type": "string", "pattern": "^[^/\\\\][^\\\\]*$"},
                },
            },
        },
    },
}


def run(request: MethodRequest, context: MethodContext) -> MethodResult:
    path = runtime.workspace_file(request, "file")
    document = runtime.read_yaml(path)
    runtime.check_document(document, FILE_SCHEMA, path)
    endpoints, seen = [], set()
    for item in document["endpoints"]:
        key = (item["method"], item["route"])
        if key in seen:
            raise MethodError(ExtensionErrorCode.PARSE_FAILED, f"{path} 中 {item['method']} {item['route']} 出现了两次")
        seen.add(key)
        endpoints.append({"method": item["method"], "route": item["route"], "requires": item.get("requires", []),
                          "anonymous": item.get("anonymous", False),
                          "symbol": item.get("symbol", f"{item['method']} {item['route']}"),
                          "sourceFile": item.get("sourceFile")})
    return MethodResult({"endpoints": endpoints, "unresolved": []})


if __name__ == "__main__":
    sys.exit(runtime.serve(run, MANIFEST))
