"""验证报告 data/verify/<Issue 编号>/<日期>-<阶段>/report.md(architecture/07 15.2)：由验证的交接文档渲染。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from tightrein.contracts.validate import check
from tightrein.domain.enums import VerifyPhase
from tightrein.store.files import atomic, markdown
from tightrein.store.files.markdown import MarkdownDocument

SCHEMA = "handoff/frontmatter/report.schema.json"
SECTIONS = ("结论", "步骤与结果", "三档汇总", "未验证项", "截图", "服务日志")
CONCLUSIONS = {"passed": "通过", "failed": "失败", "awaiting-user": "等待用户"}
RESULTS = {"pass": "验证通过", "weak": "弱证据", "unverified": "未验证", "fail": "失败"}
NONE = "无"


def _lines(items: Sequence[str]) -> str:
    return "\n".join(f"- {item}" for item in items) if items else NONE


def summary(outputs: Mapping[str, Any]) -> str:
    text = f"{CONCLUSIONS[outputs['conclusion']]}"
    failed = next((item for item in outputs["items"] if item["result"] == "fail"), None)
    if failed is not None:
        text += f"：{failed['id']} {failed['reason'] or ''}".rstrip()
    return text + "。"


def render(document: Mapping[str, Any], logs: Sequence[str] = ()) -> str:
    outputs = document["outputs"]
    items = outputs["items"]
    frontmatter = {"type": "verify-report", "id": f"verify-{outputs['phase']}-{outputs['issueId']}",
                   "issueId": outputs["issueId"], "phase": outputs["phase"], "status": document["status"],
                   "summary": summary(outputs), "tags": ["verify", outputs["phase"]], "runId": document["runId"],
                   "commit": outputs["commit"], "createdAt": document["createdAt"]}
    check(SCHEMA, frontmatter)
    grouped = {label: [item["id"] for item in items if item["result"] == key] for key, label in RESULTS.items()}
    body = {
        "结论": summary(outputs),
        "步骤与结果": _lines([f"{item['id']}({item['category']})：{RESULTS[item['result']]}"
                              + (f"；命令 `{item['command']}`" if item["command"] else "")
                              + (f"；{item['reason']}" if item["reason"] else "")
                              + (f"；证据 {'、'.join(item['evidence'])}" if item["evidence"] else "") for item in items]),
        "三档汇总": _lines([f"{label}：{'、'.join(ids) or NONE}" for label, ids in grouped.items()]),
        "未验证项": _lines([f"{item['item']}：{item['reason']}" for item in outputs["unverified"]]),
        "截图": _lines([f"`{item['evidence'][0]}`：{RESULTS[item['result']]}" + (f"({item['reason']})" if item["reason"]
                                                                                 else "")
                          for item in items if item["category"] == "screenshot" and item["evidence"]]),
        "服务日志": _lines(list(logs)),
    }
    text = f"# Issue {outputs['issueId']} 的{VerifyPhase(outputs['phase']).label}报告\n\n"
    text += "\n\n".join(f"## {name}\n\n{body[name]}" for name in SECTIONS) + "\n"
    return markdown.render(MarkdownDocument(frontmatter, text))


def write(path: Path, document: Mapping[str, Any], logs: Sequence[str] = ()) -> Path:
    atomic.write_text(path, render(document, logs))
    return path
