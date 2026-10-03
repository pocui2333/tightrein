"""doc check <文件>(redesign/00-handoff-documents.md 第 4 节)：校验一份 Markdown 交接文档。

小节长度上限取 documents.sectionMaxChars 的核心缺省；给出 --workspace 时该工作区配置中写出的键覆盖缺省(其余键仍取缺省)。
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from tightrein.cli import exit_codes
from tightrein.cli.commands.common import group, leaf
from tightrein.cli.output import Outcome, error
from tightrein.config import project
from tightrein.store.files import documents

LIMITS = "documents.sectionMaxChars"


def _check(invocation: Any) -> Outcome:
    path: Path = invocation.args.file
    if not path.is_file():
        raise exit_codes.UsageError(f"文件不存在：{path}")
    limits = {key: int(value) for key, value in project.core_config().get(LIMITS).items()}
    if invocation.args.workspace is not None:
        limits.update({key: int(value) for key, value in invocation.app.config.get(LIMITS).items()})
    problems = documents.check(path.read_text(encoding="utf-8"), limits, str(path))
    if not problems:
        return Outcome("doc check", exit_codes.OK, [f"{path} 通过"], result=[])
    lines = [f"{path} 有 {len(problems)} 个问题", *(f"- {problem}" for problem in problems)]
    return Outcome("doc check", exit_codes.FAILED, lines, result=problems,
                   errors=[error("DocumentIssue", problem) for problem in problems])


def register(commands: Any, common: argparse.ArgumentParser) -> None:
    doc = group(commands, "doc", "Markdown 交接文档")
    checked = leaf(doc, common, "check", _check, "校验头信息、必需的小节、数据块的 schema 与小节长度", "doc check")
    checked.add_argument("file", type=Path, help="交接文档的路径")
