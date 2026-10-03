"""重放一条已记录的请求(architecture/04 2.8)，供 aggregate 的复现确认、复现检查与 verify 复用。

RecordedRequest 取自信号的 context.request：方法、实际路径、查询参数与请求体；请求体写在 bodyRef 指向的文件中时
从信号所在运行的原始输出目录读出。重放用该角色的新凭证发送(凭证请求头见 common/session.py，匿名身份不带)，结果按原信号的 check 用 mapping.judge 重新判定：
still_failing 为真表示问题仍在，为假表示已不再出现，为空表示这类检查无法靠重放判断或请求没有得到响应。
采集时已脱敏的查询参数与请求体字段按脱敏后的值发送。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from tightrein.domain.signal import Signal
from tightrein.sources.api_fuzz.authz.model import AuthzModel
from tightrein.sources.api_fuzz.mapping import JSON_CONTENT_TYPE, UNAUTHORIZED_CHECK, judge
from tightrein.sources.base import ProbeTarget
from tightrein.sources.common.http import HttpRequest, HttpResponse, Transport
from tightrein.sources.common.raw import RawDir
from tightrein.sources.common.redact import ProbeRedactor
from tightrein.sources.common.session import LoginFailed, Session, join_url



@dataclass(frozen=True)
class RecordedRequest:
    method: str
    path: str
    path_template: str
    query: Mapping[str, Any] = field(default_factory=dict)
    body: Any = None

    @classmethod
    def from_context(cls, request: Mapping[str, Any], raw: RawDir | None = None) -> RecordedRequest:
        body = request.get("body")
        if "bodyRef" in request:
            if raw is None:
                raise ValueError(f"请求体在 {request['bodyRef']} 中，需要给出信号所在运行的原始输出目录")
            body = json.loads(raw.path(request["bodyRef"]).read_text(encoding="utf-8"))
        return cls(request["method"].upper(), request["path"], request["pathTemplate"], request.get("query") or {},
                   body)


@dataclass(frozen=True)
class ReplayResult:
    status: int | None
    elapsed_ms: int
    excerpt: str
    still_failing: bool | None
    error: str | None = None


def _url(base_url: str, request: RecordedRequest) -> str:
    url = join_url(base_url, request.path)
    if request.query:
        url += "?" + urlencode(dict(request.query), doseq=True)
    return url


def send(request: RecordedRequest, role: str, target: ProbeTarget, session: Session, transport: Transport,
         timeout_seconds: float) -> HttpResponse:
    """用该角色的新凭证发送一条请求(匿名身份不带凭证)；复现检查的接口类检查也经这里发送。"""
    if target.base_url is None:
        raise ValueError("重放需要目标地址")
    headers = {**session.headers(role), "Accept": JSON_CONTENT_TYPE}
    body = None
    if request.body is not None:
        body = (request.body if isinstance(request.body, str) else json.dumps(request.body)).encode("utf-8")
        headers["Content-Type"] = JSON_CONTENT_TYPE
    return transport(HttpRequest(request.method, _url(target.base_url, request), headers, body, timeout_seconds))


def replay(request: RecordedRequest, role: str, target: ProbeTarget, session: Session, transport: Transport, *,
           check: str, redactor: ProbeRedactor, model: AuthzModel | None = None,
           max_response_ms: float | None = None, documented_statuses: Iterable[str] | None = None,
           timeout_seconds: float) -> ReplayResult:
    response = send(request, role, target, session, transport, timeout_seconds)
    missing = None
    if check == UNAUTHORIZED_CHECK and model is not None:
        missing = model.missing_capabilities(role, request.method, request.path_template)
    verdict = judge(check, response.status, response.elapsed_ms, max_response_ms=max_response_ms,
                    missing_capabilities=missing, documented_statuses=documented_statuses)
    return ReplayResult(response.status, response.elapsed_ms, redactor.excerpt(response.text()), verdict,
                        response.error)


SignalReplayer = Callable[[Signal, int], list[bool | None]]


def signal_replayer(target: ProbeTarget, session: Session, transport: Transport, redactor: ProbeRedactor,
                    raw_dir: Callable[[Signal], RawDir], timeout_seconds: float) -> SignalReplayer:
    """aggregate 复现确认的重放器(architecture/05 3.4 第 5 步)：按信号的 context.request 与角色重放 attempts 次，
    每次的结果为 still_failing；没有记录请求或角色的信号、登录失败时为 None(无法判断)。"""

    def replayer(signal: Signal, attempts: int) -> list[bool | None]:
        context = signal.context or {}
        role = (signal.actor or {}).get("role")
        if "request" not in context or not role:
            return [None] * attempts
        request = RecordedRequest.from_context(context["request"], raw_dir(signal))
        found: list[bool | None] = []
        for _ in range(attempts):
            try:
                result = replay(request, role, target, session, transport, check=signal.check, redactor=redactor,
                                timeout_seconds=timeout_seconds)
            except LoginFailed:
                found.append(None)
                continue
            found.append(result.still_failing)
        return found

    return replayer
