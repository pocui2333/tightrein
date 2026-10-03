"""运行即评测的用例(redesign/08-learn.md 自我改进)：真实修复的记录作为 fix 的评测用例。

一个 Issue 的修复合并且部署后确认通过时，verify 保存 data/eval/cases/<Issue 编号>.json：
{schemaVersion, issueId, title, lane, capturedAt, issue(Issue 正文), baseCommit(修复前的提交), mergeCommit,
 deployCommit, reproduceTests[{checkId, kind, path, content}](登记的复现测试), patch(最终补丁), input}；
input 指向同目录下的 <Issue 编号>.input.json(该 Issue 的 issue 环节交接文档副本，作为评测时 fix 的 --input)。

评测 fix 时这些用例与 evals/ 中封存的用例一起使用：用例编号为 Issue 编号，来源对象为该 Issue(自我改进据此区分
参与改进与未参与改进的用例)，以修复前的提交为基准运行，按修复评分表评分。用例由程序写在 data/ 下，不经 manifest
封存；评测开始时记录两个文件的哈希，续跑时核对。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from tightrein.domain.enums import Stage
from tightrein.evaluation.errors import CaseProblem
from tightrein.store.files.layout import WorkspaceLayout

CASE_VERSION = 1
INPUT_SUFFIX = ".input.json"
REQUIRED = ("issueId", "title", "baseCommit", "input")


def key(issue_id: str) -> str:
    return f"{Stage.FIX.value}/{issue_id}"


def digest(layout: WorkspaceLayout, issue_id: str) -> str:
    """用例文件与输入交接文档合起来的 sha256。"""
    sha = hashlib.sha256()
    for path in (layout.run_case(issue_id), layout.run_case(issue_id, INPUT_SUFFIX)):
        sha.update(path.name.encode("utf-8") + b"\0" + path.read_bytes())
    return sha.hexdigest()


def read(layout: WorkspaceLayout) -> tuple[list[dict[str, Any]], dict[str, str], list[CaseProblem]]:
    """(用例内容, 用例键到哈希, 问题)；按 Issue 编号排序，不合格的文件列为问题而不使用。"""
    directory = layout.run_cases_dir()
    cases, hashes, problems = [], {}, []
    paths = sorted(path for path in directory.glob("*.json") if not path.name.endswith(INPUT_SUFFIX)) \
        if directory.is_dir() else []
    for path in paths:
        name = f"data/eval/cases/{path.name}"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            problems.append(CaseProblem(name, f"不是合法的 JSON：{error}"))
            continue
        missing = [field for field in REQUIRED if not isinstance(data, dict) or not data.get(field)]
        if missing:
            problems.append(CaseProblem(name, f"缺少 {'、'.join(missing)}"))
            continue
        if data["issueId"] != path.stem or data["input"] != f"{path.stem}{INPUT_SUFFIX}" \
                or not (directory / data["input"]).is_file():
            problems.append(CaseProblem(name, "Issue 编号与文件名不一致，或输入交接文档不存在"))
            continue
        cases.append(data)
        hashes[key(data["issueId"])] = digest(layout, data["issueId"])
    return cases, hashes, problems
