"""组装 fix-reviewer 的截图评审任务(architecture/07 13.2)：角色说明(skills/fix/references/roles/fix-reviewer.md) +
截图与页面说明；工作目录为报告目录，只读，readPaths 为截图文件；上限取 stages.verify.screenshotReview，
模型按调用点 verify.screenshot-review 的路由。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from tightrein.config.project import ProjectConfig
from tightrein.domain.enums import ReviewMode, Stage
from tightrein.runner.roles import join, read_only_task, reference, reviewer_setting
from tightrein.runner.task import RunnerTask, Subject
from tightrein.store.files.layout import ToolLayout

ROLE = "fix-reviewer"
ROLE_FILE = f"references/roles/{ROLE}.md"
SCHEMA = "handoff/outputs/fix-review.schema.json"
SETTING = "stages.verify.screenshotReview"
ROUTE = "verify.screenshot-review"


def task(tool: ToolLayout, config: ProjectConfig, run_id: str, issue_id: str, report_dir: Path,
         screenshots: Sequence[tuple[str, str]], attempt: int = 1) -> RunnerTask:
    """screenshots 为(截图路径，页面说明)，路径相对报告目录。"""
    listing = "\n".join(f"- `{path}`：{note}" for path, note in screenshots)
    body = join(reference(tool, "fix", ROLE_FILE), "本次为截图评审，输出的 `mode` 写 `screenshot`，只看下列截图，"
                "判断遮挡、错位、溢出这类布局问题。", f"## 截图与页面说明\n\n{listing}")
    found = read_only_task(run_id=run_id, stage=Stage.VERIFY, role=f"{ROLE}-{ReviewMode.SCREENSHOT.value}",
                           subject=Subject("issue", issue_id), attempt=attempt, prompt=body, workdir=report_dir,
                           output_schema=SCHEMA, role_setting=reviewer_setting(config, SETTING, ROUTE))
    return replace(found, read_paths=tuple(str(report_dir / path) for path, _ in screenshots))
