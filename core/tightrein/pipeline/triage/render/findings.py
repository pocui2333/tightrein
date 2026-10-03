"""发现报告 data/findings/<问题编号>.md(architecture/06 第 6 节)：只读交接文档，不查数据库。

frontmatter 按 handoff/frontmatter/report.schema.json 的 finding 类型；标签带根因文件(path:)与路由入口(route:)，供之后的
分诊按位置预取历史发现。正文第一句写判定与去向，第二句写根因位置与影响；时间按本机时区渲染并写明时区。
并入其他问题的(判定为空)不写发现报告。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import tzinfo
from pathlib import Path
from typing import Any

from tightrein.contracts.validate import check
from tightrein.domain.clock import parse_iso
from tightrein.domain.enums import Disposition, Verdict
from tightrein.retrieval.frontmatter import PATH_TAG, ROUTE_TAG
from tightrein.store.files import markdown
from tightrein.store.files.markdown import MarkdownDocument

SCHEMA = "handoff/frontmatter/report.schema.json"
SECTIONS = ("结论", "主张与事实", "证据", "触发条件", "反证检查", "影响面", "根因与引入", "值不值得修",
            "需用户定夺", "查了但不成立", "没查清的", "任务外发现", "评分")
FLAG_NAMES = {"design": "根因在设计本身", "dataStructure": "要动数据结构或存量数据",
              "publicContract": "会改变公共实现或接口契约"}
ROUTE = re.compile(r"^[A-Z]+ /")
NONE = "无"


def _lines(items: Sequence[str]) -> str:
    return "\n".join(f"- {item}" for item in items) if items else NONE


def _local(value: str, zone: tzinfo | None) -> str:
    moment = parse_iso(value)
    local = moment.astimezone() if zone is None else moment.astimezone(zone)
    return f"{local.strftime('%Y-%m-%d %H:%M')}({local.tzname()})"


def tags(outputs: Mapping[str, Any]) -> list[str]:
    found = ["triage"]
    found += [f"{PATH_TAG}{cause['file']}" for cause in outputs.get("rootCauses") or []]
    found += [f"{ROUTE_TAG}{entry}" for entry in outputs["claim"]["entryPoints"] if ROUTE.match(entry)]
    return list(dict.fromkeys(found))


def frontmatter(document: Mapping[str, Any]) -> dict[str, Any]:
    outputs = document["outputs"]
    data = {
        "type": "finding", "id": f"triage-{outputs['problemId']}", "problemId": outputs["problemId"],
        "status": outputs["disposition"], "summary": conclusion(outputs).split("。")[0] + "。", "tags": tags(outputs),
        "runId": document["runId"], "createdAt": document["createdAt"], "updatedAt": document["createdAt"],
        "verdict": outputs["verdict"], "severity": outputs.get("severity"), "disposition": outputs["disposition"],
        "treatment": outputs.get("treatment"), "triageCommit": outputs["triageCommit"],
    }
    check(SCHEMA, data)
    return data


def conclusion(outputs: Mapping[str, Any]) -> str:
    verdict, disposition = Verdict(outputs["verdict"]), Disposition(outputs["disposition"])
    severity = f"，严重度 {outputs['severity']}" if outputs.get("severity") else ""
    first = f"{outputs['claim']['title']}：判定为{verdict.label}{severity}，去向为{disposition.label}。"
    causes = "、".join(f"`{cause['file']}:{cause['line']}`" for cause in outputs.get("rootCauses") or [])
    impact = ((outputs.get("evidence") or {}).get("impact") or {}).get("consequence")
    second = []
    if causes:
        second.append(f"根因在 {causes}")
    if impact:
        second.append(f"影响：{impact}")
    return first + ("，".join(second) + "。" if second else "")


def _value(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def sections(outputs: Mapping[str, Any], created_at: str, zone: tzinfo | None) -> dict[str, str]:
    evidence = outputs.get("evidence") or {}
    worth = outputs.get("worth")
    claim = outputs["claim"]
    facts = [f"{number}. {fact['label']}：{_value(fact['value'])}" for number, fact in enumerate(claim["facts"], 1)]
    estimate = outputs.get("estimate")
    counter = []
    for item in evidence.get("counterEvidence") or []:
        upstream = item["upstreamValidation"]
        where = f" `{upstream['location']}`" if upstream["location"] else ""
        counter.append(f"{item['check']}：入口 `{item['entry']}`，上游校验 {upstream['status']}{where}，{item['result']}")
    impact = evidence.get("impact")
    introduced = [f"{item['commit']}(作者 {item.get('author') or '未知'}，PR {item.get('pr') or '无'})"
                  for item in outputs.get("introducedBy") or []]
    causes = [f"`{cause['file']}:{cause['line']}` {cause.get('symbol') or ''}".rstrip()
              for cause in outputs.get("rootCauses") or []]
    flagged = [f"{FLAG_NAMES[key]}：{value['reason']}(位置 {'、'.join(value['locations'])})"
               for key, value in (outputs.get("flags") or {}).items() if value.get("flagged")]
    source = evidence.get("sourceOfPhenomenon")
    refuted = NONE if not source else (f"{source['explanation']}(" + (
        f"挡住它的位置 `{source['location']}`" if source.get("location") else f"主张第 {source['factRef']} 条事实") + ")")
    return {
        "结论": f"{conclusion(outputs)}\n\n分诊时间：{_local(created_at, zone)}；取证 commit {outputs['triageCommit']}。\n\n"
              f"依据：{outputs['reason']}",
        "主张与事实": "\n\n".join([claim["statement"], "\n".join(facts) or NONE,
                              "入口：" + ("、".join(f"`{entry}`" for entry in claim["entryPoints"]) or NONE)]),
        "证据": _lines([f"`{fact['location']}` {fact['observation']}" for fact in evidence.get("facts") or []]),
        "触发条件": evidence.get("trigger") or NONE,
        "反证检查": _lines(counter),
        "影响面": NONE if not impact else (f"{impact['consequence']}(类别 {impact['kind']}，角色 "
                                          f"{'、'.join(impact['roles']) or NONE}，数据 {impact['data'] or NONE})"),
        "根因与引入": "\n\n".join([_lines(causes), "引入：" + ("；".join(introduced) or "未查到")]),
        "值不值得修": NONE if worth is None else (
            f"建议 {worth['recommendation']}：{worth['reason']}。处理标签 {outputs.get('treatment') or NONE}，任务类型 "
            f"{outputs.get('taskType') or NONE}，规模档 {outputs.get('sizeTier') or NONE}"
            + (f"(预估 {len(estimate['files'])} 个文件、{estimate['lines']} 行)" if estimate else "")
            + f"。方向：{worth['direction']}。"
            + (f"重估条件：{worth['reevaluateWhen']}。" if worth.get("reevaluateWhen") else "")),
        "需用户定夺": _lines(flagged),
        "查了但不成立": refuted,
        "没查清的": _lines([f"{item['item']}({'代码中可查' if item['source'] == 'code' else '只有用户知道'})"
                          for item in outputs.get("missingInfo") or []]),
        "任务外发现": _lines([f"`{item['file']}:{item.get('line') or ''}` {item['text']}"
                            for item in outputs.get("incidentalFindings") or []]),
        "评分": _lines([f"{item['itemId']}：{item['result']}({item['method']}) {item['reason']}".rstrip()
                      for item in outputs.get("scores") or []]),
    }


def render(document: Mapping[str, Any], zone: tzinfo | None = None) -> str:
    outputs = document["outputs"]
    parts = [f"# {outputs['claim']['title']}"]
    found = sections(outputs, document["createdAt"], zone)
    parts += [f"## {name}\n\n{found[name]}" for name in SECTIONS]
    return "\n\n".join(parts) + "\n"


def write(path: Path, document: Mapping[str, Any], zone: tzinfo | None = None) -> Path:
    markdown.write(path, MarkdownDocument(frontmatter(document), render(document, zone)))
    return path
