"""为 agent 任务预取知识(architecture/03 1.6.6，design 16.6)。

| 任务 | 按位置匹配的条目 | 按位置匹配的文档 | 直接放全文的类别 |
|---|---|---|---|
| static-review | defect-pattern：path: 是任一改动文件的前缀 | — | — |
| triage | defect-pattern、tradeoff、triage-lesson：route:、page:、path: 任一匹配 | finding、issue | tradeoff |
| fix | fix-lesson、contract：path: 是任一根因文件的前缀 | fix-report | — |

1. 按位置匹配：path: 前缀匹配，route:、page: 精确匹配；匹配到的标签越长越靠前，同等长度按 updated 从新到旧；
2. 补足：少于上限时以 keywords 检索同样的类型补足，命中依据为「关键词检索」；keywords 中没有可检索的文字时不补足；
3. 去除 exclude_ids 与非 active 的条目；
4. 直接放全文：该类别全部 active 条目的正文估算 token 数不超过 inlineFullTokens 时放进 inline_documents，
   不再出现在 items 中；超过时按普通条目处理；
5. routes 为空而给出了问题编号时，从该问题的信号中补取路由，页面与文件为空时一并补取。
items 只含编号与摘要，agent 需要正文时用 kb get 读取；预取不计命中。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field

from tightrein.domain.enums import ContextKind, KnowledgeStatus, KnowledgeType
from tightrein.retrieval import search
from tightrein.retrieval.errors import InvalidQuery
from tightrein.retrieval.frontmatter import PAGE_TAG, PATH_TAG, ROUTE_TAG
from tightrein.retrieval.locations import problem_locations
from tightrein.retrieval.models import MAX_LIMIT, EntryDocument, SearchFilters
from tightrein.retrieval.ranking import CandidateSource
from tightrein.retrieval.sources import FINDING, FIX_REPORT, ISSUE
from tightrein.retrieval.text import estimate_tokens
from tightrein.runner.task import ContextItem
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.repos import knowledge
from tightrein.store.repos.knowledge import KnowledgeRecord

KEYWORD_REASON = "关键词检索"
HEADER = "以下是与本任务相关的知识条目摘要，需要正文时用 `kb get <编号>` 读取："
INLINE_HEADER = "以下类别的条目总量较小，直接附上全文："
EMPTY = "没有与本任务相关的知识条目。"


@dataclass(frozen=True)
class Rule:
    types: tuple[str, ...]
    prefixes: tuple[str, ...]
    inline: tuple[str, ...] = ()


RULES: dict[ContextKind, Rule] = {
    ContextKind.STATIC_REVIEW: Rule((KnowledgeType.DEFECT_PATTERN.value,), (PATH_TAG,)),
    ContextKind.TRIAGE: Rule(
        (KnowledgeType.DEFECT_PATTERN.value, KnowledgeType.TRADEOFF.value, KnowledgeType.TRIAGE_LESSON.value,
         FINDING, ISSUE),
        (ROUTE_TAG, PAGE_TAG, PATH_TAG), (KnowledgeType.TRADEOFF.value,)),
    ContextKind.FIX: Rule((KnowledgeType.FIX_LESSON.value, KnowledgeType.CONTRACT.value, FIX_REPORT), (PATH_TAG,)),
}


@dataclass(frozen=True)
class ContextRequest:
    kind: ContextKind
    paths: tuple[str, ...] = ()
    routes: tuple[str, ...] = ()
    pages: tuple[str, ...] = ()
    keywords: str = ""
    exclude_ids: tuple[str, ...] = ()
    problem_id: str | None = None


@dataclass(frozen=True)
class ContextBundle:
    items: list[ContextItem]
    inline_documents: list[EntryDocument] = field(default_factory=list)
    estimated_tokens: int = 0

    def render(self) -> str:
        """放进执行器任务说明的一段文本。"""
        if not self.items and not self.inline_documents:
            return EMPTY + "\n"
        parts = []
        if self.items:
            parts.append("\n".join([HEADER, *(f"- {item.id} [{item.type}] {item.summary}({item.reason})"
                                               for item in self.items)]))
        if self.inline_documents:
            parts.append(INLINE_HEADER)
            parts += [f"## {document.id}({document.path})\n\n{document.body.strip()}"
                      for document in self.inline_documents]
        return "\n\n".join(parts) + "\n"


def _match(record: KnowledgeRecord, request: ContextRequest, prefixes: Sequence[str]) -> tuple[int, str] | None:
    """标签与任务位置的最佳匹配：(匹配到的标签长度, 命中依据)。"""
    best: tuple[int, str] | None = None
    for tag in record.tags:
        found = None
        if PATH_TAG in prefixes and tag.startswith(PATH_TAG):
            value = tag[len(PATH_TAG):]
            if any(path.startswith(value) for path in request.paths):
                found = (len(value), f"{tag} 前缀匹配")
        elif ROUTE_TAG in prefixes and tag.startswith(ROUTE_TAG) and tag[len(ROUTE_TAG):] in request.routes:
            found = (len(tag) - len(ROUTE_TAG), f"{tag} 匹配")
        elif PAGE_TAG in prefixes and tag.startswith(PAGE_TAG) and tag[len(PAGE_TAG):] in request.pages:
            found = (len(tag) - len(PAGE_TAG), f"{tag} 匹配")
        if found is not None and (best is None or found[0] > best[0]):
            best = found
    return best


def _item(record: KnowledgeRecord, reason: str) -> ContextItem:
    return ContextItem(record.id, record.type, record.summary, record.path, reason)


def with_problem_locations(conn: sqlite3.Connection, request: ContextRequest) -> ContextRequest:
    if request.routes or request.problem_id is None:
        return request
    found = problem_locations(conn, [request.problem_id])
    return ContextRequest(request.kind, request.paths or found.paths, found.routes, request.pages or found.pages,
                          request.keywords, request.exclude_ids, request.problem_id)


def build(conn: sqlite3.Connection, layout: WorkspaceLayout, sources: Sequence[CandidateSource],
          request: ContextRequest, limit: int, inline_full_tokens: int, *, candidate_factor: int | None = None,
          rrf_k: int | None = None) -> ContextBundle:
    """candidate_factor、rrf_k 传给关键词检索(search.search)。"""
    request = with_problem_locations(conn, request)
    rule = RULES[request.kind]
    excluded = set(request.exclude_ids)
    active = [record for record in knowledge.find(conn, types=rule.types, status=KnowledgeStatus.ACTIVE)
              if record.id not in excluded]
    inline: list[EntryDocument] = []
    inlined_types: set[str] = set()
    for kind in rule.inline:
        documents = [search.read_entry(layout, record) for record in active if record.type == kind]
        if sum(estimate_tokens(document.body) for document in documents) <= inline_full_tokens:
            inline += documents
            inlined_types.add(kind)
    candidates = [record for record in active if record.type not in inlined_types]
    matched = []
    for record in candidates:
        found = _match(record, request, rule.prefixes)
        if found is not None:
            matched.append((found, record))
    matched.sort(key=lambda pair: (-pair[0][0], -pair[1].updated.toordinal(), pair[1].id))
    items = [_item(record, reason) for (_, reason), record in matched][:limit]
    if len(items) < limit and request.keywords.strip():
        chosen = {item.id for item in items} | excluded
        types = tuple(kind for kind in rule.types if kind not in inlined_types)
        try:
            hits = search.search(conn, sources, request.keywords,
                                 SearchFilters(types, (), KnowledgeStatus.ACTIVE, MAX_LIMIT),
                                 candidate_factor=candidate_factor, rrf_k=rrf_k)
        except InvalidQuery:
            hits = []
        for hit in hits:
            if len(items) >= limit:
                break
            if hit.id not in chosen:
                items.append(ContextItem(hit.id, hit.type, hit.summary, hit.path, KEYWORD_REASON))
                chosen.add(hit.id)
    bundle = ContextBundle(items, inline)
    return ContextBundle(items, inline, estimate_tokens(bundle.render()))
