"""Schemathesis 的自定义检查(architecture/04 2.7)，由 schemathesis.toml 的 hooks 加载，运行在 Schemathesis 进程中。

模块加载时读取 TIGHTREIN_AUTHZ_MODEL(模型文件)与 TIGHTREIN_ROLE(当前角色)：两者都有才注册
unauthorized_role_access；本次没有模型时 invoke 不设置这两个变量，检查不注册，Schemathesis 只运行内置检查。
检查按请求的方法与路由模板在模型中查找端点：匿名端点、不需要能力的端点、模型中没有的端点直接通过；当前角色缺少
任一所需能力而响应为 2xx 时判为越权。只覆盖端点级别的准入，方法体内按数据归属收窄的校验不在范围内。
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import schemathesis

from tightrein.sources.api_fuzz.authz.model import AuthzModel, load

CHECK_NAME = "unauthorized_role_access"
MODEL_ENV = "TIGHTREIN_AUTHZ_MODEL"
ROLE_ENV = "TIGHTREIN_ROLE"


def violation(model: AuthzModel, role: str, method: str, route: str, status: int) -> str | None:
    """越权时返回失败消息，否则为空。"""
    if not 200 <= status < 300:
        return None
    missing = model.missing_capabilities(role, method, route)
    if not missing:
        return None
    return f"角色 {role} 缺少能力 {'、'.join(missing)}，{method.upper()} {route} 却返回 {status}"


def make_check(model: AuthzModel, role: str) -> Callable[[Any, Any, Any], None]:
    def unauthorized_role_access(ctx: Any, response: Any, case: Any) -> None:
        message = violation(model, role, case.method, case.path, response.status_code)
        if message is not None:
            raise AssertionError(message)

    unauthorized_role_access.__name__ = CHECK_NAME
    return unauthorized_role_access


def register(environ: Mapping[str, str] = os.environ) -> bool:
    path, role = environ.get(MODEL_ENV), environ.get(ROLE_ENV)
    if not path or not role:
        return False
    schemathesis.check(make_check(load(Path(path)), role))
    return True


register()
