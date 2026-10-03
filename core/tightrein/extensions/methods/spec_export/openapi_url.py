"""core/openapi-url：从本机启动的服务读取接口描述(architecture/10 1.4、3.1)。

以 GET 请求 options.url(只允许 http 与 https)，超时为 options.timeoutSeconds；没有得到响应或状态码不是 2xx 时以
source-unavailable 结束，提示先启动服务。服务由调用方事先启动，本方法不启动任何进程。
"""

from __future__ import annotations

import sys
from pathlib import Path

from tightrein.domain.enums import ExtensionErrorCode
from tightrein.extensions.methods import openapi, runtime
from tightrein.extensions.methods.runtime import MethodContext, MethodError, MethodRequest, MethodResult
from tightrein.sources.common.http import HttpRequest

MANIFEST = Path(__file__).with_suffix(".yaml")
TOOL = "openapi-url"
ACCEPT = "application/json, application/yaml;q=0.9, */*;q=0.1"
START_HINT = "确认服务已在本机启动，地址与端口与 options.url 一致"


def run(request: MethodRequest, context: MethodContext) -> MethodResult:
    url = request.options["url"]
    response = context.transport(HttpRequest("GET", url, {"Accept": ACCEPT}, None,
                                             float(request.options["timeoutSeconds"])))
    if response.status is None:
        raise MethodError(ExtensionErrorCode.SOURCE_UNAVAILABLE, f"无法读取 {url}：{response.error}", START_HINT)
    if not response.ok:
        raise MethodError(ExtensionErrorCode.SOURCE_UNAVAILABLE, f"{url} 返回 {response.status}", START_HINT)
    document = openapi.parse(response.text(), url)
    return openapi.export(document, Path(request.input["outputFile"]), url, TOOL)


if __name__ == "__main__":
    sys.exit(runtime.serve(run, MANIFEST))
