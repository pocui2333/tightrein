"""规范化(design 2.5)：把消息与位置中的易变部分替换为占位符。"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_LENGTH = 200


@dataclass(frozen=True)
class Rule:
    pattern: str
    replacement: str


_DEFAULT_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<guid>"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"), "<time>"),
    (re.compile(r"\b\d{2}:\d{2}:\d{2}\b"), "<time>"),
    (re.compile(r"\b(?=[0-9a-fA-F]*[a-fA-F])(?=[0-9a-fA-F]*\d)[0-9a-fA-F]{8,}\b"), "<hex>"),
    (re.compile(r"\d{4,}"), "<num>"),
    (re.compile(r"[A-Za-z]:\\[^\s'\"]+"), "<path>"),
    (re.compile(r"(?<![\w<])/(?:[\w.-]+/)+[\w.-]+"), "<path>"),
    (re.compile(r"'[^']*'|\"[^\"]*\""), "<value>"),
)

_WHITESPACE = re.compile(r"\s+")


def normalize(text: str, project_rules: tuple[Rule, ...]) -> str:
    result = text
    for pattern, replacement in _DEFAULT_RULES:
        result = pattern.sub(replacement, result)
    for rule in project_rules:
        result = re.sub(rule.pattern, rule.replacement, result)
    result = _WHITESPACE.sub(" ", result).strip()
    return result[:MAX_LENGTH]


_HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
_ID_SEGMENT = re.compile(
    r"^(\d+|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$"
)
_TRAILING_LINE = re.compile(r"(:\d+){1,2}$")


def _template_path(path: str, placeholder: str) -> str:
    path = path.split("?", 1)[0].split("#", 1)[0]
    segments = [placeholder if _ID_SEGMENT.match(segment) else segment for segment in path.split("/")]
    return "/".join(segments)


def location(value: str) -> str:
    """把原始位置统一为路由模板、页面路由模板或 文件:类名.方法名。"""
    text = value.strip()
    head, _, rest = text.partition(" ")
    if head.upper() in _HTTP_METHODS and rest.startswith("/"):
        return f"{head.upper()} {_template_path(rest.strip(), '{id}')}"
    if text.startswith("/"):
        return _template_path(text, ":id")
    return _TRAILING_LINE.sub("", text)


def symbol(value: str) -> str:
    """`文件:类名.方法名:行号` 或 `类名.方法名` 取出 `类名.方法名`。"""
    return location(value).rsplit(":", 1)[-1]
