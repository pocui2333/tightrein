"""查重(assess.dedup)的变量：本问题的主张与候选列表(编号、种类、标题、根因位置、状态)。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from tightrein.assess.claims import Claim
from tightrein.assess.prompts.common import feedback_text

POINT = "assess.dedup"
ISSUE = "issue"
PROBLEM = "problem"


@dataclass(frozen=True)
class Candidate:
    id: str
    kind: str  # issue、problem
    title: str
    location: str | None
    root_causes: tuple[str, ...]
    status: str

    def render(self) -> str:
        causes = "、".join(self.root_causes) or "(没有根因位置)"
        where = f"，位置 {self.location}" if self.location else ""
        return f"- {self.id}({self.kind}，{self.status}{where})：{self.title}；根因 {causes}"


def variables(claim: Claim, candidates: Sequence[Candidate], feedback: list[str]) -> dict[str, str]:
    return {"claim": claim.render(), "candidates": "\n".join(item.render() for item in candidates),
            "feedback": feedback_text(feedback)}
