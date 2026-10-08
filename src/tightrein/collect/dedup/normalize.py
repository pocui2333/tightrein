"""规范化：去掉消息与位置中每次都变的部分(编号、时间、地址)，让同一问题的消息与位置一致。

内置规则的顺序有讲究，项目规则(controls."collect.dedup".normalize)排在内置规则之后；截到 200 字属于指纹算法的一部分，
不做成可调项(改了会让已有指纹对不上)。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

MAX_LENGTH = 200
HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})

# 顺序：GUID → 带日期的时间 → 时刻 → 十六进制(必须同时含字母和数字，先于纯数字，否则 a1234567ff 会被拆成数字)
# → 4 位以上数字(短数字常是有意义的：第 12 页、HTTP 404) → Windows 与 POSIX 路径 → 引号里的值
_BUILT_IN: tuple[tuple[re.Pattern[str], str], ...] = (
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
_ID_SEGMENT = re.compile(r"^(\d+|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$")
_TRAILING_LINE = re.compile(r"(:\d+){1,2}$")
_LINE = re.compile(r":(\d+)(?::\d+)?$")


@dataclass(frozen=True)
class Rule:
    pattern: re.Pattern[str]
    replacement: str


class RuleInvalid(ValueError):
    pass


def rules_from(items: Iterable[Mapping[str, str]]) -> tuple[Rule, ...]:
    """项目规则：`{"pattern": 正则, "replacement": 替换}`；正则写错即报错，不跳过。"""
    result = []
    for index, item in enumerate(items):
        try:
            result.append(Rule(re.compile(item["pattern"]), item["replacement"]))
        except (KeyError, TypeError, re.error) as error:
            raise RuleInvalid(f'controls."collect.dedup".normalize[{index}]：{error}') from error
    return tuple(result)


def message(text: str, project_rules: Sequence[Rule] = ()) -> str:
    result = text
    for pattern, replacement in _BUILT_IN:
        result = pattern.sub(replacement, result)
    for rule in project_rules:
        result = rule.pattern.sub(rule.replacement, result)
    return _WHITESPACE.sub(" ", result).strip()[:MAX_LENGTH]


def location(value: str | None) -> str | None:
    """接口为「方法 路由模板」(数字与 GUID 段换成 `{id}`)，页面为路由模板(换成 `:id`)，代码位置去掉末尾的 `:行[:列]`。"""
    if value is None:
        return None
    text = value.strip()
    head, _, rest = text.partition(" ")
    if head.upper() in HTTP_METHODS and rest.startswith("/"):
        return f"{head.upper()} {_template(rest.strip(), '{id}')}"
    if text.startswith("/"):
        return _template(text, ":id")
    return _TRAILING_LINE.sub("", text)


def line_of(value: str | None) -> int | None:
    """代码位置中的行号(跨来源合并按行号相近判定)；接口与页面没有行号。"""
    if value is None or value.startswith("/") or value.partition(" ")[0].upper() in HTTP_METHODS:
        return None
    found = _LINE.search(value)
    return int(found.group(1)) if found else None


def symbol(value: str) -> str:
    """`文件:类名.方法名:行号` 或 `类名.方法名` 取出 `类名.方法名`(平台堆栈帧只用符号，不含文件与行)。"""
    return (location(value) or "").rsplit(":", 1)[-1]


def _template(path: str, placeholder: str) -> str:
    path = path.split("?", 1)[0].split("#", 1)[0]
    return "/".join(placeholder if _ID_SEGMENT.match(part) else part for part in path.split("/"))
