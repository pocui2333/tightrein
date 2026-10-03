"""运行摘要中的聚合片段(design 2.13)：新发现、回归、已解决各有多少，列出新发现与回归问题的标题。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tightrein.domain.enums import ProblemStatus

LISTED = (ProblemStatus.NEW, ProblemStatus.REGRESSED, ProblemStatus.RESOLVED)


def summary(run_id: str, outputs: Mapping[str, Any], titles: Mapping[str, str]) -> str:
    counts = outputs["counts"]
    lines = [f"## 聚合 {run_id}", "",
             "- " + "，".join(f"{status.label} {counts[status.value]}" for status in LISTED)]
    for item in outputs["forTriage"]:
        lines.append(f"- 待分诊 {item['problemId']}：{titles.get(item['problemId'], '')}")
    if outputs["reopenedIssues"]:
        lines.append(f"- 因回归重新打开的 Issue：{'、'.join(outputs['reopenedIssues'])}")
    lines += [f"- {note}" for note in outputs["notes"]]
    return "\n".join(lines) + "\n"
