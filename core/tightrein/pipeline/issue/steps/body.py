"""由分诊交接文档生成 Issue 正文(redesign/04-issue.md 的 WRAP 小节)，纯函数；返回结论、「内容」各小节与引用，
由 render/issue.py 按交接文档版式渲染。

- 结论：report.title 加后果；
- 问题：取证输出 report.summary(没有时为主张原文)，附期望与实际；
- 影响：严重度及理由、后果、受影响的角色与数据、调用点；
- 复现：report.steps 编号列表(没有时以触发条件作一条)，另附信号中的复现命令、失败步骤、截图与 trace；
- 原因：根因位置、触发条件、调用链入口、引入的 commit；
- 范围：可能涉及的文件(assessment.estimate，没有时取根因文件)与明确不做的部分(scope.outOfScope)；
- 注意事项：「需要先与代码作者讨论」、不能改的文件与行为(scope.mustKeep)、需用户定夺的标记；
- 验收标准：固定三条(复现测试修复前失败、修复后通过，现有测试全部通过，必须保持不变的行为)在前，report.acceptance
  与探针可检查的条件在后，复选框；
- 修复方向：取证评估的方向(建议，不强制)；
- 引用：发现报告、完整证据(代码事实)、关联问题。
缺少的字段显示「—」，不丢节。代码位置一律写成反引号中的 `路径:行号`，GitHub 镜像据此换成永久链接。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from tightrein.domain.enums import IssueLabel
from tightrein.domain.handoff.document import Reference
from tightrein.domain.issue_sections import ACCEPTANCE, CAUSE, DIRECTION, IMPACT, NOTES, PROBLEM, REPRODUCE, SCOPE
from tightrein.domain.problem import Problem
from tightrein.pipeline.issue.render import labels
from tightrein.pipeline.issue.render.labels import MISSING

REPRODUCE_KEYS = ("reproduce", "step", "screenshot", "trace")
FLAG_KEYS = ("design", "dataStructure", "publicContract")
ELLIPSIS = "…"
REPORT_FIELDS = ("summary", "steps", "expected", "actual", "acceptance")


def _lines(items: Sequence[str]) -> str:
    return "\n".join(f"- {item}" for item in items) if items else MISSING


def _numbered(items: Sequence[str]) -> str:
    return "\n".join(f"{number}. {item}" for number, item in enumerate(items, start=1))


def report(outputs: Mapping[str, Any]) -> Mapping[str, Any]:
    return outputs.get("report") or {}


def title(outputs: Mapping[str, Any], max_length: int) -> str:
    """Issue 标题：report.title，没有时为主张的标题；超过上限时截断。"""
    found = (report(outputs).get("title") or outputs["claim"]["title"]).strip()
    return found if len(found) <= max_length else found[:max_length - 1].rstrip() + ELLIPSIS


def missing_fields(outputs: Mapping[str, Any]) -> list[str]:
    """新模板需要而分诊交接文档中没有的字段(rerender 时列出，重新分诊才能补上)。"""
    found = report(outputs)
    missing = ["title", *REPORT_FIELDS, "severityReason"] if not found else [
        key for key in REPORT_FIELDS if not found.get(key)]
    return missing + ([] if outputs.get("scope") else ["scope"])


def conclusion(outputs: Mapping[str, Any], title_text: str, language: str) -> str:
    """一句话说清问题与后果。"""
    consequence = ((outputs.get("evidence") or {}).get("impact") or {}).get("consequence")
    return title_text + (f"；{labels.text('consequenceShort', language, text=consequence)}" if consequence else "")


def _problem(outputs: Mapping[str, Any], language: str) -> str:
    found = report(outputs)
    text = found.get("summary") or outputs["claim"]["statement"]
    if found.get("expected") or found.get("actual"):
        text += "\n\n" + _lines([f"{labels.text('expected', language)}：{found.get('expected') or MISSING}",
                                  f"{labels.text('actual', language)}：{found.get('actual') or MISSING}"])
    return text


def _impact(outputs: Mapping[str, Any], language: str) -> str:
    severity = outputs.get("severity") or MISSING
    reason = report(outputs).get("severityReason")
    items = [f"{labels.text('severity', language)}：{severity}" + (f"({reason})" if reason else "")]
    impact = (outputs.get("evidence") or {}).get("impact")
    if impact:
        calls = "、".join(f"`{site}`" for site in impact.get("callSites") or [])
        items += [f"{labels.text('consequence', language)}：{impact.get('consequence') or MISSING}",
                  f"{labels.text('roles', language)}：{'、'.join(impact.get('roles') or []) or MISSING}",
                  f"{labels.text('data', language)}：{impact.get('data') or MISSING}",
                  f"{labels.text('scope', language)}：{calls or MISSING}"]
    return _lines(items)


def _reproduce(outputs: Mapping[str, Any], language: str) -> str:
    steps = list(report(outputs).get("steps") or [])
    trigger = (outputs.get("evidence") or {}).get("trigger")
    if not steps and trigger:
        steps = [trigger]
    extra: list[str] = []
    for fact in outputs["claim"]["facts"]:
        value = fact["value"]
        context = value.get("context") if isinstance(value, dict) else None
        if not isinstance(context, dict):
            continue
        for key in REPRODUCE_KEYS:
            if context.get(key):
                shown = f"`{context[key]}`" if key == "reproduce" else str(context[key])
                extra.append(f"{labels.text(key, language)}：{shown}")
    parts = [_numbered(steps)] if steps else []
    if extra:
        parts.append(_lines(list(dict.fromkeys(extra))))
    return "\n\n".join(parts) or MISSING


def _cause(outputs: Mapping[str, Any], language: str) -> str:
    evidence = outputs.get("evidence") or {}
    causes = [f"`{cause['file']}:{cause['line']}` {cause.get('symbol') or ''}".rstrip()
              for cause in outputs.get("rootCauses") or []]
    items = [f"{labels.text('rootCause', language)}：{'、'.join(causes) or MISSING}"]
    if evidence.get("trigger"):
        items.append(f"{labels.text('trigger', language)}：{evidence['trigger']}")
    entries = list(dict.fromkeys(item["entry"] for item in evidence.get("counterEvidence") or []))
    if entries:
        items.append(f"{labels.text('entries', language)}：" + "、".join(f"`{entry}`" for entry in entries))
    introduced = [labels.text("introducedItem", language, commit=item["commit"], author=item.get("author") or MISSING,
                              pr=item.get("pr") or MISSING) for item in outputs.get("introducedBy") or []]
    items.append(f"{labels.text('introduced', language)}：{'、'.join(introduced) or MISSING}")
    return _lines(items)


def _scope_of(outputs: Mapping[str, Any]) -> Mapping[str, Any]:
    return outputs.get("scope") or {}


def _scope(outputs: Mapping[str, Any], language: str) -> str:
    files = [item["path"] for item in (outputs.get("estimate") or {}).get("files") or []] or list(
        dict.fromkeys(cause["file"] for cause in outputs.get("rootCauses") or []))
    excluded = list(_scope_of(outputs).get("outOfScope") or [])
    return _lines([f"{labels.text('scopeFiles', language)}：" + ("、".join(f"`{file}`" for file in files) or MISSING),
                   f"{labels.text('outOfScope', language)}：" + ("；".join(excluded) or MISSING)])


def _notes(outputs: Mapping[str, Any], language: str) -> str:
    items = []
    if IssueLabel.DISCUSS_WITH_AUTHOR.value in outputs.get("labels", []):
        items.append(f"**{labels.text('discuss', language)}**")
    items += [f"{labels.text('mustKeep', language)}：{item}" for item in _scope_of(outputs).get("mustKeep") or []]
    items += [f"{labels.text('decide', language)}：{labels.text('flag.' + key, language)}：{value['reason']}("
              + "、".join(f"`{location}`" for location in value["locations"]) + ")"
              for key, value in (outputs.get("flags") or {}).items() if key in FLAG_KEYS and value.get("flagged")]
    return _lines(items)


def _direction(outputs: Mapping[str, Any]) -> str:
    return (outputs.get("worth") or {}).get("direction") or MISSING


def fixed_acceptance(must_keep: Sequence[str], repro_test: bool, language: str) -> list[str]:
    """每个 Issue 都有的验收标准：复现测试修复前失败、修复后通过(不写复现测试的类型写明)，现有测试全部通过，
    必须保持不变的行为(没有列出时写一条通用的)。"""
    keep = [labels.text("accept.keep", language, item=item) for item in must_keep] or [
        labels.text("accept.keepDefault", language)]
    first = labels.text("accept.reproTest" if repro_test else "accept.reproTestSkipped", language)
    return [first, labels.text("accept.existingTests", language), *keep]


def checkboxes(items: Sequence[str]) -> str:
    return "\n".join(f"- [ ] {item}" for item in dict.fromkeys(items))


def _acceptance(outputs: Mapping[str, Any], acceptance: Sequence[str], repro_test: bool, language: str) -> str:
    fixed = fixed_acceptance(list(_scope_of(outputs).get("mustKeep") or []), repro_test, language)
    return checkboxes([*fixed, *(report(outputs).get("acceptance") or []), *acceptance])


def references(outputs: Mapping[str, Any], findings: str | None, problem: Problem, language: str) -> list[Reference]:
    """引用：发现报告、完整证据(代码事实)、问题。"""
    found = [Reference(findings, labels.text("findings", language))] if findings else []
    evidence = outputs.get("evidence") or {}
    found += [Reference(fact["location"], fact["observation"]) for fact in evidence.get("facts") or []]
    found.append(Reference(problem.id, problem.title))
    return found


def sections(outputs: Mapping[str, Any], acceptance: Sequence[str], language: str,
             repro_test: bool = True) -> dict[str, str]:
    """「内容」下的八个小节。"""
    return {
        PROBLEM: _problem(outputs, language),
        IMPACT: _impact(outputs, language),
        REPRODUCE: _reproduce(outputs, language),
        CAUSE: _cause(outputs, language),
        SCOPE: _scope(outputs, language),
        NOTES: _notes(outputs, language),
        ACCEPTANCE: _acceptance(outputs, acceptance, repro_test, language),
        DIRECTION: _direction(outputs),
    }
