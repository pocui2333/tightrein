"""core/loki：在 Grafana Loki 上按 LogQL 查询时间窗口内的日志原文(redesign/01-collect.md 第 1 节)。

GET <url>/loki/api/v1/query_range，参数 query、start、end(纳秒)、limit(每页 pageSize)、direction=forward；
一页取满时从本页最后一条之后继续，读满 input.limit 条仍未到窗口终点时 truncated 为真。每个标签组合(流)一个片段，
流名为标签的 {k="v",...} 写法，片段正文为按时间排列的原文行；字段映射由 log-parse(core/json-lines、core/regex)完成。
认证：有 user 时为基本认证(Grafana Cloud 为实例编号加访问令牌)，否则为 Bearer；keychainItem 为空时不带认证；
tenant 给出时带 X-Scope-OrgID。oldestAvailable 按 retentionDays 估算。
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tightrein.domain.clock import format_iso, parse_iso
from tightrein.domain.enums import ExtensionErrorCode
from tightrein.extensions.methods import platforms, runtime
from tightrein.extensions.methods.runtime import MethodContext, MethodError, MethodRequest, MethodResult

MANIFEST = Path(__file__).with_suffix(".yaml")
PLATFORM = "Loki"
NANOSECONDS = 1_000_000_000
TENANT_HEADER = "X-Scope-OrgID"


def nanoseconds(value: str) -> int:
    moment = parse_iso(value)
    return int(moment.timestamp()) * NANOSECONDS + moment.microsecond * 1000


def stream_name(labels: Mapping[str, str]) -> str:
    return "{" + ",".join(f'{key}="{value}"' for key, value in sorted(labels.items())) + "}"


def entries(document: Any) -> list[tuple[int, str, str]]:
    """(纳秒时间, 流名, 原文行)，按时间排序。"""
    data = document.get("data") if isinstance(document, Mapping) else None
    if not isinstance(data, Mapping) or data.get("resultType") != "streams":
        raise MethodError(ExtensionErrorCode.PARSE_FAILED, "Loki 的响应不是日志流(resultType 应为 streams)",
                          "查询须是日志查询，不能是指标查询")
    found = []
    for stream in data.get("result") or []:
        name = stream_name(stream.get("stream") or {})
        found += [(int(value[0]), name, str(value[1])) for value in stream.get("values") or []]
    return sorted(found)


def chunks(found: list[tuple[int, str, str]]) -> list[dict[str, Any]]:
    streams: dict[str, list[tuple[int, str]]] = {}
    for at, name, line in found:
        streams.setdefault(name, []).append((at, line))
    result = []
    for name, lines in streams.items():
        text = "".join(line.rstrip("\n") + "\n" for _, line in lines)
        last = datetime.fromtimestamp(lines[-1][0] / NANOSECONDS, timezone.utc)
        result.append({"stream": name, "text": text, "startPosition": 0, "endPosition": len(text.encode("utf-8")),
                       "modifiedAt": format_iso(last)})
    return result


def run(request: MethodRequest, context: MethodContext) -> MethodResult:
    options = request.options
    headers = platforms.auth_headers(platforms.token(context, options["keychainItem"]), options["user"])
    if options["tenant"]:
        headers[TENANT_HEADER] = options["tenant"]
    url = options["url"].rstrip("/") + "/loki/api/v1/query_range"
    start, end = nanoseconds(request.input["since"]), nanoseconds(request.input["until"])
    limit, page_size = int(request.input["limit"]), int(options["pageSize"])
    found: list[tuple[int, str, str]] = []
    truncated = False
    while start < end:
        size = min(page_size, limit - len(found))
        page = entries(platforms.get_json(context, url, {"query": request.input["query"], "start": start, "end": end,
                                                         "limit": size, "direction": "forward"},
                                          headers, float(options["timeoutSeconds"]), PLATFORM))
        found += page
        if len(page) < size:
            break
        if len(found) >= limit:
            truncated = True
            break
        start = page[-1][0] + 1
    notes = ("读满条数上限，窗口中其余日志下次运行继续读取",) if truncated else ()
    oldest = platforms.oldest_available(context.now(), int(options["retentionDays"]))
    return MethodResult({"chunks": chunks(found), "truncated": truncated, "oldestAvailable": oldest}, notes)


if __name__ == "__main__":
    sys.exit(runtime.serve(run, MANIFEST))
