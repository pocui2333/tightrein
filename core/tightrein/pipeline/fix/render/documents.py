"""修复各步的交接文档(redesign/00-handoff-documents.md、05-fix.md)：进度(progress)、任务(task)、勘察结果与收集结果
(result)、计划(plan)、评审(review)、待决定(decision)，都写在 data/fixes/<编号>/ 下，由程序按类型模板渲染。

模型的输出先写自由分析(analysis)再写结构化字段，这里把分析与结构化字段整理进各类型的固定小节；编号为
`<Issue 编号>-<种类>[-<序号>]`，头信息的 from 为 `fix/<角色或 program>`。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any

from tightrein.domain.enums import DocumentStatus
from tightrein.domain.handoff.document import Decision, Event, HandoffDocument, Header, NextStep, Reference
from tightrein.store.files import documents

SOURCE = "fix"
NONE = "无"
PROGRESS = "progress.md"
TASK = "task.md"
SCOUT = "scout.md"
PLAN = "plan.md"
RESULT = "result.md"
DECISION = "decision.md"


def review_name(round_number: int, mode: str) -> str:
    return f"review-{round_number}-{mode}.md"


def _lines(items: Sequence[str]) -> str:
    return "\n".join(f"- {item}" for item in items) if items else NONE


@dataclass(frozen=True)
class Writer:
    """写一份修复文档所需的公共信息。"""

    directory: Path
    issue_id: str
    language: str
    zone: tzinfo | None = None

    def header(self, kind: str, name: str, status: DocumentStatus, source: str, target: str, now: datetime,
               parent: str | None = None, next_step: str | None = None) -> Header:
        return Header(kind, f"{self.issue_id}-{name}", status, f"{SOURCE}/{source}", target, self.issue_id, now, now,
                      parent, next_step)

    def write(self, file_name: str, document: HandoffDocument) -> Path:
        path = self.directory / file_name
        documents.write(path, document, self.language, self.zone)
        return path

    def history(self, file_name: str, at: datetime, text: str, status: DocumentStatus | None = None) -> None:
        path = self.directory / file_name
        if path.is_file():
            documents.append_history(path, at, text, status, self.zone)


def progress(writer: Writer, now: datetime, *, status: DocumentStatus, conclusion: str,
             checklist: Sequence[Mapping[str, str]], completed: Sequence[str], blockers: Sequence[str],
             next_steps: Sequence[NextStep], history: Sequence[Event]) -> HandoffDocument:
    return HandoffDocument(
        writer.header("progress", "progress", status, "program", "user", now), conclusion,
        {"checklist": _lines([f"{item['item']}：{item['state']}" for item in checklist]),
         "completed": _lines(completed), "blockers": _lines(blockers)},
        {"checklist": [dict(item) for item in checklist]}, next_steps=tuple(next_steps), history=tuple(history))


def task(writer: Writer, now: datetime, *, goal: str, inputs: Sequence[str], constraints: Sequence[str],
         acceptance: Sequence[str], deliverables: str, references: Sequence[Reference] = ()) -> HandoffDocument:
    blocks = {"acceptance": [{"id": f"A{number}", "text": text} for number, text in enumerate(acceptance, start=1)]} \
        if acceptance else {}
    return HandoffDocument(
        writer.header("task", "task", DocumentStatus.IN_PROGRESS, "program", "fix/fix-executor", now),
        goal.split("\n", 1)[0], {"goal": goal, "inputs": _lines(inputs), "constraints": _lines(constraints),
                                 "acceptance": _lines(acceptance), "deliverables": deliverables},
        blocks, references=tuple(references), history=(Event(now, "程序生成写复现测试与写代码的任务"),))


def scout(writer: Writer, now: datetime, output: Mapping[str, Any]) -> HandoffDocument:
    groups = {"existing": "已有实现", "reusable": "可复用", "dataStructure": "数据结构", "linkage": "联动方",
              "problems": "问题"}
    found = [f"{label}：`{item['location']}` {item['description']}"
             for key, label in groups.items() for item in output.get(key) or []]
    design = output.get("designIssue")
    deviations = NONE if not design else f"根因在设计本身：{design['rootCause']}；{design['reason']}"
    return HandoffDocument(
        writer.header("result", "scout", DocumentStatus.DONE, "fix-scout", "fix/fix-planner", now),
        f"勘察找到 {len(found)} 处相关位置。",
        {"done": output["analysis"], "outputs": _lines(found),
         "evidence": _lines([*(f"受影响接口：{item}" for item in output.get("affectedEndpoints") or []),
                             *(f"受影响页面：{item}" for item in output.get("affectedPages") or [])]),
         "deviations": deviations}, history=(Event(now, "fix-scout 完成勘察"),))


def plan(writer: Writer, now: datetime, planned: Mapping[str, Any], *, status: DocumentStatus,
         attention: Sequence[str], decisions: Sequence[Decision] = ()) -> HandoffDocument:
    estimate = planned["estimate"]
    lane = planned.get("lane")
    head = f"{planned['summary']}(预估 {estimate['files']} 个文件、{estimate['lines']} 行"
    head += f"，通道 {lane}" if lane else ""
    later = (planned.get("split") or {}).get("followUps") or []
    steps = [f"{number}. `{step['file']}`：{step['change']}(验证：{step['verification']})"
             for number, step in enumerate(planned["steps"], start=1)]
    if later:
        steps += ["", f"拆分为 {len(later) + 1} 个子任务，本计划只做第 1 个：",
                  *(f"- 第 {number} 个「{item['title']}」：{item['goal']}(预估 {item['estimate']['files']} 个文件、"
                    f"{item['estimate']['lines']} 行)" for number, item in enumerate(later, start=2))]
    files = [{"path": item["path"], "change": "add" if item["isNew"] else "modify",
              **({"reason": item["reason"]} if item.get("reason") else {})} for item in planned["files"]]
    files += [{"path": item["path"], "change": "delete", "reason": item["reason"]} for item in planned["deletions"]]
    design = planned.get("frontendDesign")
    frontend = "前端设计说明：\n" + _lines([*design["layout"], *design["mobile"]]) if design else ""
    approach = "\n\n".join(part for part in (planned.get("analysis") or "", planned["summary"],
                                            "需要特别关注：\n" + _lines(attention) if attention else "", frontend)
                           if part)
    return HandoffDocument(
        writer.header("plan", "plan", status, "fix-planner" if planned.get("analysis") else "program",
                      "fix/fix-executor", now),
        head + ")。",
        {"approach": approach, "steps": "\n".join(steps),
         "acceptanceMapping": _lines([f"{item['criterion']} → 步骤 {'、'.join(str(step) for step in item['steps'])}"
                                      for item in planned["acceptanceMapping"]]),
         "outOfScope": _lines(planned["notDoing"])},
        {"files": files}, decisions=tuple(decisions), history=(Event(now, "计划已写入"),))


def result(writer: Writer, now: datetime, *, passed: bool, done: str, outputs: Sequence[str],
           checks: Sequence[Mapping[str, str]], deviations: Sequence[str],
           references: Sequence[Reference] = ()) -> HandoffDocument:
    conclusion = "复现测试与全部检查通过。" if passed else "有检查没有通过，交回修改。"
    return HandoffDocument(
        writer.header("result", "result", DocumentStatus.DONE if passed else DocumentStatus.FAILED, "program",
                      "fix/fix-reviewer", now),
        conclusion,
        {"done": done, "outputs": _lines(outputs),
         "evidence": _lines([f"{item['name']}：{item['verdict']}" + (f"，{item['summary']}" if item.get("summary")
                                                                    else "") for item in checks]),
         "deviations": _lines(deviations)},
        {"checks": [dict(item) for item in checks]} if checks else {}, references=tuple(references),
        history=(Event(now, "程序收集复现测试、项目检查与改动统计"),))


def review(writer: Writer, now: datetime, mode: str, output: Mapping[str, Any] | None,
           kept: Sequence[Mapping[str, Any]], error: str | None) -> HandoffDocument:
    blocking = [{"level": "blocking", "text": f"{item['problem']}(触发条件：{item['trigger']}；根源：{item['rootCause']})",
                 **({"location": item["location"]} if item.get("location") else {})} for item in kept]
    items = (output or {}).get("items") or []
    conclusion = (f"评审没有给出结果：{error}" if output is None
                  else ("有阻断项，交回修改。" if blocking else "没有阻断项。"))
    status = DocumentStatus.FAILED if output is None or blocking else DocumentStatus.DONE
    return HandoffDocument(
        writer.header("review", f"review-{mode}", status, f"fix-reviewer-{mode}", "fix/fix-executor", now),
        conclusion,
        {"verdict": (output or {}).get("analysis") or conclusion,
         "findings": _lines([item["text"] for item in blocking]),
         "basis": _lines([f"{item['itemId']}：{item['result']}，{item['reason']}" for item in items])},
        {"issues": blocking}, history=(Event(now, f"{mode} 评审完成"),))


def decision(writer: Writer, now: datetime, *, background: str, options: Sequence[tuple[str, str, bool]],
             recommendation: str, reason: str, name: str = "decision", source: str = "program") -> HandoffDocument:
    return HandoffDocument(
        writer.header("decision", name, DocumentStatus.BLOCKED, source, "user", now),
        background.split("\n")[0],
        {"background": background, "options": _lines([summary for _, summary, _ in options]),
         "recommendation": f"{recommendation}。理由：{reason}"},
        {"options": [{"id": key, "summary": summary, "recommended": chosen} for key, summary, chosen in options]},
        decisions=(Decision(background.split("\n")[0], recommendation, reason),),
        history=(Event(now, "生成待决定事项"),))
