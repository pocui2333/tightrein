"""运行即评测(redesign/08-learn.md 自我改进)：修复合并且部署后确认通过时，把这次修复存为 fix 的
评测用例 data/eval/cases/<Issue 编号>.json(格式见 evaluation/run_cases.py)，并复制该 Issue 的 issue 环节交接文档
为 <Issue 编号>.input.json。同一 Issue 再次确认通过时覆盖。

材料取自：Issue 文件(正文)、最近一份修复交接文档(baseCommit、lane)、PR 记录(合并提交)、登记的复现检查(文件内容)、
git 中修复前到合并提交的补丁。缺少任何一项时不保存，返回原因。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from tightrein.domain.clock import format_iso
from tightrein.domain.enums import RunStage
from tightrein.evaluation.run_cases import CASE_VERSION, INPUT_SUFFIX
from tightrein.pipeline.common import stage_runs
from tightrein.store.files import atomic
from tightrein.store.files.layout import WorkspaceLayout
from tightrein.store.repos import handoffs, issues, pulls, regressions
from tightrein.vcs.errors import VcsError
from tightrein.vcs.git_read import GitReader


def capture(layout: WorkspaceLayout, conn: sqlite3.Connection, git: GitReader, repo: Path, issue_id: str,
            deploy_commit: str, now: datetime) -> tuple[Path | None, str | None]:
    """返回(保存的用例文件, 没有保存的原因)。"""
    record = issues.get(conn, issue_id)
    fixed = stage_runs.latest_outputs(conn, layout, RunStage.FIX, issue_id)
    pull = pulls.get(conn, issue_id)
    source = handoffs.get(conn, RunStage.ISSUE, issue_id)
    if record is None or source is None:
        return None, "没有 Issue 文件或 issue 环节的交接文档"
    if fixed is None or not fixed[1].get("baseCommit"):
        return None, "没有修复交接文档"
    if pull is None or pull.merge_commit is None:
        return None, "没有合并提交"
    base = fixed[1]["baseCommit"]
    try:
        patch = git.patch(repo, base, pull.merge_commit)
    except VcsError as error:
        return None, f"读不到修复前到合并提交的补丁：{error}"
    tests = [{"checkId": check.check_id, "kind": check.kind.value, "path": check.path,
              "content": (layout.root / check.path).read_text(encoding="utf-8")
              if (layout.root / check.path).is_file() else None}
             for check in regressions.find(conn, issue_id=issue_id)]
    data = {"schemaVersion": CASE_VERSION, "issueId": issue_id, "title": record.issue.title,
            "lane": fixed[1].get("lane"), "capturedAt": format_iso(now),
            "issue": (layout.root / record.path).read_text(encoding="utf-8"), "baseCommit": base,
            "mergeCommit": pull.merge_commit, "deployCommit": deploy_commit, "reproduceTests": tests, "patch": patch,
            "input": f"{issue_id}{INPUT_SUFFIX}"}
    target = layout.run_case(issue_id, INPUT_SUFFIX)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(layout.root / source.path, target)
    path = layout.run_case(issue_id)
    atomic.write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return path, None
