"""修复报告 data/fixes/<Issue 编号>/report.md(architecture/07 第 7 章)：由修复的交接文档渲染。

frontmatter 按 handoff/frontmatter/report.schema.json 的 fix-report 类型；「任务外发现」节标题固定，incidental 探针据此读取。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from tightrein.contracts.validate import check
from tightrein.retrieval.frontmatter import PATH_TAG
from tightrein.store.files import atomic, markdown
from tightrein.store.files.markdown import MarkdownDocument

SCHEMA = "handoff/frontmatter/report.schema.json"
SECTIONS = ("结论", "改动内容", "涉及文件", "复现检查", "风险判定", "检查与评审结果", "偏离计划之处", "未验证项", "遗留事项",
            "任务外发现")
NONE = "无"
AFTER_FIX = {"passed": "修复后通过", "failed": "修复后仍然失败", "not-run": "由合并前验证执行", "invalid": "无法执行"}


def _lines(items: Sequence[str]) -> str:
    return "\n".join(f"- {item}" for item in items) if items else NONE


def _risk(risk: Mapping[str, Any] | None, label: str) -> str:
    if risk is None:
        return f"{label}：未判定"
    hits = "；".join(f"{hit['category']} {hit['rule']}" + (f"(`{hit['file']}`)" if hit["file"] else "")
                     for hit in risk["hits"])
    return f"{label}：{'高风险' if risk['level'] == 'high' else '常规'}" + (f"，依据 {hits}" if hits else "")


def conclusion(document: Mapping[str, Any]) -> str:
    outputs = document["outputs"]
    files = outputs.get("changedFiles") or []
    lines = sum(item["added"] + item["removed"] for item in files)
    head = {"ok": "评审通过，改动留在修复 worktree 中、未提交", "blocked": "等待用户",
            "failed": "转人工"}[document["status"]]
    reason = f"：{document['blockedReason']}" if document.get("blockedReason") else ""
    return f"{head}{reason}。改动 {len(files)} 个文件、{lines} 行。"


def frontmatter(document: Mapping[str, Any]) -> dict[str, Any]:
    outputs = document["outputs"]
    data = {
        "type": "fix-report", "id": f"fix-{outputs['issueId']}", "issueId": outputs["issueId"],
        "status": document["status"], "summary": conclusion(document), "runId": document["runId"],
        "tags": ["fix", *dict.fromkeys(f"{PATH_TAG}{item['path']}" for item in outputs.get("changedFiles") or [])],
        "branch": outputs["branch"], "baseCommit": outputs["baseCommit"], "createdAt": document["createdAt"],
        "updatedAt": document["createdAt"],
    }
    check(SCHEMA, data)
    return data


def sections(document: Mapping[str, Any]) -> dict[str, str]:
    outputs = document["outputs"]
    files = sorted(outputs.get("changedFiles") or [], key=lambda item: (PurePosixPath(item["path"]).name, item["path"]))
    rounds = outputs.get("rounds") or []
    results = [f"`{item['name']}`：退出码 {item['exitCode']}(日志 {item['log']})" for item in outputs.get("checks") or []]
    for item in rounds:
        reviews = "、".join(f"{review['mode']} {'通过' if review['passed'] else '不通过'}" for review in item["reviews"])
        results.append(f"第 {item['round']} 轮：确定性检查{'通过' if item['checksPassed'] else '不通过'}"
                       + (f"；评审 {reviews}" if reviews else "")
                       + "".join(f"\n  - {failure['check']}：{failure['problem']}" for failure in item["failures"]))
    results += [f"被丢弃的评审问题：{item['finding'].get('problem', '')}({item['reason']})"
                for item in outputs.get("discardedFindings") or []]
    risk = outputs.get("risk") or {}
    return {
        "结论": conclusion(document),
        "改动内容": outputs.get("summary") or NONE,
        "涉及文件": _lines([f"`{item['path']}`(+{item['added']} -{item['removed']})" for item in files]),
        "复现检查": _lines([f"{item['checkId']}({item['kind']})：{AFTER_FIX.get(item['afterFix'] or '', '未执行')}"
                            for item in outputs.get("reproCheck") or []]),
        "风险判定": _lines([_risk(risk.get("plan"), "出计划前"), _risk(risk.get("apply"), "实施后")]),
        "检查与评审结果": _lines(results),
        "偏离计划之处": _lines(outputs.get("deviations") or []),
        "未验证项": _lines([f"{item['item']}：{item['reason']}" for item in outputs.get("unverified") or []]),
        "遗留事项": _lines(outputs.get("leftovers") or []),
        "任务外发现": _lines([f"`{item['file']}" + (f":{item['line']}" if item.get("line") else "") + f"` {item['text']}"
                              for item in outputs.get("incidentalFindings") or []]),
    }


def render(document: Mapping[str, Any]) -> str:
    body = sections(document)
    text = f"# Issue {document['outputs']['issueId']} 的修复报告\n\n" + "\n\n".join(
        f"## {name}\n\n{body[name]}" for name in SECTIONS) + "\n"
    return markdown.render(MarkdownDocument(frontmatter(document), text))


def write(path: Path, document: Mapping[str, Any]) -> Path:
    atomic.write_text(path, render(document))
    return path
