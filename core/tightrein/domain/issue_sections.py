"""Issue 文件正文的版式：小节键、排列顺序与旧版式的标题别名(redesign/04-issue.md)。

Issue 是交接文档的 issue 类型(domain/handoff/types.py)：基础小节(结论、内容、需要决定、下一步、引用、历史)为二级标题，
「内容」下为问题、影响、复现、原因、范围、注意事项、验收标准、修复方向八个三级标题。旧版式(十一个二级小节：问题、影响、
触发条件或复现步骤、期望与实际、原因、修复方向、完整证据、验收标准、元信息、关联、历史；用户需求为需求、验收标准、关联、
历史；更早的结论、复现、证据、根因、影响面、引入)照常可读。split 把两种版式都摊平成「标题 → 内容」，修复、评审、发布、
镜像与评分表一律按键取节，不区分版式；标题为任一语言。
"""

from __future__ import annotations

from collections.abc import Mapping

from tightrein.domain import language
from tightrein.domain.handoff import sections, types

# 「内容」下的小节
PROBLEM = "problem"
IMPACT = "impact"
REPRODUCE = "reproduce"
CAUSE = "cause"
SCOPE = "scope"
NOTES = "notes"
ACCEPTANCE = "acceptance"
DIRECTION = "direction"
# 基础小节
CONCLUSION = types.CONCLUSION
CONTENT = types.CONTENT
DECISIONS = types.DECISIONS
NEXT = types.NEXT
REFERENCES = types.REFERENCES
HISTORY = types.HISTORY
# 旧版式另有的小节
EXPECTED = "expected"
EVIDENCE = "evidence"
META = "meta"
RELATED = "related"
REQUIREMENT = "requirement"

CONTENT_KEYS = types.get("issue").sections
BASE_KEYS = types.BASE_SECTIONS
LEGACY_TRIAGE_KEYS = (PROBLEM, IMPACT, REPRODUCE, EXPECTED, CAUSE, DIRECTION, EVIDENCE, ACCEPTANCE, META, RELATED,
                      HISTORY)
LEGACY_MANUAL_KEYS = (REQUIREMENT, ACCEPTANCE, RELATED, HISTORY)

# Issue 的小节键到交接文档小节键：旧版式的「完整证据」与其他类型的「证据」不是同一节
_HEADING_KEYS = {EVIDENCE: "fullEvidence"}
HEADINGS: dict[str, dict[str, str]] = {
    key: types.HEADINGS[_HEADING_KEYS.get(key, key)]
    for key in dict.fromkeys((*BASE_KEYS, *CONTENT_KEYS, *LEGACY_TRIAGE_KEYS, *LEGACY_MANUAL_KEYS))}
LEGACY = {"复现": REPRODUCE, "证据": EVIDENCE, "根因": CAUSE, "影响面": IMPACT, "引入": CAUSE,
          "触发条件或复现步骤": REPRODUCE, "Trigger or steps to reproduce": REPRODUCE, "発生条件・再現手順": REPRODUCE}
ALIASES: dict[str, str] = {**{title: key for key, titles in HEADINGS.items() for title in titles.values()}, **LEGACY}
OLDEST_PROBLEM = "结论"  # 最早的版式以「结论」作问题一节；新版式的「结论」是基础小节
_CONTENT_TITLES = frozenset(HEADINGS[CONTENT].values())


def heading(key: str, code: str) -> str:
    return language.pick(HEADINGS[key], code)


def key_of(title: str) -> str | None:
    return sections.key_of(title, ALIASES)


def is_handoff(body: str) -> bool:
    """正文是交接文档版式(有「内容」这一基础小节)。"""
    return any(title.strip() in _CONTENT_TITLES for title in sections.split(body))


def split(body: str) -> dict[str, str]:
    """正文中各节的「标题 → 内容」，按出现顺序：交接文档版式时「内容」换成它下面的各三级小节；旧版式为各二级小节，
    最早版式的「结论」改用「问题」的标题。"""
    top = sections.split(body)
    if not is_handoff(body):
        problem = heading(PROBLEM, "zh")
        return {(problem if title.strip() == OLDEST_PROBLEM else title): text for title, text in top.items()}
    found: dict[str, str] = {}
    for title, text in top.items():
        if title.strip() in _CONTENT_TITLES:
            found.update(sections.split(text, 3))
        else:
            found[title] = text
    return found


def find(found: Mapping[str, str], key: str) -> str | None:
    """按键取一节的内容(标题为任一语言或旧标题)；没有时为 None。同一键有多节(旧文件的根因与引入)时依次拼接。"""
    return sections.find(found, key, ALIASES)


def title_in(body: str, key: str) -> str | None:
    """正文中这一节实际使用的标题(二级或「内容」下的三级)。"""
    return next((title for title in split(body) if key_of(title) == key), None)


def keys_in(body: str) -> set[str]:
    return {key for key in (key_of(title) for title in split(body)) if key is not None}
