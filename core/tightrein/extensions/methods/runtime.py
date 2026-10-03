"""核心方法的运行框架(architecture/10 2.2 到 2.4)。

核心方法与其他扩展一样以子进程运行(`{python} -m <模块>`)：serve 从标准输入读取请求、按方法的 optionsSchema 校验
options(默认值由核心在解析扩展点时从 config/defaults.yaml 合并进来)，调用方法的 run，把结果或错误写成一个响应 JSON。
请求的 protocol 不受支持、缺少外层字段、point 不是本方法的扩展点、options 不合格时以 invalid-input 结束；方法以
MethodError 报告扩展定义的错误码；其他异常属于程序错误，不捕获，进程以非 0 退出，由核心按 crashed 处理。
外部进程、HTTP 请求、当前时间与环境变量经 MethodContext 注入，单元测试直接调用 respond。
"""

from __future__ import annotations

import codecs
import json
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any

from jsonschema import Draft202012Validator

from tightrein.domain.enums import ExtensionErrorCode, ExtensionPoint
from tightrein.extensions import catalog, points
from tightrein.extensions.invoke import ProcessRunner, SubprocessRunner
from tightrein.sources.common.http import Transport, UrllibTransport
from tightrein.store.files import yaml_text

STATUS_OK = "ok"
STATUS_ERROR = "error"
REQUEST_FIELDS = ("point", "workspace", "repo", "commit", "options", "scratchDir", "base", "input")


class MethodError(Exception):
    """方法按 architecture/10 2.4 报告的错误；code 只能是扩展给出的六个错误码之一。"""

    def __init__(self, code: ExtensionErrorCode, message: str, hint: str | None = None) -> None:
        self.code = code
        self.message = message
        self.hint = hint
        super().__init__(message)


@dataclass(frozen=True)
class MethodRequest:
    point: ExtensionPoint
    workspace: Path
    repo: Path | None
    commit: str | None
    options: Mapping[str, Any]
    scratch_dir: Path
    base: Mapping[str, Any] | None
    input: Mapping[str, Any]


@dataclass(frozen=True)
class MethodResult:
    output: Mapping[str, Any]
    notes: tuple[str, ...] = ()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class MethodContext:
    runner: ProcessRunner = field(default_factory=SubprocessRunner)
    transport: Transport = field(default_factory=UrllibTransport)
    environ: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    now: Callable[[], datetime] = _utc_now


Handler = Callable[[MethodRequest, MethodContext], MethodResult]


def error_response(protocol: Any, code: ExtensionErrorCode, message: str, hint: str | None = None) -> dict[str, Any]:
    return {"protocol": protocol, "status": STATUS_ERROR, "error": {"code": code.value, "message": message,
                                                                     "hint": hint}}


def respond(document: Any, handler: Handler, method: catalog.Method, context: MethodContext) -> dict[str, Any]:
    """一次请求的完整响应。"""
    protocol = document.get("protocol") if isinstance(document, dict) else None
    if protocol != points.PROTOCOL:
        return error_response(protocol if isinstance(protocol, int) else points.PROTOCOL,
                              ExtensionErrorCode.INVALID_INPUT, f"不支持的协议版本：{protocol}")
    missing = [name for name in REQUEST_FIELDS if name not in document]
    if missing:
        return error_response(protocol, ExtensionErrorCode.INVALID_INPUT, f"请求缺少字段：{', '.join(missing)}")
    if document["point"] != method.point.value:
        return error_response(protocol, ExtensionErrorCode.INVALID_INPUT,
                              f"{method.id} 属于 {method.point.value}，请求的扩展点为 {document['point']}")
    options = dict(document["options"] or {})
    problems = catalog.option_problems(method.options_schema, options)
    if problems:
        return error_response(protocol, ExtensionErrorCode.INVALID_INPUT, "options 不合格：" + "；".join(problems),
                              f"检查 extensions.{method.point.value}.options")
    request = MethodRequest(
        point=method.point,
        workspace=Path(document["workspace"]),
        repo=None if document["repo"] is None else Path(document["repo"]),
        commit=document["commit"],
        options=options,
        scratch_dir=Path(document["scratchDir"]),
        base=document["base"],
        input=document["input"] or {},
    )
    try:
        result = handler(request, context)
    except MethodError as error:
        return error_response(protocol, error.code, error.message, error.hint)
    response: dict[str, Any] = {"protocol": protocol, "status": STATUS_OK, "output": dict(result.output)}
    if result.notes:
        response["notes"] = list(result.notes)
    return response


def serve(handler: Handler, manifest: Path, context: MethodContext | None = None, *,
          stdin: IO[str] | None = None, stdout: IO[str] | None = None) -> int:
    """作为扩展进程运行一次；标准输入不是 JSON 时同样以 invalid-input 响应。"""
    source = stdin or sys.stdin
    target = stdout or sys.stdout
    method = catalog.read_core(manifest, check=False)
    try:
        document = json.loads(source.read())
    except ValueError as error:
        response = error_response(points.PROTOCOL, ExtensionErrorCode.INVALID_INPUT, f"请求不是 JSON：{error}")
    else:
        response = respond(document, handler, method, context or MethodContext())
    target.write(json.dumps(response, ensure_ascii=False))
    target.flush()
    return 0


def inside(base: Path, relative: str, key: str) -> Path:
    """base 下的相对路径；越出 base 时以 invalid-input 结束。"""
    target = (base / relative).resolve()
    if not target.is_relative_to(base.resolve()):
        raise MethodError(ExtensionErrorCode.INVALID_INPUT, f"options.{key} 越出了 {base}：{relative}")
    return target


def require_repo(request: MethodRequest) -> Path:
    if request.repo is None:
        raise MethodError(ExtensionErrorCode.INVALID_INPUT, f"{request.point.value} 的请求缺少 repo")
    return request.repo


def read_yaml(path: Path) -> Any:
    """读取 YAML(JSON 是它的子集)；无法读取或解析时以 parse-failed 结束。"""
    try:
        return yaml_text.load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml_text.YamlError) as error:
        raise MethodError(ExtensionErrorCode.PARSE_FAILED, f"{path} 无法解析：{error}") from error


def check_document(document: Any, schema: Mapping[str, Any], path: Path) -> None:
    """按 schema 校验读到的文件内容；不合格时以 parse-failed 结束，列出每处的 JSON 路径与原因。"""
    validator = Draft202012Validator(dict(schema))
    errors = sorted(validator.iter_errors(document), key=lambda error: error.json_path)
    if errors:
        details = "；".join(f"{error.json_path}: {error.message}" for error in errors)
        raise MethodError(ExtensionErrorCode.PARSE_FAILED, f"{path} 不合格：{details}")


def workspace_file(request: MethodRequest, key: str) -> Path:
    """options.<key> 给出的工作区文件(相对工作区根目录)；不存在时以 invalid-input 结束。"""
    relative = request.options[key]
    path = inside(request.workspace, relative, key)
    if not path.is_file():
        raise MethodError(ExtensionErrorCode.INVALID_INPUT, f"工作区中没有 {relative}",
                          f"在工作区中创建该文件，或检查 extensions.{request.point.value}.options.{key}")
    return path


def check_encoding(encoding: str) -> None:
    """options 中的编码名必须是 Python 认识的编码，否则以 invalid-input 结束。"""
    try:
        codecs.lookup(encoding)
    except LookupError as error:
        raise MethodError(ExtensionErrorCode.INVALID_INPUT, f"不认识的编码：{encoding}") from error
