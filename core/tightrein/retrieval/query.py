"""查询串解析与 FTS5 MATCH 表达式(architecture/03 1.5、1.6.2)。

1. 按空白拆分查询串，双引号括起的部分作为一个检索词整体保留；
2. 每个检索词经 split_cjk 处理，没有任何词字符的丢弃，全部丢弃时抛出 InvalidQuery；
3. 每个检索词中的双引号加倍后包进双引号，成为一个 FTS5 短语：`*`、`^`、`:`、括号、AND 等都在引号内，不被解释为运算符；
4. 短语以 OR 连接，部分命中的条目也能出现，命中的短语越多、越集中在高权重列，bm25 排序越靠前。
列权重只定义在这里，修改后必须运行检索评测(1.9)。
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from tightrein.retrieval.errors import InvalidQuery
from tightrein.retrieval.text import has_token, split_cjk

COLUMNS = ("id", "title", "summary", "tags", "body")


def rank_expression(weights: Mapping[str, float]) -> str:
    """bm25 的排序表达式；weights 为各列权重(runtime.retrieval.columnWeights)，按全文索引的列顺序排列。"""
    return "bm25(knowledge_fts, {})".format(", ".join(str(float(weights[column])) for column in COLUMNS))
TERM = re.compile(r'"([^"]*)"|(\S+)')


def terms(query: str) -> list[str]:
    """拆出检索词：引号内整体保留，未闭合的引号按普通字符处理。"""
    found = []
    for match in TERM.finditer(query):
        quoted, plain = match.groups()
        found.append(quoted if quoted is not None else plain)
    return found


def phrase(term: str) -> str | None:
    """一个检索词对应的 FTS5 短语；没有词字符时为空。"""
    text = split_cjk(term)
    if not has_token(text):
        return None
    return '"' + text.replace('"', '""') + '"'


def match_expression(query: str) -> str:
    phrases = [item for item in (phrase(term) for term in terms(query)) if item is not None]
    if not phrases:
        raise InvalidQuery(f"查询串中没有可检索的文字：{query!r}")
    return " OR ".join(phrases)
