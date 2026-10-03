"""core/openapi-file：读取现成的接口描述文件(architecture/10 1.4、3.1)。

options.base 为 repo(缺省)时 options.path 相对仓库根目录，取该 commit 的只读 worktree 中的文件；为 workspace 时
相对工作区根目录，供仓库中没有接口描述、由工作区维护的项目使用。path 不能越出基准目录；没有这个文件时以
not-applicable 结束，按核心默认处理(api-fuzz 跳过)。文件可以是 JSON 或 YAML，统一写成 JSON 到 input.outputFile。
"""

from __future__ import annotations

import sys
from pathlib import Path

from tightrein.domain.enums import ExtensionErrorCode
from tightrein.extensions.methods import openapi, runtime
from tightrein.extensions.methods.runtime import MethodContext, MethodError, MethodRequest, MethodResult

MANIFEST = Path(__file__).with_suffix(".yaml")
TOOL = "openapi-file"
BASE_WORKSPACE = "workspace"
BASE_NAMES = {BASE_WORKSPACE: "工作区", "repo": "仓库"}


def run(request: MethodRequest, context: MethodContext) -> MethodResult:
    relative = request.options["path"]
    base = request.options["base"]
    root = request.workspace if base == BASE_WORKSPACE else runtime.require_repo(request)
    path = runtime.inside(root, relative, "path")
    if not path.is_file():
        raise MethodError(ExtensionErrorCode.NOT_APPLICABLE, f"{BASE_NAMES[base]}中没有接口描述文件 {relative}")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise MethodError(ExtensionErrorCode.PARSE_FAILED, f"{relative} 无法读取：{error}") from error
    document = openapi.parse(text, relative)
    return openapi.export(document, Path(request.input["outputFile"]), relative, TOOL)


if __name__ == "__main__":
    sys.exit(runtime.serve(run, MANIFEST))
