"""commit 的先后关系(architecture/05 3.4)：第 6 步之前经注入的只读查询取得本次涉及的 commit 对的祖先关系。

查询不到的 commit(例如本地尚未同步)视为关系未知，相关问题本次不做解决或回归判定。GitReader 满足 AncestryReader。
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Protocol

from tightrein.domain.commit_facts import CommitFacts
from tightrein.vcs.errors import RefNotFound


class AncestryReader(Protocol):
    def is_ancestor(self, repo: Path, commit: str, of: str) -> bool: ...


def query(reader: AncestryReader, repo: Path, pairs: Iterable[tuple[str, str]]) -> CommitFacts:
    """pairs 为 (祖先, 后代)。"""
    facts = []
    for ancestor, descendant in sorted(set(pairs)):
        if ancestor == descendant:
            continue
        try:
            facts.append((ancestor, descendant, reader.is_ancestor(repo, ancestor, descendant)))
        except RefNotFound:
            continue
    return CommitFacts.of(facts)
