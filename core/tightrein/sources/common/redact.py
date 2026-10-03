"""采集端脱敏(architecture/04 1.5)：规则与 observability/redact.py 共用一份，额外处理请求头、查询参数、URL、
响应摘录与复现命令。

- 请求头中的 Authorization、Cookie、Set-Cookie 整体替换；其余请求头与查询参数按键名与文本规则脱敏；
- 复现命令中本次使用过的 token 替换为 `<TOKEN>`，`Authorization` 请求头的取值一律写成 `Bearer <TOKEN>`；
- 响应摘录与日志原文摘录经脱敏后按 runtime.sources 的上限截断(ProbeLimits)；消息截断由信号构造负责。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from tightrein.config import layers
from tightrein.config.project import ProjectConfig
from tightrein.observability.redact import REDACTED, Redactor

TOKEN_PLACEHOLDER = "<TOKEN>"
HEADERS_REPLACED = frozenset({"authorization", "cookie", "set-cookie"})
AUTHORIZATION_HEADER = re.compile(r"(-H\s+'Authorization:\s*)[^']*(')", re.IGNORECASE)


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit]


@dataclass(frozen=True)
class ProbeLimits:
    """探针输出的长度上限(runtime.sources.*)：信号消息、上下文、报告错误消息、响应与日志摘录、手动补登摘录、本项目帧数。"""

    message_chars: int
    context_bytes: int
    report_error_chars: int
    response_excerpt_chars: int
    log_excerpt_chars: int
    unlocated_excerpt_chars: int
    project_frames: int

    @classmethod
    def from_values(cls, get: Callable[[str], Any]) -> ProbeLimits:
        names = ("messageChars", "contextBytes", "reportErrorChars", "responseExcerptChars", "logExcerptChars",
                 "unlocatedExcerptChars", "projectFrames")
        return cls(*(int(get(f"runtime.sources.{name}")) for name in names))

    @classmethod
    def from_config(cls, config: ProjectConfig) -> ProbeLimits:
        return cls.from_values(config.get)

    @classmethod
    def default(cls) -> ProbeLimits:
        return cls.from_values(layers.core_value)


class ProbeRedactor:
    """探针共用的脱敏与截断；limits 缺省为核心缺省值。"""

    def __init__(self, redactor: Redactor | None = None, limits: ProbeLimits | None = None) -> None:
        self.redactor = redactor or Redactor()
        self.limits = limits or ProbeLimits.default()

    def text(self, value: str) -> str:
        return self.redactor.text(value)

    def value(self, data: Any) -> Any:
        return self.redactor.value(data)

    def headers(self, headers: Mapping[str, Any]) -> dict[str, Any]:
        return {name: REDACTED if name.lower() in HEADERS_REPLACED else self.redactor.value(value)
                for name, value in headers.items()}

    def query(self, query: Mapping[str, Any]) -> dict[str, Any]:
        return self.redactor.value(dict(query))

    def url(self, url: str) -> str:
        """URL 中查询参数的值按键名与文本规则脱敏(解码后展示)，其余部分只做文本脱敏，片段丢弃。"""
        parts = urlsplit(url)
        base = self.redactor.text(urlunsplit((parts.scheme, parts.netloc, parts.path, "", "")))
        if not parts.query:
            return base
        pairs = parse_qsl(parts.query, keep_blank_values=True)
        cleaned = [f"{name}={REDACTED if self.redactor.is_sensitive_key(name) else self.redactor.text(value)}"
                   for name, value in pairs]
        return f"{base}?{'&'.join(cleaned)}"

    def excerpt(self, text: str, limit: int | None = None) -> str:
        """脱敏后截断；limit 缺省为响应摘录的上限。"""
        return truncate(self.redactor.text(text), self.limits.response_excerpt_chars if limit is None else limit)

    def reproduce(self, command: str, tokens: Iterable[str] = ()) -> str:
        """复现命令：先替换已知 token，再做文本脱敏，最后把 Authorization 请求头写回 `Bearer <TOKEN>`
        (文本规则会把 `Authorization: Bearer` 当作键值对整体替换)。"""
        for token in sorted(set(tokens), key=len, reverse=True):
            if token:
                command = command.replace(token, TOKEN_PLACEHOLDER)
        command = self.redactor.text(command)
        return AUTHORIZATION_HEADER.sub(rf"\1Bearer {TOKEN_PLACEHOLDER}\2", command)
