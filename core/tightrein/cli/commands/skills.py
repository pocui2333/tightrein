"""skills check(architecture/09 5.1)：对仓库中的全部 skill 做机械检查，锁定的第三方 skill 不检查。"""

from __future__ import annotations

import argparse
from typing import Any

from tightrein.cli import exit_codes
from tightrein.cli.commands.common import group, leaf, packaging_context
from tightrein.cli.output import Outcome, error
from tightrein.packaging import skills_check


def _check(invocation: Any) -> Outcome:
    ctx = packaging_context(invocation)
    issues = skills_check.check(ctx.tool.skills_dir(), ctx.commands,
                                max_lines=int(ctx.config.get("packaging.skillBodyMaxLines")),
                                toc_lines=int(ctx.config.get("packaging.referenceTocLines")),
                                skip={skill.name for skill in ctx.lock()})
    if not issues:
        return Outcome("skills check", exit_codes.OK, ["skills check 通过"], result=[])
    lines = [f"skills check 发现 {len(issues)} 个问题"]
    lines += [f"- {issue.skill}({issue.rule})：{issue.detail}  {issue.path}" for issue in issues]
    return Outcome("skills check", exit_codes.FAILED, lines, result=[issue.to_dict() for issue in issues],
                   errors=[error("SkillIssue", f"{issue.skill} {issue.rule}：{issue.detail}") for issue in issues])


def register(commands: Any, common: argparse.ArgumentParser) -> None:
    skills = group(commands, "skills", "skill 的一致性检查")
    leaf(skills, common, "check", _check, "检查 frontmatter、正文行数、参考文件与引用的命令")
