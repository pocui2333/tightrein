"""失败条目 → 信号(只有服务端报错)。

- 同一操作、同一检查的多个失败用例只留第一条，数量记进 sameCaseCount，避免一个缺陷刷出几十条信号；
- check_type 为 server_error，location 为「方法 路由模板」(取失败所属的操作)；message 统一写「服务端返回 <状态码>」，
  不带每次不同的响应内容；occurred_at 取交互的记录时间，取不到时用调用结束时间；
- 证据：request(方法、实际路径、路由模板、脱敏后的查询参数与请求体；请求体超过上限时写进原始输出 `refs/` 并以
  bodyRef 引用)、response(状态码、耗时、脱敏后截断的响应摘录)、status(去重按状态码大类算指纹)、reproduce(curl，
  凭证请求头的值是占位符，请求体在 refs/ 时用 `-d @<相对路径>`；URL 的查询参数按键名逐个脱敏，片段丢弃)、seed、sameCaseCount、reportPath、phase，
  以及 run(重放时据此找回原始输出目录)；
- 5xx 几乎没有误报：deterministic 为真，不走去重的「偶发先观察」，由去重重放确认。
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from tightrein.collect.api_fuzz.schemathesis.invoke import SERVER_ERROR_CHECK
from tightrein.collect.api_fuzz.schemathesis.report_parser import Failure, Report
from tightrein.collect.common.signals import Signal, SignalFactory, serialized_size
from tightrein.protocol.security import Redactor

CHECK_TYPE = "server_error"
TOKEN_PLACEHOLDER = "<TOKEN>"
JSON_CONTENT_TYPE = "application/json"
REFS_DIR = "refs"
# 文本脱敏会把「请求头: 值」当成键值对整个替换，复现命令里的凭证请求头脱敏后改回占位符
_CREDENTIAL_HEADER = "(-H\\s+'{header}:\\s*)[^']*(')"

ReleaseAt = Callable[[datetime], str | None]


@dataclass(frozen=True)
class MappingContext:
    run: str
    finished_at: datetime  # 调用结束时间：交互没有记录时间时用它
    seed: int | None
    base_url: str
    auth: tuple[str, str] | None  # 凭证请求头与值的前缀；匿名为 None
    environment: str | None
    report_path: str  # 报告目录(相对原始输出目录)
    body_bytes: int  # 请求体超过它就写进 refs/
    release_at: ReleaseAt


def group(failures: Iterable[Failure]) -> list[tuple[Failure, int]]:
    """只留服务端报错；同一检查、同一操作的只留第一条，带上数量。"""
    groups: dict[tuple[str, tuple[str, str]], list[Failure]] = {}
    for failure in failures:
        if failure.check == SERVER_ERROR_CHECK:
            groups.setdefault((failure.check, failure.operation), []).append(failure)
    return [(items[0], len(items)) for items in groups.values()]


def describe(failure: Failure) -> str:
    status = _status(failure)
    return f"服务端返回 {status}" if status is not None else failure.title


def reproduce(method: str, url: str, body: Any, media_type: str | None, body_file: str | None,
              auth: tuple[str, str] | None) -> str:
    """curl 形式的复现命令；凭证请求头的值写成占位符，匿名不带；请求体写在 refs/ 时(body_file)用 `-d @<相对路径>`。"""
    parts = ["curl", "-X", method, _quote(url)]
    if auth is not None:
        parts += ["-H", _quote(f"{auth[0]}: {auth[1]}{TOKEN_PLACEHOLDER}")]
    if body_file is not None:
        parts += ["-H", _quote(f"Content-Type: {media_type or JSON_CONTENT_TYPE}"), "-d", _quote(f"@{body_file}")]
    elif body is not None:
        payload = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
        parts += ["-H", _quote(f"Content-Type: {media_type or JSON_CONTENT_TYPE}"), "-d", _quote(payload)]
    return " ".join(parts)


def redact_reproduce(command: str, redactor: Redactor, auth: tuple[str, str] | None) -> str:
    """先做文本脱敏，再把凭证请求头改回占位符(文本规则会把它整个替换掉)。"""
    cleaned = redactor.text(command)
    if auth is None:
        return cleaned
    pattern = re.compile(_CREDENTIAL_HEADER.format(header=re.escape(auth[0])), re.IGNORECASE)
    return pattern.sub(lambda match: f"{match.group(1)}{auth[1]}{TOKEN_PLACEHOLDER}{match.group(2)}", cleaned)


def to_signals(report: Report, context: MappingContext, factory: SignalFactory, redactor: Redactor) -> list[Signal]:
    signals = []
    for failure, count in group(report.failures):
        request = _request(failure, context, factory, redactor)
        command = reproduce(_method(failure), redactor.text(_url(failure, context.base_url, redactor)), request.get("body"),
                            failure.case.media_type, request.get("bodyRef"), context.auth)
        occurred = _occurred(failure, context.finished_at)
        signals.append(factory.create(
            check_type=CHECK_TYPE, location=f"{failure.operation[0]} {failure.operation[1]}",
            message=describe(failure), occurred_at=occurred, commit=context.release_at(occurred),
            environment=context.environment, deterministic=True,
            evidence={
                "request": request,
                "response": _response(failure, factory),
                "status": _status(failure),
                "reproduce": redact_reproduce(command, redactor, context.auth),
                "seed": context.seed,
                "sameCaseCount": count,
                "reportPath": context.report_path,
                "phase": failure.phase,
                "run": context.run,
            },
        ))
    return signals


def _status(failure: Failure) -> int | None:
    return None if failure.interaction is None else failure.interaction.status


def _quote(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


def _occurred(failure: Failure, fallback: datetime) -> datetime:
    if failure.interaction is None or failure.interaction.timestamp is None:
        return fallback
    return datetime.fromtimestamp(failure.interaction.timestamp, UTC)


def _method(failure: Failure) -> str:
    return failure.case.method if failure.interaction is None else failure.interaction.method


def _url(failure: Failure, base_url: str, redactor: Redactor) -> str:
    """查询参数按键名逐个脱敏(键名敏感的整个值替换)，片段(#…)丢弃：片段只在浏览器里用，常带令牌。"""
    if failure.interaction is None:
        return base_url.rstrip("/") + failure.case.path_template
    parts = urlsplit(failure.interaction.uri)
    pairs = [(key, str(redactor.mapping({key: value})[key]))
             for key, value in parse_qsl(parts.query, keep_blank_values=True)]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(pairs, safe="[]:"), ""))


def _request(failure: Failure, context: MappingContext, factory: SignalFactory,
             redactor: Redactor) -> dict[str, Any]:
    path = failure.case.path_template if failure.interaction is None else urlsplit(failure.interaction.uri).path
    request: dict[str, Any] = {"method": _method(failure), "path": path, "pathTemplate": failure.operation[1],
                               "query": redactor.mapping(dict(failure.case.query))}
    body = redactor.mapping(failure.case.body)
    if body is not None and serialized_size(body) > context.body_bytes:
        request["bodyRef"] = factory.raw.write_json(f"{REFS_DIR}/{failure.case_id}-body.json", body)
    else:
        request["body"] = body
    return request


def _response(failure: Failure, factory: SignalFactory) -> dict[str, Any] | None:
    interaction = failure.interaction
    if interaction is None:
        return None
    return {"status": interaction.status, "elapsedMs": interaction.elapsed_ms,
            "bodyExcerpt": factory.excerpt(interaction.body.decode("utf-8", errors="replace"))}
