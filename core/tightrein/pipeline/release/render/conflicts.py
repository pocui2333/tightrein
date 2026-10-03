"""冲突报告 data/fixes/<编号>/conflicts.md(architecture/07 19.2 第 5 步)：每个冲突文件列出两侧的相关提交与冲突片段。

两侧都改了同一段；如果两侧实现的是同一功能，属于互斥实现，由用户选择保留哪一侧，本工具不自行选择，也不修改冲突文件。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class ConflictFile:
    path: str
    fix_commits: str
    main_commits: str
    hunks: Sequence[tuple[str, str]]


MUTUAL = "两侧都修改了这一段；如果两侧实现的是同一功能，属于互斥实现，需要用户选择保留哪一侧"


def render(issue_id: str, files: Sequence[ConflictFile]) -> str:
    parts = [f"# Issue {issue_id} 合并 origin/main 的冲突", "",
             "本工具不自行选择任何一侧，也不修改冲突文件。逐个文件解决后执行：", "",
             f"- `tightrein release sync {issue_id} --continue`：完成合并提交(需要确认)",
             f"- `tightrein release sync {issue_id} --abort`：放弃合并，回到合并前的状态(需要确认)"]
    for item in files:
        parts += ["", f"## {item.path}", "", "修复分支一侧的提交：", "", f"```\n{item.fix_commits.strip() or '无'}\n```",
                  "", "origin/main 一侧的提交：", "", f"```\n{item.main_commits.strip() or '无'}\n```"]
        for number, (ours, theirs) in enumerate(item.hunks, start=1):
            parts += ["", f"### 冲突片段 {number}", "", MUTUAL, "", "修复分支：", "", f"```\n{ours}\n```", "",
                      "origin/main：", "", f"```\n{theirs}\n```"]
    return "\n".join(parts).rstrip() + "\n"
