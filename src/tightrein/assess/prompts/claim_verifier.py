"""取证(assess.triage)与证伪复核(assess.refute)的变量。

复核是盲审：拿到与取证完全相同的主张、事实、代码笔记与知识，看不到第一次的判定与输出；它的模型按调用点
assess.refute 的路由，加载配置时检查与 assess.triage 不同(settings 的 independence)。
"""

from __future__ import annotations

from dataclasses import dataclass

from tightrein.assess.claims import Claim
from tightrein.assess.prompts.common import feedback_text, text_or_none

TRIAGE = "assess.triage"
REFUTE = "assess.refute"


@dataclass(frozen=True)
class Inputs:
    """同一个问题交给取证与复核的输入；复核与取证共用同一份(盲审)。"""

    case: str
    claim: Claim
    notes: str
    knowledge: str
    severity_guide: str | None
    title_limit: int


def variables(point: str, inputs: Inputs, feedback: list[str]) -> dict[str, str]:
    found = {
        "claim": inputs.claim.render(),
        "notes": inputs.notes,
        "knowledge": text_or_none(inputs.knowledge),
        "severity_guide": text_or_none(inputs.severity_guide),
        "title_limit": str(inputs.title_limit),
        "feedback": feedback_text(feedback),
    }
    if point == TRIAGE:
        found["case"] = inputs.case
    return found
