"""按采集方法与指纹生成验收标准(architecture/06 10.3)，纯函数：每条都是探针可以检查的条件，最后附上固定的一条
「本 Issue 的复现检查在修复后通过」。文字按 project.language(render/labels.py)；取证输出 report.acceptance 中的条件
由正文排在这些条件之前。"""

from __future__ import annotations

from tightrein.domain.enums import Probe
from tightrein.domain.problem import SOURCE_COVERED, Problem, role_of
from tightrein.domain.signal import Signal
from tightrein.pipeline.issue.render import labels

def build(problem: Problem, latest: Signal | None, language: str = "zh") -> list[str]:
    def text(key: str, **values: object) -> str:
        return labels.text(key, language, **values)

    probe, location = problem.probe, problem.scope.location
    check = latest.check if latest is not None else ""
    if probe is Probe.API_FUZZ:
        role = (role_of(latest) if latest is not None else None) or text("accept.anonymous")
        found = [text("accept.apiFuzz", location=location, check=check, role=role)]
    elif probe in SOURCE_COVERED:
        found = [text("accept.observed", fingerprint=problem.fingerprint)]
    elif probe is Probe.STATIC:
        found = [text("accept.static", check=check, location=location)]
    else:
        found = [text("accept.refuted"), text("accept.regression")]
    return [*found, text("accept.reproduce")]
