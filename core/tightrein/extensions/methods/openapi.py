"""接口描述的解析与导出(architecture/10 3.1)：openapi-file、openapi-url、openapi-security 共用。

接口描述可以是 JSON 或 YAML；必须是含 paths 对象的映射，版本由 openapi(3.0.x、3.1.x)或 swagger(2.0)字段判断。
导出时一律写成 JSON 到 input.outputFile，操作数为 paths 下各路径中 HTTP 方法键的个数。
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from tightrein.domain.enums import ExtensionErrorCode
from tightrein.extensions.methods.runtime import MethodError, MethodResult
from tightrein.store.files import yaml_text

HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
OPENAPI_30 = "openapi-3.0"
OPENAPI_31 = "openapi-3.1"
SWAGGER_20 = "swagger-2.0"


def parse(text: str, source: str) -> dict[str, Any]:
    """把文本解析为接口描述；不是 JSON 也不是 YAML、不是映射或没有 paths 对象时以 parse-failed 结束。"""
    try:
        document = json.loads(text)
    except ValueError:
        try:
            document = yaml_text.load(text)
        except yaml_text.YamlError as error:
            raise MethodError(ExtensionErrorCode.PARSE_FAILED, f"{source} 不是 JSON 或 YAML：{error}") from error
    if not isinstance(document, dict) or not isinstance(document.get("paths"), dict):
        raise MethodError(ExtensionErrorCode.PARSE_FAILED, f"{source} 中没有 paths 对象")
    return document


def detect_format(document: Mapping[str, Any], source: str) -> str:
    version = str(document.get("openapi", ""))
    if version.startswith("3.0"):
        return OPENAPI_30
    if version.startswith("3.1"):
        return OPENAPI_31
    if str(document.get("swagger", "")) == "2.0":
        return SWAGGER_20
    found = f"openapi 为 {document.get('openapi')}，swagger 为 {document.get('swagger')}"
    raise MethodError(ExtensionErrorCode.PARSE_FAILED, f"{source} 的版本无法识别：{found}")


def operations(document: Mapping[str, Any]) -> Iterator[tuple[str, str, Mapping[str, Any]]]:
    """(大写的 HTTP 方法, 路径, 操作对象)，按 paths 中的顺序。"""
    for route, item in document["paths"].items():
        if not isinstance(item, dict):
            continue
        for method in HTTP_METHODS:
            operation = item.get(method)
            if isinstance(operation, dict):
                yield method.upper(), route, operation


def export(document: Mapping[str, Any], output_file: Path, source: str, tool: str) -> MethodResult:
    """把接口描述写成 JSON 并给出 spec-export 的 output。"""
    format = detect_format(document, source)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    output = {"specFile": str(output_file), "format": format,
              "operationCount": sum(1 for _ in operations(document)), "tool": {"name": tool, "version": None},
              "logFile": None}
    return MethodResult(output)
