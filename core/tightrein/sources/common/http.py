"""探针自己发出的 HTTP 请求(登录、请求重放)。以 Transport 注入，测试用按请求应答的替身，不访问网络。

UrllibTransport 用标准库发送：4xx、5xx 照常返回状态码与响应体；连接失败、超时等没有得到响应的情况 status 为空，
error 写明原因。耗时由单调时钟计算，精确到毫秒。给出代理表(config.network.proxies)时按它发送，主机匹配其中的
no 项时直连；没有给出时按进程环境变量(含 no_proxy)处理。
"""

from __future__ import annotations

import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from tightrein.config import layers

MILLISECONDS_PER_SECOND = 1000


@dataclass(frozen=True)
class HttpRequest:
    method: str
    url: str
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes | None = None
    timeout_seconds: float = field(
        default_factory=lambda: float(layers.core_value("runtime.sources.httpTimeoutSeconds")))


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


Transport = Callable[[HttpRequest], HttpResponse]


class UrllibTransport:
    def __init__(self, monotonic: Callable[[], float] = time.monotonic,
                 proxies: Mapping[str, str] | None = None) -> None:
        self.monotonic = monotonic
        self.proxies = None if proxies is None else dict(proxies)

    def opener(self, url: str) -> urllib.request.OpenerDirector | None:
        """给出代理表时按它建立的 opener；主机在 no 项中时不经代理。没有代理表时为空，使用 urllib 的缺省 opener。"""
        if self.proxies is None:
            return None
        host = urllib.parse.urlsplit(url).hostname or ""
        direct = urllib.request.proxy_bypass_environment(host, self.proxies)
        table = {} if direct else {scheme: value for scheme, value in self.proxies.items() if scheme != "no"}
        return urllib.request.build_opener(urllib.request.ProxyHandler(table))

    def __call__(self, request: HttpRequest) -> HttpResponse:
        prepared = urllib.request.Request(request.url, data=request.body, headers=dict(request.headers),
                                          method=request.method)
        opener = self.opener(request.url)
        started = self.monotonic()
        try:
            opened = opener.open(prepared, timeout=request.timeout_seconds) if opener is not None \
                else urllib.request.urlopen(prepared, timeout=request.timeout_seconds)
            with opened as response:
                body = response.read()
                return HttpResponse(response.status, body, dict(response.headers.items()), self._elapsed(started))
        except urllib.error.HTTPError as error:
            body = error.read()
            return HttpResponse(error.code, body, dict(error.headers.items()), self._elapsed(started))
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            reason = getattr(error, "reason", error)
            return HttpResponse(None, elapsed_ms=self._elapsed(started), error=f"{type(error).__name__}: {reason}")

    def _elapsed(self, started: float) -> int:
        return round((self.monotonic() - started) * MILLISECONDS_PER_SECOND)
