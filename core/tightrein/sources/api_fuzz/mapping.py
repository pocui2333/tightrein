"""失败条目的过滤与信号映射(architecture/04 2.6)，以及重放时共用的判定。

过滤：
- status_code_conformance 失败、状态码为 401 或 403：有越权模型且该角色本就缺少端点所需的能力时丢弃(预期的拒绝)；
  没有模型，或模型中查不到该端点或角色(匿名身份不在模型中)时无法判断，丢弃并计入 unjudgedAuthFailures；
- 同一角色、同一操作、同一检查的多个失败用例只保留第一条，数量记入 context.sameCaseCount。
信号：source 为 synthetic，check 为 Schemathesis 的检查名，location 为「方法 路由模板」(取失败所属的操作)，
message 为失败标题加说明的第一行(服务端错误写成「服务端返回 <状态码>」，越权检查直接用检查给出的说明)，
occurred_at 为该交互的记录时间，取不到时用该角色调用的结束时间。请求体超过 16 KB 时写入 raw/ 并以 bodyRef 引用。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from tightrein.domain.enums import Source
from tightrein.domain.signal import Signal
from tightrein.sources.api_fuzz.authz.model import AuthzModel
from tightrein.sources.api_fuzz.report_parser import Failure, RoleReport
from tightrein.sources.common.redact import TOKEN_PLACEHOLDER, ProbeRedactor
from tightrein.sources.common.session import BEARER_AUTH
from tightrein.sources.common.signals import SignalFactory, serialized_size

SERVER_ERROR_CHECK = "not_a_server_error"
STATUS_CHECK = "status_code_conformance"
UNAUTHORIZED_CHECK = "unauthorized_role_access"
RESPONSE_TIME_CHECK = "max_response_time"
AUTH_STATUSES = (401, 403)
SERVER_ERROR_MIN = 500
JSON_CONTENT_TYPE = "application/json"
DEFAULT_RESPONSE_KEY = "default"


@dataclass(frozen=True)
class RoleContext:
    role: str
    report_dir: str
    finished_at: datetime
    seed: int | None
    release: str | None
    tokens: tuple[str, ...] = ()
    auth: tuple[str, str] | None = BEARER_AUTH


@dataclass(frozen=True)
class MappingResult:
    signals: tuple[Signal, ...]
    unjudged_auth_failures: int
    expected_denials: int


def judge(check: str, status: int | None, elapsed_ms: int | None, *, max_response_ms: float | None = None,
          missing_capabilities: tuple[str, ...] | None = None,
          documented_statuses: Iterable[str] | None = None) -> bool | None:
    """按检查名判断一次响应是否仍然失败；重放无法判断的检查(例如响应体与描述是否一致)返回空。"""
    if status is None:
        return None
    if check == SERVER_ERROR_CHECK:
        return status >= SERVER_ERROR_MIN
    if check == UNAUTHORIZED_CHECK:
        if missing_capabilities is None:
            return None
        return bool(missing_capabilities) and 200 <= status < 300
    if check == RESPONSE_TIME_CHECK:
        if max_response_ms is None or elapsed_ms is None:
            return None
        return elapsed_ms > max_response_ms
    if check == STATUS_CHECK:
        if documented_statuses is None:
            return None
        documented = set(documented_statuses)
        return DEFAULT_RESPONSE_KEY not in documented and str(status) not in documented
    return None


def _status(failure: Failure) -> int | None:
    return None if failure.interaction is None else failure.interaction.status


def auth_decision(failure: Failure, role: str, model: AuthzModel | None) -> str | None:
    """401、403 的状态码不符：expected(预期的拒绝)、unjudged(无法判断)；其他失败为空。"""
    if failure.check != STATUS_CHECK or _status(failure) not in AUTH_STATUSES:
        return None
    if model is None:
        return "unjudged"
    missing = model.missing_capabilities(role, *failure.operation)
    if missing is None:
        return "unjudged"
    return "expected" if missing else None


def group(failures: Iterable[Failure], role: str,
          model: AuthzModel | None) -> tuple[list[tuple[Failure, int]], int, int]:
    """过滤后按(检查, 操作)分组，返回 [(第一条失败, 数量)]、无法判断的 401/403 数与预期的拒绝数。"""
    groups: dict[tuple[str, tuple[str, str]], list[Failure]] = {}
    unjudged = expected = 0
    for failure in failures:
        decision = auth_decision(failure, role, model)
        if decision == "unjudged":
            unjudged += 1
            continue
        if decision == "expected":
            expected += 1
            continue
        groups.setdefault((failure.check, failure.operation), []).append(failure)
    return [(items[0], len(items)) for items in groups.values()], unjudged, expected


def describe(failure: Failure) -> str:
    status = _status(failure)
    if failure.check == SERVER_ERROR_CHECK and status is not None:
        return f"服务端返回 {status}"
    lines = failure.message.strip().splitlines()
    first = lines[0].strip() if lines else ""
    if failure.check == UNAUTHORIZED_CHECK:
        return first or failure.title
    return f"{failure.title}：{first}" if first else failure.title


def _quote(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


def reproduce(method: str, url: str, body: Any, media_type: str | None, body_file: str | None = None,
              auth: tuple[str, str] | None = BEARER_AUTH) -> str:
    """curl 形式的复现命令，凭证请求头的值写成占位符(匿名身份不带)；请求体写在原始输出中时以
    `-d @<相对原始输出目录的路径>` 引用。"""
    parts = ["curl", "-X", method, _quote(url)]
    if auth is not None:
        parts += ["-H", _quote(f"{auth[0]}: {auth[1]}{TOKEN_PLACEHOLDER}")]
    if body is not None:
        payload = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
        data = f"@{body_file}" if body_file is not None else payload
        parts += ["-H", _quote(f"Content-Type: {media_type or JSON_CONTENT_TYPE}"), "-d", _quote(data)]
    return " ".join(parts)


def _occurred(failure: Failure, fallback: datetime) -> datetime:
    if failure.interaction is None or failure.interaction.timestamp is None:
        return fallback
    return datetime.fromtimestamp(failure.interaction.timestamp, timezone.utc)


def _method(failure: Failure) -> str:
    return failure.case.method if failure.interaction is None else failure.interaction.method


def _url(failure: Failure, base_url: str) -> str:
    if failure.interaction is None:
        return base_url.rstrip("/") + failure.case.path_template
    return failure.interaction.uri


def _request(failure: Failure, redactor: ProbeRedactor, factory: SignalFactory, role: str) -> dict[str, Any]:
    path = failure.case.path_template if failure.interaction is None else urlsplit(failure.interaction.uri).path
    request: dict[str, Any] = {"method": _method(failure), "path": path, "pathTemplate": failure.operation[1],
                               "query": redactor.query(failure.case.query)}
    body = redactor.value(failure.case.body)
    if body is not None and serialized_size(body) > redactor.limits.context_bytes:
        request["bodyRef"] = factory.raw.write_json(f"refs/{role}-{failure.case_id}-body.json", body)
    else:
        request["body"] = body
    return request


def _response(failure: Failure, redactor: ProbeRedactor) -> dict[str, Any] | None:
    interaction = failure.interaction
    if interaction is None:
        return None
    return {"status": interaction.status, "elapsedMs": interaction.elapsed_ms,
            "bodyExcerpt": redactor.excerpt(interaction.body.decode("utf-8", errors="replace"))}


def _capabilities(failure: Failure, role: str, model: AuthzModel | None) -> dict[str, Any]:
    if failure.check != UNAUTHORIZED_CHECK or model is None:
        return {}
    rule = model.rule(*failure.operation)
    return {"requiredCapabilities": [] if rule is None else list(rule.requires),
            "grantedCapabilities": list(model.role_capabilities(role))}


def to_signals(report: RoleReport, context: RoleContext, model: AuthzModel | None, factory: SignalFactory,
               redactor: ProbeRedactor, base_url: str) -> MappingResult:
    kept, unjudged, expected = group(report.failures, context.role, model)
    signals = []
    for failure, count in kept:
        request = _request(failure, redactor, factory, context.role)
        command = reproduce(_method(failure), redactor.url(_url(failure, base_url)), failure.case.body,
                            failure.case.media_type, request.get("bodyRef"), context.auth)
        signals.append(factory.create(
            source=Source.SYNTHETIC, check=failure.check, location=f"{failure.operation[0]} {failure.operation[1]}",
            message=describe(failure), occurred_at=_occurred(failure, context.finished_at), release=context.release,
            context={
                "role": context.role,
                "request": request,
                "response": _response(failure, redactor),
                "reproduce": redactor.reproduce(command, context.tokens),
                "seed": context.seed,
                "sameCaseCount": count,
                "reportPath": context.report_dir,
                "phase": failure.phase,
                **_capabilities(failure, context.role, model),
            },
            actor={"id": context.role, "role": context.role},
        ))
    return MappingResult(tuple(signals), unjudged, expected)


def documented_statuses(spec: Mapping[str, Any], method: str, route: str) -> tuple[str, ...] | None:
    """接口描述中该操作声明的响应状态码；操作不存在时为空。"""
    operation = spec.get("paths", {}).get(route, {}).get(method.lower())
    if not isinstance(operation, Mapping):
        return None
    return tuple(str(key) for key in operation.get("responses", {}))
