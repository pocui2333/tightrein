"""采集读取平台(Sentry、Loki、Alertmanager)的只读 HTTP 请求。以 Transport 注入，测试用按请求应答的假实现，不联网。

- 4xx、5xx 照常返回状态码与响应体，不抛异常；没有得到响应(连接失败、超时)时 status 为 None，error 写明原因；
- 耗时用单调时钟，精确到毫秒；
- 给出代理表时按它发送，主机命中 no 项的直连；没给出时按进程环境变量(含 no_proxy)处理；
- 平台令牌只放进请求头，不进输出、日志与错误信息；非 2xx 报 Unavailable，响应不是 JSON 报 Invalid。
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from tightrein.protocol.external import Invalid, Unavailable

MILLISECONDS_PER_SECOND = 1000

Query = Mapping[str, Any] | Sequence[tuple[str, Any]]


@dataclass(frozen=True)
class HttpRequest:
    method: str
    url: str
    timeout_s: float
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes | None = None


@dataclass(frozen=True)
class HttpResponse:
    status: int | None
    body: bytes = b""
    headers: Mapping[str, str] = field(default_factory=dict)
    elapsed_ms: int = 0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def header(self, name: str) -> str | None:
        wanted = name.lower()
        return next((value for key, value in self.headers.items() if key.lower() == wanted), None)


Transport = Callable[[HttpRequest], HttpResponse]


class UrllibTransport:
    def __init__(self, monotonic: Callable[[], float] = time.monotonic,
                 proxies: Mapping[str, str] | None = None) -> None:
        self.monotonic = monotonic
        self.proxies = None if proxies is None else dict(proxies)

    def opener(self, url: str) -> urllib.request.OpenerDirector | None:
        """给出代理表时按它建 opener，主机在 no 项中时不经代理；没有代理表时为 None(用 urllib 缺省)。"""
        if self.proxies is None:
            return None
        host = urllib.parse.urlsplit(url).hostname or ""
        # proxy_bypass_environment 是 urllib 的公开函数(3.12)，typeshed 没有收录
        direct = urllib.request.proxy_bypass_environment(host, self.proxies)  # type: ignore[attr-defined]
        table = {} if direct else {scheme: value for scheme, value in self.proxies.items() if scheme != "no"}
        return urllib.request.build_opener(urllib.request.ProxyHandler(table))

    def __call__(self, request: HttpRequest) -> HttpResponse:
        prepared = urllib.request.Request(request.url, data=request.body, headers=dict(request.headers),
                                          method=request.method)
        opener = self.opener(request.url)
        started = self.monotonic()
        try:
            opened = (opener.open(prepared, timeout=request.timeout_s) if opener is not None
                      else urllib.request.urlopen(prepared, timeout=request.timeout_s))
            with opened as response:
                body = response.read()
                return HttpResponse(response.status, body, dict(response.headers.items()), self._elapsed(started))
        except urllib.error.HTTPError as error:
            return HttpResponse(error.code, error.read(), dict(error.headers.items()), self._elapsed(started))
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            reason = getattr(error, "reason", error)
            return HttpResponse(None, elapsed_ms=self._elapsed(started), error=f"{type(error).__name__}: {reason}")

    def _elapsed(self, started: float) -> int:
        return round((self.monotonic() - started) * MILLISECONDS_PER_SECOND)


@dataclass(frozen=True)
class Platform:
    """一个平台的只读访问：地址、认证与超时。令牌只在请求头里出现。"""

    name: str  # 显示用：Sentry、Loki
    transport: Transport
    timeout_s: float
    headers: Mapping[str, str] = field(default_factory=dict)

    def get(self, url: str, query: Query | None = None) -> HttpResponse:
        full = f"{url}?{urlencode(query, doseq=True)}" if query else url
        headers = {**self.headers, "Accept": "application/json"}
        response = self.transport(HttpRequest("GET", full, self.timeout_s, headers))
        if response.status is None:
            raise Unavailable(f"无法访问 {self.name}：{response.error}")
        if not response.ok:
            raise Unavailable(f"{self.name} 返回 {response.status}：确认地址正确、令牌有效且有只读权限")
        return response

    def get_json(self, url: str, query: Query | None = None) -> Any:
        return parse_json(self.get(url, query), self.name)


def auth_headers(token: str | None, user: str | None = None) -> dict[str, str]:
    """有用户名时为基本认证(Grafana Cloud 是实例编号加令牌)，否则为 Bearer；没有令牌时不带认证。"""
    if token is None:
        return {}
    if user is not None:
        encoded = base64.b64encode(f"{user}:{token}".encode()).decode("ascii")
        return {"Authorization": f"Basic {encoded}"}
    return {"Authorization": f"Bearer {token}"}


def parse_json(response: HttpResponse, platform: str) -> Any:
    try:
        return json.loads(response.text())
    except ValueError as error:
        raise Invalid(f"{platform} 的响应不是 JSON") from error
