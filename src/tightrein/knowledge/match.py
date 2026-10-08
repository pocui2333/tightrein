"""按本次要动的位置匹配知识条目，按条数与 token 上限截取(knowledge/README.md「读取」)。

- 位置有三种写法，先分类再匹配：「方法 路由」为接口，以 `/` 开头为页面，其余取 `文件:符号`、`文件:行号` 中的文件；
- `path:` 前缀匹配(条目写目录前缀时覆盖其下全部文件)，`route:`、`page:` 精确匹配；
- 匹配到的标签越长越靠前，同样长按更新日期从新到旧；没有位置的条目对整个项目都适用，排在最后；
- 只取有效(active)条目；待确认与已取代的不取；
- 不让模型自己搜索：调用方把 render 的结果拼进提示。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from tightrein.knowledge.entries import PAGE_TAG, PAGE_VALUE, PATH_TAG, ROUTE_TAG, ROUTE_VALUE, Entry, active, load
from tightrein.store.files.layout import WorkspaceLayout

CJK_CHAR = re.compile(
    "[ᄀ-ᇿ぀-ヿ㄰-㆏ㇰ-ㇿ㐀-䶿一-鿿가-힯"
    "豈-﫿ｦ-ﾟ\U00020000-\U0003134f]"
)
LATIN_CHARS_PER_TOKEN = 4
LOCATION_SUFFIX = re.compile(r":[^/]*$")
EMPTY = "没有与本次相关的知识条目。"
FULL_HEADER = "以下是与本次相关的知识条目："
BRIEF_HEADER = "以下是与本次相关的知识条目(条目较多，只列摘要)："


@dataclass(frozen=True)
class Locations:
    paths: tuple[str, ...] = ()
    routes: tuple[str, ...] = ()
    pages: tuple[str, ...] = ()


def classify(values: Sequence[str]) -> Locations:
    """把调用方给的位置(文件、`文件:行号`、`文件:符号`、「方法 路由」、页面)分成三类。"""
    paths, routes, pages = set(), set(), set()
    for value in (item.strip() for item in values if item.strip()):
        if ROUTE_VALUE.match(value):
            routes.add(value)
        elif PAGE_VALUE.match(value):
            pages.add(value)
        else:
            paths.add(LOCATION_SUFFIX.sub("", value).removeprefix("./"))
    return Locations(tuple(sorted(paths)), tuple(sorted(routes)), tuple(sorted(pages)))


def estimate_tokens(text: str) -> int:
    """中日韩字符每字算 1，其余非空白字符每 4 个算 1，向上取整；只用于上限判断。"""
    cjk = len(CJK_CHAR.findall(text))
    others = sum(1 for char in text if not char.isspace()) - cjk
    return cjk + -(-others // LATIN_CHARS_PER_TOKEN)


def match(layout: WorkspaceLayout, paths: list[str], *, limit_entries: int, limit_tokens: int,
          warnings: list[str] | None = None) -> list[Entry]:
    """命中的有效条目，最多 limit_entries 条，且按摘要列出时不超过 limit_tokens。

    paths 可混写文件、`文件:行号`、「方法 路由」与页面；格式坏了的条目文件的警告追加到 warnings。
    """
    loaded = load(layout)
    if warnings is not None:
        warnings.extend(loaded.warnings)
    return select(active(loaded.entries), classify(paths), limit_entries=limit_entries, limit_tokens=limit_tokens)


def select(entries: Sequence[Entry], locations: Locations, *, limit_entries: int, limit_tokens: int) -> list[Entry]:
    scored = [(score, entry) for entry in entries if (score := _score(entry, locations)) is not None]
    scored.sort(key=lambda pair: (-pair[0], _newest_first(pair[1].updated), pair[1].id))
    chosen: list[Entry] = []
    used = estimate_tokens(BRIEF_HEADER)
    for _, entry in scored[:limit_entries]:
        cost = estimate_tokens(_brief(entry))
        if used + cost > limit_tokens:
            break
        chosen.append(entry)
        used += cost
    return chosen


def render(entries: Sequence[Entry], *, limit_tokens: int) -> str:
    """拼进提示的文本：全部正文在 token 上限以内时放全文，超过就只列编号与摘要。"""
    if not entries:
        return EMPTY
    full = "\n\n".join([FULL_HEADER, *(_full(entry) for entry in entries)])
    if estimate_tokens(full) <= limit_tokens:
        return full
    return "\n".join([BRIEF_HEADER, *(_brief(entry) for entry in entries)])


# 内部


def _score(entry: Entry, locations: Locations) -> int | None:
    """匹配到的最长标签的长度；没有位置的条目为 0；有位置却一个都没匹配到为 None。"""
    if not entry.locations:
        return 0
    best: int | None = None
    for location in entry.locations:
        length: int | None = None
        if location.startswith(PATH_TAG):
            value = location[len(PATH_TAG):]
            if any(path == value or path.startswith(value if value.endswith("/") else value + "/")
                   for path in locations.paths):
                length = len(value)
        elif location.startswith(ROUTE_TAG) and location[len(ROUTE_TAG):] in locations.routes:
            length = len(location) - len(ROUTE_TAG)
        elif location.startswith(PAGE_TAG) and location[len(PAGE_TAG):] in locations.pages:
            length = len(location) - len(PAGE_TAG)
        if length is not None and (best is None or length > best):
            best = length
    return best


def _newest_first(updated: str) -> tuple[int, ...]:
    return tuple(-int(part) for part in updated.split("-")) if updated else (0,)


def _full(entry: Entry) -> str:
    return f"## {entry.id} {entry.title}({entry.kind})\n\n{entry.body.strip()}"


def _brief(entry: Entry) -> str:
    return f"- {entry.id} [{entry.kind}] {entry.title}：{entry.summary}"
