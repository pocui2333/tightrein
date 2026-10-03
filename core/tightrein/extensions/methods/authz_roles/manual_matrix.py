"""core/manual-matrix：读取工作区中手工维护的角色能力矩阵(architecture/10 1.4、3.3)。

矩阵为 YAML，位置由 options.file 给出(相对工作区根目录)：

    capabilities: [OrderRead, OrderWrite]
    roles:
      Viewer: [OrderRead]
      Editor: [OrderRead, OrderWrite]

输出每个请求的角色对每个能力是否具备；矩阵中没有的角色不输出，写入 notes(该角色不做越权检查)。角色列出了
capabilities 之外的能力时以 parse-failed 结束。矩阵是工作区文件而不是仓库中的源文件，sourceFiles 为空。
"""

from __future__ import annotations

import sys
from pathlib import Path

from tightrein.domain.enums import ExtensionErrorCode
from tightrein.extensions.methods import runtime
from tightrein.extensions.methods.runtime import MethodContext, MethodError, MethodRequest, MethodResult

MANIFEST = Path(__file__).with_suffix(".yaml")
NAMES = {"type": "array", "items": {"type": "string", "minLength": 1}, "uniqueItems": True}
FILE_SCHEMA = {
    "type": "object",
    "required": ["capabilities", "roles"],
    "additionalProperties": False,
    "properties": {"capabilities": NAMES, "roles": {"type": "object", "additionalProperties": NAMES}},
}


def run(request: MethodRequest, context: MethodContext) -> MethodResult:
    path = runtime.workspace_file(request, "file")
    document = runtime.read_yaml(path)
    runtime.check_document(document, FILE_SCHEMA, path)
    capabilities = document["capabilities"]
    known = set(capabilities)
    for role, granted in document["roles"].items():
        unknown = [name for name in granted if name not in known]
        if unknown:
            raise MethodError(ExtensionErrorCode.PARSE_FAILED,
                              f"{path} 中角色 {role} 的能力不在 capabilities 中：{', '.join(unknown)}")
    roles, notes = {}, []
    for role in request.input["roles"]:
        granted = document["roles"].get(role)
        if granted is None:
            notes.append(f"矩阵中没有角色 {role}，该角色不做越权检查")
            continue
        roles[role] = {name: name in granted for name in capabilities}
    return MethodResult({"capabilities": capabilities, "roles": roles, "sourceFiles": []}, tuple(notes))


if __name__ == "__main__":
    sys.exit(runtime.serve(run, MANIFEST))
