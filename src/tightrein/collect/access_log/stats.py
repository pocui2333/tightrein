"""按接口统计与基线比较(纯计算)。

- 统计：每个接口(「方法 路由」)的请求数、p95 耗时(毫秒，按最近秩取；没有耗时的请求不参与 p95 但计入错误率)与 5xx 比例；
- 比较：本次与基线的请求数都不少于 min_requests 才比较(样本少不报)；p95 超过基线的 latency_ratio 倍为耗时退化，
  5xx 比例比基线高出 error_rate_delta 为错误率退化；
- 基线：按指数平均更新，weight 为本次窗口的权重；没有基线的接口以本次统计为基线。
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from tightrein.collect.access_log.parse import Request

SERVER_ERROR = 500
PERCENTILE = 0.95
LATENCY = "latency"
ERROR_RATE = "error_rate"


@dataclass(frozen=True)
class EndpointStats:
    requests: int
    p95_ms: float | None
    error_rate: float

    def to_json(self) -> dict[str, Any]:
        return {"requests": self.requests, "p95Ms": self.p95_ms, "errorRate": self.error_rate}

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> EndpointStats:
        return cls(int(data["requests"]), data.get("p95Ms"), float(data["errorRate"]))


@dataclass(frozen=True)
class Regression:
    endpoint: str
    check_type: str
    current: EndpointStats
    baseline: EndpointStats

    def describe(self) -> str:
        if self.check_type == LATENCY:
            return f"p95 耗时 {self.current.p95_ms:.0f} ms，基线 {self.baseline.p95_ms:.0f} ms"
        return f"5xx 比例 {self.current.error_rate:.1%}，基线 {self.baseline.error_rate:.1%}"


def summarize(requests: Iterable[Request]) -> dict[str, EndpointStats]:
    grouped: dict[str, list[Request]] = {}
    for request in requests:
        grouped.setdefault(request.endpoint, []).append(request)
    return {endpoint: EndpointStats(len(items),
                                    _p95([item.duration_ms for item in items if item.duration_ms is not None]),
                                    sum(1 for item in items if item.status >= SERVER_ERROR) / len(items))
            for endpoint, items in sorted(grouped.items())}


def compare(current: Mapping[str, EndpointStats], baseline: Mapping[str, EndpointStats], *, min_requests: int,
            latency_ratio: float, error_rate_delta: float) -> list[Regression]:
    found = []
    for endpoint, now in current.items():
        before = baseline.get(endpoint)
        if before is None or now.requests < min_requests or before.requests < min_requests:
            continue
        if now.p95_ms is not None and before.p95_ms and now.p95_ms > before.p95_ms * latency_ratio:
            found.append(Regression(endpoint, LATENCY, now, before))
        if now.error_rate - before.error_rate >= error_rate_delta:
            found.append(Regression(endpoint, ERROR_RATE, now, before))
    return found


def update(baseline: Mapping[str, EndpointStats], current: Mapping[str, EndpointStats],
           weight: float) -> dict[str, EndpointStats]:
    merged = dict(baseline)
    for endpoint, now in current.items():
        before = baseline.get(endpoint)
        merged[endpoint] = now if before is None else EndpointStats(
            round(weight * now.requests + (1 - weight) * before.requests), _blend(now.p95_ms, before.p95_ms, weight),
            weight * now.error_rate + (1 - weight) * before.error_rate)
    return merged


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(PERCENTILE * len(ordered)) - 1)]


def _blend(new: float | None, old: float | None, weight: float) -> float | None:
    if new is None or old is None:
        return new if old is None else old
    return weight * new + (1 - weight) * old
