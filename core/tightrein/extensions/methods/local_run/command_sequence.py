"""core/command-sequence：按顺序启动一组命令，逐个等待就绪信号(architecture/10 1.4、3.8)。

只给出启动计划，不启动任何进程。options.services 中每一项是一个服务：
- port 为 backend、frontend(取 input.ports 中的端口)或具体的端口号；port 为 frontend 的服务只在 page 模式启动，
  其余服务两种模式都启动；
- argv 与 env 的值中的 {port}、{backendPort}、{frontendPort} 替换为端口号；api 模式没有前端端口，用到时以
  invalid-input 结束；
- readyPath 给出时就绪地址为 http://localhost:<port><readyPath>；
- after 缺省为本模式中排在前面的上一个服务，即按列出的顺序逐个启动；显式给出时必须是本模式中的服务，null 表示
  不等待。
unavailable 与 migrationPaths 原样输出。
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tightrein.domain.enums import ExtensionErrorCode
from tightrein.extensions.methods import runtime
from tightrein.extensions.methods.runtime import MethodContext, MethodError, MethodRequest, MethodResult

MANIFEST = Path(__file__).with_suffix(".yaml")
BACKEND = "backend"
FRONTEND = "frontend"
PAGE_MODE = "page"
LOCALHOST = "http://localhost"


def fill(text: str, values: Mapping[str, int | None], name: str) -> str:
    for key, value in values.items():
        placeholder = "{" + key + "}"
        if placeholder not in text:
            continue
        if value is None:
            raise MethodError(ExtensionErrorCode.INVALID_INPUT, f"服务 {name} 用到了 {placeholder}，本模式没有这个端口")
        text = text.replace(placeholder, str(value))
    return text


def run(request: MethodRequest, context: MethodContext) -> MethodResult:
    mode = request.input["mode"]
    ports = request.input["ports"]
    configured = request.options["services"]
    names = [service["name"] for service in configured]
    duplicated = sorted({name for name in names if names.count(name) > 1})
    if duplicated:
        raise MethodError(ExtensionErrorCode.INVALID_INPUT, f"服务名重复：{', '.join(duplicated)}")
    selected = [service for service in configured if mode == PAGE_MODE or service["port"] != FRONTEND]
    included = {service["name"] for service in selected}
    services: list[dict[str, Any]] = []
    for service in selected:
        name = service["name"]
        port = service["port"] if isinstance(service["port"], int) else ports[service["port"]]
        values = {"port": port, "backendPort": ports[BACKEND], "frontendPort": ports[FRONTEND]}
        after = service["after"] if "after" in service else (services[-1]["name"] if services else None)
        if after is not None and after not in included:
            message = f"服务 {name} 的 after 为 {after}，{mode} 模式中没有这个服务"
            raise MethodError(ExtensionErrorCode.INVALID_INPUT, message)
        ready_path = service.get("readyPath")
        services.append({
            "name": name,
            "argv": [fill(part, values, name) for part in service["argv"]],
            "cwd": service.get("cwd", "."),
            "env": {key: fill(value, values, name) for key, value in service.get("env", {}).items()},
            "port": port,
            "readyUrl": None if ready_path is None else f"{LOCALHOST}:{port}{ready_path}",
            "readyPatterns": list(service.get("readyPatterns", [])),
            "failPatterns": list(service.get("failPatterns", [])),
            "after": after,
        })
    output = {"services": services, "unavailable": list(request.options["unavailable"]),
              "migrationPaths": list(request.options["migrationPaths"])}
    return MethodResult(output)


if __name__ == "__main__":
    sys.exit(runtime.serve(run, MANIFEST))
