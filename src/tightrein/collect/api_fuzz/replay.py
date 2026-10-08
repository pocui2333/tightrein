"""重放一条记录过的请求，供去重的复现确认调用(collect/dedup)：`replay(runtime, evidence)`。

- 请求取自信号证据的 request：方法、实际路径、查询参数与请求体；请求体在 bodyRef 时(或整个 request 因证据超限被
  移成 requestRef 时)从信号所在运行的原始输出目录读回；采集时已脱敏的字段按脱敏后的值发送；
- 被测地址取 sites.json 的 target.baseUrl，登录方式取 api_fuzz.login(同采集)；
- 用新取得的凭证发送：一次运行内只登录一次(按运行编号缓存)；
- 只按状态码判定：5xx 为 reproduced，其余为 not_reproduced；没有记录请求、没有目标地址、登录失败、没有得到响应都是
  unavailable(无法判断)，不能当成「未复现」，去重据此保持待确认、下次再试。
"""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Literal
from urllib.parse import urlencode, urlsplit

from tightrein.collect.api_fuzz import limits, login
from tightrein.collect.api_fuzz.login import Credential, LoginFailed, LoginSettings
from tightrein.protocol.http import HttpRequest, HttpResponse, Transport, UrllibTransport
from tightrein.protocol.raw import RawDir, raw_dir
from tightrein.protocol.runtime import Runtime

SOURCE = "collect.api_fuzz"
REPRODUCED: Final = "reproduced"
NOT_REPRODUCED: Final = "not_reproduced"
UNAVAILABLE: Final = "unavailable"
SERVER_ERROR_MIN = 500
JSON_CONTENT_TYPE = "application/json"
REQUEST_KEY = "request"
REF_SUFFIX = "Ref"

Verdict = Literal["reproduced", "not_reproduced", "unavailable"]

_sessions: dict[str, Credential | LoginFailed | None] = {}  # 运行编号 → 凭证、登录失败或匿名
_sessions_lock = threading.Lock()


@dataclass(frozen=True)
class RecordedRequest:
    method: str
    path: str
    query: Mapping[str, Any] = field(default_factory=dict)
    body: Any = None

    @classmethod
    def from_evidence(cls, evidence: Mapping[str, Any], raw: RawDir) -> RecordedRequest | None:
        """证据中没有记录请求时为 None。"""
        request = evidence.get(REQUEST_KEY)
        reference = evidence.get(REQUEST_KEY + REF_SUFFIX)
        if request is None and reference is not None:
            request = _read(raw, reference)
        if not isinstance(request, Mapping):
            return None
        body = _read(raw, request["bodyRef"]) if "bodyRef" in request else request.get("body")
        return cls(str(request["method"]).upper(), str(request["path"]), request.get("query") or {}, body)


def replay(runtime: Runtime, evidence: Mapping[str, Any], *, transport: Transport | None = None) -> Verdict:
    sites = runtime.settings.sites.get(limits.SITE) or {}
    base_url = limits.Target.from_sites(runtime.settings.sites).base_url
    run = evidence.get("run")
    if not base_url or not run:
        return UNAVAILABLE
    request = RecordedRequest.from_evidence(evidence, RawDir(raw_dir(runtime.workspace, str(run), SOURCE)))
    if request is None:
        return UNAVAILABLE
    sender = transport or UrllibTransport()
    timeout_s = runtime.settings.duration("limits.timeouts.http")
    try:
        credential = _credential(runtime, sites, base_url, sender, timeout_s)
    except LoginFailed:
        return UNAVAILABLE
    return judge(send(request, base_url, credential, sender, timeout_s))


def remember(run: str, credential: Credential | None) -> None:
    """采集时已登录的凭证留给同一次运行的重放用，不再登录一次。"""
    with _sessions_lock:
        _sessions[run] = credential


def send(request: RecordedRequest, base_url: str, credential: Credential | None, transport: Transport,
         timeout_s: float) -> HttpResponse:
    headers = {**(credential.headers() if credential is not None else {}), "Accept": JSON_CONTENT_TYPE}
    body = None
    if request.body is not None:
        body = (request.body if isinstance(request.body, str) else json.dumps(request.body)).encode("utf-8")
        headers["Content-Type"] = JSON_CONTENT_TYPE
    url = login.join_url(base_url, relative_path(base_url, request.path))
    if request.query:
        url += "?" + urlencode(dict(request.query), doseq=True)
    return transport(HttpRequest(request.method, url, timeout_s, headers, body))


def relative_path(base_url: str, path: str) -> str:
    """记录的是实际请求的完整路径：目标地址带路径前缀(如 https://h/api)时去掉它，拼回时才不会重复。"""
    prefix = urlsplit(base_url).path.rstrip("/")
    return path[len(prefix):] if prefix and path.startswith(prefix + "/") else path


def judge(response: HttpResponse) -> Verdict:
    if response.status is None:
        return UNAVAILABLE
    return REPRODUCED if response.status >= SERVER_ERROR_MIN else NOT_REPRODUCED


def _credential(runtime: Runtime, sites: Mapping[str, Any], base_url: str, transport: Transport,
                timeout_s: float) -> Credential | None:
    """登录失败也记下：同一次运行内不反复请求登录接口。"""
    with _sessions_lock:
        if runtime.run not in _sessions:
            try:
                _sessions[runtime.run] = login.login(LoginSettings.from_sites(sites), base_url=base_url,
                                                     secrets=runtime.secrets, transport=transport,
                                                     redactor=runtime.redactor, timeout_s=timeout_s)
            except LoginFailed as error:
                _sessions[runtime.run] = error
        found = _sessions[runtime.run]
    if isinstance(found, LoginFailed):
        raise found
    return found


def _read(raw: RawDir, relative: str) -> Any:
    return json.loads(raw.path(relative).read_text(encoding="utf-8"))
