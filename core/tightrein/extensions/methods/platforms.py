"""平台读取方法的共用部分(redesign/01-collect.md 第 1、4 节)：从钥匙串取只读令牌、组装认证请求头、发出只读 GET 请求。

令牌在方法进程内经 config/secrets.Keychain 读取，只放进请求头，不写进输出、日志与错误信息(扩展进程的环境变量会滤掉
凭证，因此不经环境变量传入)。请求失败、非 2xx 与响应不是 JSON 分别以 source-unavailable 与 parse-failed 结束。
"""

from __future__ import annotations

import base64
import json
import re
import subprocess
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from tightrein.config.secrets import Keychain, SecretError
from tightrein.domain.clock import format_iso, parse_iso
from tightrein.domain.enums import ExtensionErrorCode
from tightrein.extensions.invoke import ProcessRequest
from tightrein.extensions.methods.runtime import MethodContext, MethodError
from tightrein.sources.common.http import HttpRequest, HttpResponse

FRACTION = re.compile(r"\.\d+(?=(Z|[+-]\d{2}:?\d{2})?$)")
KEYCHAIN_HINT = "用 security add-generic-password -s <条目名> -a <账号> -w 存放只读令牌"


def keychain(context: MethodContext) -> Keychain:
    """经方法的进程执行器调用 security，测试注入假的执行器。"""

    def run(args: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
        outcome = context.runner(ProcessRequest(tuple(args), Path.cwd(), dict(context.environ), b"", timeout))
        if outcome.start_error is not None:
            raise OSError(outcome.start_error)
        return subprocess.CompletedProcess(list(args), outcome.exit_code or 0,
                                           outcome.stdout.decode("utf-8", errors="replace"),
                                           outcome.stderr.decode("utf-8", errors="replace"))

    return Keychain(lambda value: None, run=run)


def token(context: MethodContext, item: str | None) -> str | None:
    """钥匙串条目中的令牌；条目名为空时不带认证。"""
    if item is None:
        return None
    try:
        return keychain(context).read(item).value
    except SecretError as error:
        raise MethodError(ExtensionErrorCode.SOURCE_UNAVAILABLE, str(error), KEYCHAIN_HINT) from error


def auth_headers(secret: str | None, user: str | None = None) -> dict[str, str]:
    """有用户名时为基本认证，否则为 Bearer；没有令牌时为空。"""
    if secret is None:
        return {}
    if user is not None:
        encoded = base64.b64encode(f"{user}:{secret}".encode("utf-8")).decode("ascii")
        return {"Authorization": f"Basic {encoded}"}
    return {"Authorization": f"Bearer {secret}"}


def get(context: MethodContext, url: str, query: Mapping[str, Any] | Sequence[tuple[str, Any]] | None,
        headers: Mapping[str, str], timeout: float, platform: str) -> HttpResponse:
    full = f"{url}?{urlencode(query, doseq=True)}" if query else url
    response = context.transport(HttpRequest("GET", full, {**headers, "Accept": "application/json"}, None, timeout))
    if response.status is None:
        raise MethodError(ExtensionErrorCode.SOURCE_UNAVAILABLE, f"无法访问 {platform}：{response.error}")
    if not response.ok:
        raise MethodError(ExtensionErrorCode.SOURCE_UNAVAILABLE, f"{platform} 返回 {response.status}",
                          "确认地址正确、令牌有效且有只读权限")
    return response


def parse_json(response: HttpResponse, platform: str) -> Any:
    try:
        return json.loads(response.text())
    except ValueError as error:
        raise MethodError(ExtensionErrorCode.PARSE_FAILED, f"{platform} 的响应不是 JSON") from error


def get_json(context: MethodContext, url: str, query: Mapping[str, Any] | Sequence[tuple[str, Any]] | None,
             headers: Mapping[str, str], timeout: float, platform: str) -> Any:
    return parse_json(get(context, url, query, headers, timeout, platform), platform)


def oldest_available(now: datetime, retention_days: int) -> str:
    """按保留天数估算平台还能查到的最早时间。"""
    return format_iso(now - timedelta(days=retention_days))


def utc(value: str) -> str:
    """平台给出的带时区时间换算为 UTC 的 format_iso 写法；秒以下的部分(可能多于 6 位)去掉。无法解析时以
    parse-failed 结束。"""
    try:
        return format_iso(parse_iso(FRACTION.sub("", value)))
    except ValueError as error:
        raise MethodError(ExtensionErrorCode.PARSE_FAILED, f"无法识别的时间：{value}") from error
