"""openapi_url：从运行中的服务读取接口描述(JSON 或 YAML)。

只允许 http 与 https；GET 请求，没有得到响应或状态码不是 2xx 时报来源不可用，提示确认服务已启动、地址正确。
服务由部署或调用方事先启动，本方法不启动任何进程；每次重新读，不缓存(同一 commit 的服务也可能换了配置)。
"""

from __future__ import annotations

from urllib.parse import urlsplit

from tightrein.collect.api_fuzz.spec import SpecRequest, SpecText
from tightrein.collect.common.source import SourceMisconfigured, SourceUnavailable
from tightrein.protocol.http import HttpRequest

ACCEPT = "application/json, application/yaml;q=0.9, */*;q=0.1"
SCHEMES = ("http", "https")
START_HINT = "确认服务已启动，地址与端口与 url 一致"


def cache_key(request: SpecRequest) -> str | None:
    return None


def describe(request: SpecRequest) -> str:
    return str(request.options["url"])


def fetch(request: SpecRequest) -> SpecText:
    url = str(request.options["url"])
    if urlsplit(url).scheme not in SCHEMES:
        raise SourceMisconfigured(f'controls."collect.api_fuzz".openapi_url.url 只接受 http 与 https：{url}')
    response = request.transport(HttpRequest("GET", url, float(request.options["timeoutSeconds"]),
                                             {"Accept": ACCEPT}))
    if response.status is None:
        raise SourceUnavailable(f"无法读取接口描述 {url}：{response.error}；{START_HINT}")
    if not response.ok:
        raise SourceUnavailable(f"接口描述 {url} 返回 {response.status}；{START_HINT}")
    return SpecText(response.text(), url)
