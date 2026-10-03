"""core/manual-list(page-routes)：读取工作区中手工维护的页面路由清单(architecture/10 1.4、3.7)。

清单为 YAML，位置由 options.file 给出(相对工作区根目录)：

    routes:
      - {path: /orders, name: orders, componentFile: src/views/Orders.vue}
      - {path: /orders/:id}

name、componentFile、meta 缺省为空；只列可以打开的页面，同一路径出现两次时以 parse-failed 结束。
文件不存在属于配置问题，以 invalid-input 结束。
"""

from __future__ import annotations

import sys
from pathlib import Path

from tightrein.domain.enums import ExtensionErrorCode
from tightrein.extensions.methods import runtime
from tightrein.extensions.methods.runtime import MethodContext, MethodError, MethodRequest, MethodResult

MANIFEST = Path(__file__).with_suffix(".yaml")
FILE_SCHEMA = {
    "type": "object",
    "required": ["routes"],
    "additionalProperties": False,
    "properties": {
        "routes": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["path"],
                "additionalProperties": False,
                "properties": {
                    "path": {"type": "string", "pattern": "^/"},
                    "name": {"type": "string", "minLength": 1},
                    "componentFile": {"type": "string", "pattern": "^[^/\\\\][^\\\\]*$"},
                    "meta": {"type": "object"},
                },
            },
        },
    },
}


def run(request: MethodRequest, context: MethodContext) -> MethodResult:
    path = runtime.workspace_file(request, "file")
    document = runtime.read_yaml(path)
    runtime.check_document(document, FILE_SCHEMA, path)
    routes, seen = [], set()
    for item in document["routes"]:
        if item["path"] in seen:
            raise MethodError(ExtensionErrorCode.PARSE_FAILED, f"{path} 中路由 {item['path']} 出现了两次")
        seen.add(item["path"])
        routes.append({"path": item["path"], "name": item.get("name"), "componentFile": item.get("componentFile"),
                       "meta": item.get("meta")})
    return MethodResult({"routes": routes, "sourceFiles": []})


if __name__ == "__main__":
    sys.exit(runtime.serve(run, MANIFEST))
