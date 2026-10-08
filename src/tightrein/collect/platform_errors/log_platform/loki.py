"""loki：在 Grafana Loki 上按 LogQL 查询时间窗口内的日志原文(只读 API)。

GET <url>/loki/api/v1/query_range，参数 query、start、end(纳秒)、limit(每页 pageSize)、direction=forward；
一页取满就从本页最后一条的纳秒时间加 1 继续，读满 limit 仍未到窗口终点时 truncated 为真(同一纳秒的多条跨页可能少读)。
每个标签组合(流)一个片段，流名为排序后的 `{k="v",...}`；结果类型不是 streams(指标查询)时报错。
认证：有 user 时为基本认证(Grafana Cloud 为实例编号加令牌)，否则为 Bearer；没有令牌不带认证；给了 tenant 时带
X-Scope-OrgID。oldestAvailable 按 retentionDays 估算。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from tightrein.collect.common.source import SourceInvalid
from tightrein.collect.common.window import oldest_available
from tightrein.collect.platform_errors.log_platform.chunks import Chunk, LogRead
from tightrein.protocol.http import Platform, Transport, auth_headers
from tightrein.protocol.methods import Configured

PLATFORM = "Loki"
NANOSECONDS = 1_000_000_000
TENANT_HEADER = "X-Scope-OrgID"
QUERY_PATH = "/loki/api/v1/query_range"


def read(configured: Configured, *, transport: Transport, timeout_s: float, query: str, since: datetime,
         until: datetime, limit: int, now: datetime) -> LogRead:
    options = configured.options
    headers = auth_headers(configured.token, options.get("user"))
    if options.get("tenant"):
        headers[TENANT_HEADER] = options["tenant"]
    platform = Platform(PLATFORM, transport, timeout_s, headers)
    url = options["url"].rstrip("/") + QUERY_PATH
    start, end = nanoseconds(since), nanoseconds(until)
    page_size = int(options["pageSize"])
    found: list[tuple[int, str, str]] = []
    truncated = False
    while start < end:
        size = min(page_size, limit - len(found))
        page = entries(platform.get_json(url, {"query": query, "start": start, "end": end, "limit": size,
                                               "direction": "forward"}))
        found += page
        if len(page) < size:
            break
        if len(found) >= limit:
            truncated = True
            break
        start = page[-1][0] + 1
    return LogRead(chunks(found), truncated, oldest_available(now, int(options["retentionDays"])))


def nanoseconds(moment: datetime) -> int:
    return int(moment.timestamp()) * NANOSECONDS + moment.microsecond * 1000


def stream_name(labels: Mapping[str, str]) -> str:
    return "{" + ",".join(f'{key}="{value}"' for key, value in sorted(labels.items())) + "}"


def entries(document: Any) -> list[tuple[int, str, str]]:
    """(纳秒时间, 流名, 原文行)，按时间排序。"""
    data = document.get("data") if isinstance(document, Mapping) else None
    if not isinstance(data, Mapping) or data.get("resultType") != "streams":
        raise SourceInvalid("Loki 的响应不是日志流(resultType 应为 streams)：查询须是日志查询，不能是指标查询")
    found = []
    for stream in data.get("result") or []:
        name = stream_name(stream.get("stream") or {})
        found += [(int(value[0]), name, str(value[1])) for value in stream.get("values") or []]
    return sorted(found)


def chunks(found: list[tuple[int, str, str]]) -> list[Chunk]:
    streams: dict[str, list[tuple[int, str]]] = {}
    for at, name, line in found:
        streams.setdefault(name, []).append((at, line))
    return [Chunk(name, "".join(line.rstrip("\n") + "\n" for _, line in lines), 0,
                  datetime.fromtimestamp(lines[-1][0] // NANOSECONDS, UTC))
            for name, lines in streams.items()]
