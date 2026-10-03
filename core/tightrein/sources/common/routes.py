"""把实际路径匹配为路由模板(architecture/04 1.2、3.6)。

- 接口：模板取接口描述 paths 的键，`{参数}` 匹配一个路径段内的任意字符(不含 /)；
- 页面：模板取 page-routes 的 routes[].path，`:参数` 匹配一个路径段，`:参数?` 可省略，`*` 匹配其余全部；
  路径参数后的自定义正则(如 `:id(\\d+)`)按普通参数处理。
- 匹配忽略查询参数、片段与末尾的 /，字面部分不区分大小写(常见 Web 框架路由的缺省行为)。
- 多个模板都能匹配时取字面字符最多的一个，再按模板文本排序，结果与模板的给出顺序无关；匹配不到时为空。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import urlsplit

API_PARAMETER = re.compile(r"\{[^}/]+\}")
PAGE_PARAMETER = re.compile(r"^:(?P<name>\w+)(?:\([^)]*\))?(?P<optional>\?)?$")
WILDCARD = "*"


@dataclass(frozen=True)
class _Pattern:
    template: str
    regex: re.Pattern[str]
    literal_chars: int


def clean_path(path: str) -> str:
    """去掉协议与主机、查询参数、片段与末尾的 /；空路径为 /。"""
    value = urlsplit(path).path if "://" in path else path.split("?", 1)[0].split("#", 1)[0]
    value = value.rstrip("/")
    return value or "/"


def _template_path(template: str) -> str:
    """模板本身不含查询参数，`?` 是可省略参数的标记，只去掉末尾的 /。"""
    return template.rstrip("/") or "/"


def _api_pattern(template: str) -> _Pattern:
    parts = API_PARAMETER.split(_template_path(template))
    body = "[^/]+".join(re.escape(part) for part in parts)
    return _Pattern(template, re.compile(f"^{body}$", re.IGNORECASE), sum(len(part) for part in parts))


def _page_pattern(template: str) -> _Pattern:
    body = ""
    literal = 0
    for segment in _template_path(template).split("/")[1:]:
        match = PAGE_PARAMETER.match(segment)
        if segment == WILDCARD:
            body += "(?:/.*)?"
        elif match is None:
            body += "/" + re.escape(segment)
            literal += len(segment) + 1
        elif match.group("optional"):
            body += "(?:/[^/]+)?"
        else:
            body += "/[^/]+"
    return _Pattern(template, re.compile(f"^{body or '/'}$", re.IGNORECASE), literal)


class RouteMatcher:
    def __init__(self, patterns: Iterable[_Pattern]) -> None:
        self.patterns = sorted(patterns, key=lambda item: (-item.literal_chars, item.template))

    def match(self, path: str) -> str | None:
        cleaned = clean_path(path)
        for pattern in self.patterns:
            if pattern.regex.match(cleaned):
                return pattern.template
        return None

    def template_or_path(self, path: str) -> str:
        """匹配到的模板；匹配不到时为去掉查询参数的实际路径。"""
        return self.match(path) or clean_path(path)


def api_matcher(templates: Iterable[str]) -> RouteMatcher:
    return RouteMatcher(_api_pattern(template) for template in set(templates))


def page_matcher(templates: Iterable[str]) -> RouteMatcher:
    return RouteMatcher(_page_pattern(template) for template in set(templates))
