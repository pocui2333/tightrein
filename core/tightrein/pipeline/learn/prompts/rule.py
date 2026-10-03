"""组装 rule-writer 的任务(redesign/08-learn.md「缺陷变规则」)：一个以已修复关闭的 Issue 写一条 Semgrep 规则；
semgrep-rule-variant-creator 已安装时加载它。工作目录为只读 worktree，便于对照全仓库的写法。"""

from __future__ import annotations

from pathlib import Path

from tightrein.pipeline.learn.prompts.common import RULE_WRITER, LearnPrompt, section
from tightrein.runner.roles import SEMGREP_RULE_VARIANT_CREATOR, installed_skills
from tightrein.runner.task import RunnerTask, Subject


def rule_task(prompt: LearnPrompt, issue_id: str, issue_text: str, finding: str, fix_report: str, patch: str,
              rules: str, workdir: Path | None) -> RunnerTask:
    return prompt.task(RULE_WRITER, RULE_WRITER, Subject("issue", issue_id), section("Issue", issue_text),
                       section("发现报告", finding), section("修复报告", fix_report), section("原补丁", patch),
                       section("规则库中已有的规则", rules),
                       skills=installed_skills(prompt.tool, (SEMGREP_RULE_VARIANT_CREATOR,)), workdir=workdir)
