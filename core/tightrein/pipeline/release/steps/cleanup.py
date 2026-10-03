"""收尾清理的说明(architecture/07 19.10)：删除 worktree 与本地分支需要确认两次；第一次确认时展示的风险。"""

from __future__ import annotations

from tightrein.domain.enums import CloseReason


def risks(branch: str, close_reason: CloseReason | None) -> str:
    lines = ["worktree 中未提交的内容会丢失(已检查工作区是干净的)。",
             f"git branch -d 只删除已合并的分支；{branch} 未合并时 git 会拒绝，本工具不使用 -D。",
             "远程分支由用户合并 PR 时在 GitHub 上删除。", "第二次确认时请输入分支名。",
             "复现检查(regressions/)不随清理删除，继续作为回归测试。"]
    if close_reason is not CloseReason.FIXED:
        lines.insert(1, f"Issue 以「{close_reason.label if close_reason else '未知'}」关闭，分支很可能没有合并，"
                        "git branch -d 会拒绝删除。")
    return "\n".join(f"- {line}" for line in lines)
