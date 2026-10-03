"""core/openapi-security：从接口描述的 security 声明推出端点所需的能力(architecture/10 1.4、3.2)。

接口描述取 options.path(相对仓库根目录)；没有给出时取本 commit 的 spec-export 结果 data/specs/<commit>/openapi.json，
文件不存在时以 not-applicable 结束。每个操作的 security 取操作自身的声明，没有时取顶层的声明：
- security 为空数组，或其中有空对象(表示可以不认证)：匿名端点；
- 只有一种要求(或几种要求的作用域集合相同)：requires 为这种要求中全部作用域的并集，作用域名即能力名；
- 几种可选要求的作用域集合不同：无法写成「同时需要」的能力列表，列入 unresolved；
- 操作与顶层都没有 security：不能断定端点无需认证，列入 unresolved；security 不是「方案名 → 作用域数组」的对象数组时
  同样列入 unresolved。
symbol 取 operationId，没有时为「方法 路径」；sourceFile 为空。
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tightrein.domain.enums import ExtensionErrorCode
from tightrein.extensions.methods import openapi, runtime
from tightrein.extensions.methods.runtime import MethodContext, MethodError, MethodRequest, MethodResult
from tightrein.store.files.layout import WorkspaceLayout

MANIFEST = Path(__file__).with_suffix(".yaml")
NO_SECURITY = "接口描述中没有 security 声明"
ALTERNATIVES = "security 有多种可选的要求，无法写成同时需要的能力"
MALFORMED = "security 声明的写法无法识别"
NOT_ABSOLUTE = "路径不是以 / 开头"


def spec_path(request: MethodRequest) -> tuple[Path, str]:
    relative = request.options.get("path")
    if relative is not None:
        return runtime.inside(runtime.require_repo(request), relative, "path"), relative
    if request.commit is None:
        raise MethodError(ExtensionErrorCode.INVALID_INPUT, "请求缺少 commit，无法找到 spec-export 的结果")
    path = WorkspaceLayout(request.workspace).openapi(request.commit)
    return path, str(path)


def requirement(security: Any) -> tuple[bool, list[str] | None, str | None]:
    """(是否匿名, 所需能力, 无法判断的原因)。"""
    if not isinstance(security, list) or not all(
        isinstance(item, dict) and all(isinstance(scopes, list) for scopes in item.values()) for item in security
    ):
        return False, None, MALFORMED
    if not security or any(item == {} for item in security):
        return True, [], None
    choices = {frozenset(scope for scopes in item.values() for scope in scopes) for item in security}
    if len(choices) != 1:
        return False, None, ALTERNATIVES
    return False, sorted(choices.pop()), None


def endpoint(method: str, route: str, operation: Mapping[str, Any],
             default: Any) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
    symbol = operation.get("operationId") or f"{method} {route}"
    if not route.startswith("/"):
        return None, {"symbol": symbol, "reason": NOT_ABSOLUTE}
    security = operation.get("security", default)
    if security is None:
        return None, {"symbol": symbol, "reason": NO_SECURITY}
    anonymous, requires, reason = requirement(security)
    if requires is None:
        return None, {"symbol": symbol, "reason": reason}
    return {"method": method, "route": route, "requires": requires, "anonymous": anonymous, "symbol": symbol,
            "sourceFile": None}, None


def run(request: MethodRequest, context: MethodContext) -> MethodResult:
    path, source = spec_path(request)
    if not path.is_file():
        raise MethodError(ExtensionErrorCode.NOT_APPLICABLE, f"没有接口描述 {source}",
                          "先配置 spec-export，或给出 options.path")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise MethodError(ExtensionErrorCode.PARSE_FAILED, f"{source} 无法读取：{error}") from error
    document = openapi.parse(text, source)
    endpoints, unresolved = [], []
    for method, route, operation in openapi.operations(document):
        found, problem = endpoint(method, route, operation, document.get("security"))
        if found is not None:
            endpoints.append(found)
        else:
            unresolved.append(problem)
    notes = (f"{len(unresolved)} 个端点无法从 security 声明推出所需能力",) if unresolved else ()
    return MethodResult({"endpoints": endpoints, "unresolved": unresolved}, notes)


if __name__ == "__main__":
    sys.exit(runtime.serve(run, MANIFEST))
