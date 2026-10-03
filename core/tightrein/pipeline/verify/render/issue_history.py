"""Issue「历史」一节的验证摘要(architecture/07 13.1)：结论与理由；弱证据与未验证的条目单独列出。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tightrein.domain.enums import VerifyPhase
from tightrein.pipeline.verify.render.report import summary


def verify_line(outputs: Mapping[str, Any]) -> str:
    weak = [item["id"] for item in outputs["items"] if item["result"] == "weak"]
    unverified = [f"{item['item']}({item['reason']})" for item in outputs["unverified"]]
    text = f"{VerifyPhase(outputs['phase']).label}：{summary(outputs)}"
    if weak:
        text += f" 弱证据：{'、'.join(weak)}。"
    if unverified:
        text += f" 未验证：{'；'.join(unverified)}。"
    return text
