"""CommitFacts：调用方经 vcs 查询好的 commit 祖先关系，供解决、回归与忽略到期判定使用。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable


@dataclass(frozen=True)
class CommitFacts:
    """ancestry[(a, b)] 为 True 表示 a 是 b 的祖先；查询不到的 commit 对不出现在其中，视为未知。"""

    ancestry: dict[tuple[str, str], bool] = field(default_factory=dict)

    @classmethod
    def of(cls, facts: Iterable[tuple[str, str, bool]]) -> "CommitFacts":
        return cls({(ancestor, descendant): known for ancestor, descendant, known in facts})

    def is_ancestor(self, ancestor: str, descendant: str) -> bool | None:
        """同一个 commit 视为自身的祖先；关系未知时返回 None。"""
        if ancestor == descendant:
            return True
        return self.ancestry.get((ancestor, descendant))

    def is_newer(self, commit: str | None, than: str | None) -> bool | None:
        """commit 是否严格晚于 than：than 是 commit 的祖先且两者不同。任一方为空或关系未知时返回 None。"""
        if commit is None or than is None:
            return None
        if commit == than:
            return False
        return self.is_ancestor(than, commit)
