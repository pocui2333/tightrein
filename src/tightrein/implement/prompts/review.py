"""审查(implement.review、implement.review.deep)的提示变量。

- 轻量审查：Issue、验收标准、代码笔记、方案摘要(根因假说只留因果链与修改位置)、用户的决定与补充、改动、自检的实际
  结果与疑似写死提示；修正轮次另给上一轮的问题，改动只给这一轮又改了的文件；
- 深度审查是盲审：只给验收标准、用户的决定与补充、合并进来的 main、最终改动与自检的实际输出，不给 Issue 正文、方案、
  根因假说与编码模型的任何说明，让它独立于编码时的思路判断；用户的决定是用户本人的要求(高于方案)，两种审查都核对它
  照做了没有。用户的决定只记了回答：轻量审查从方案摘要拿到当时交给用户拍板的问题与推荐(userDecisions)，深度审查
  拿不到方案，只把这些问题与推荐附在用户的决定之后(方案的其他内容不给)；
- 这次交付合并过 main(发布合并 main 后退回实施)时两种审查都拿到合并信息(MergedMain)：两侧都改了的文件、main 一侧
  的相关提交与 main 一侧对这些文件的改动。改动相对合并进来的 main 算，解决冲突时丢掉的 main 一侧在改动里只是一处
  删除，没有这份信息审查分不出它与方案内的删改。人在发布阶段解决过冲突的文件、用户在编码关卡上为改文件解决不了的
  冲突选过一侧的文件，另写明取了哪一侧：都与用户的决定一样作数，整份取了修复一侧的不再列 main 一侧的改动。编码
  关卡上的选择在用户的决定里只是「implement.code，选项 1/2」，这里写明它对应哪些文件、哪一侧。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from tightrein.implement.prompts.common import (
    NONE,
    acceptance,
    decisions_text,
    issue_text,
    json_text,
    list_text,
    notes_text,
    plan_view,
)
from tightrein.protocol.security import external
from tightrein.release.sync import BOTH, FIX_SIDE, MAIN_SIDE

if TYPE_CHECKING:
    from tightrein.implement.context import ImplementContext, Risk

# 方案交给用户拍板的点(userDecisions)也给：用户的决定只写了回答，没有问题本身审查核对不了它照做了没有
PLAN_FOR_REVIEW = ("summary", "hypothesis", "files", "acceptanceMapping", "notDoing", "migration", "userDecisions")
DIFF_LIMIT = 400_000  # 改动量已有上限，这里只防意外的超大补丁
MAIN_DIFF_LIMIT = 100_000  # main 一侧的改动只取两侧都改了的文件，这里只防意外的超大补丁
FULL_SCOPE = "本轮审查全部改动。"
INCREMENTAL_SCOPE = ("修正轮次：下面只给这一轮又改了的文件({files})。先逐条核对上一轮的问题是否已解决，没解决的照常列为"
                     "阻断项；再看这一轮的改动有没有引入新问题。")


# 人在发布阶段解决冲突时取的一侧(release.sync.resolution)，或用户在编码关卡上选的一侧：都与用户的决定一样作数
SIDES = {FIX_SIDE: "整份取了修复一侧：main 一侧对它的改动是人有意舍弃的，不报 `main-dropped`，下面也不列它",
         MAIN_SIDE: "整份取了 main 一侧：修复一侧原来对它的改动是人有意舍弃的",
         BOTH: "两侧合并：人合并的结果在提交 {merged}，那时已经没有的 main 一侧内容是人有意舍弃的，不报 `main-dropped`"
               "(用 `git show {merged}:{path}` 核对)"}
RELEASE_CHOICE = "人在发布阶段解决冲突时的取舍(与用户的决定一样作数)"
CODE_CHOICE = ("用户在编码关卡上为改文件解决不了的冲突选的一侧(这是用户的决定：用户的决定里 implement.code 关卡上的"
               "选项 1 为修复一侧、2 为 main 一侧)")


@dataclass(frozen=True)
class MergedMain:
    """这次交付合并进来的 main：两侧都改了的文件里，main 一侧的改动必须保留(人解决冲突时有意舍弃的除外)。"""

    main: str  # 合并进来的 main commit
    files: tuple[str, ...]  # 两侧都改了的文件(编码解决过冲突的，或 main 改到修复文件的、人解决过冲突的)
    commits: tuple[str, ...]  # main 一侧改到要保留的文件的提交(「短哈希 标题」)
    diff: str  # main 一侧对要保留的文件的改动：从合并前修复分支与 main 的分叉点到 main；整份取了修复一侧的不在其中
    resolved_by_code: bool  # 冲突由编码这一圈解决(否则由发布合并：git 自动合并或人解决)
    # 人在发布阶段解决过冲突的文件(或用户在编码关卡上为改文件解决不了的冲突选过一侧的文件) → 取了哪一侧
    sides: Mapping[str, str] = field(default_factory=dict)
    merged: str | None = None  # 发布完成的合并提交(人解决冲突的结果)


def light(context: ImplementContext, *, plan: Mapping[str, Any], diff: str, results: str, hints: Sequence[str],
          previous: Sequence[str], files: Sequence[str] | None, merge: MergedMain | None = None) -> dict[str, str]:
    """files 为 None 时审全部改动；否则为修正轮次这一轮又改了的文件。merge 为这次交付合并进来的 main。"""
    return {
        "issue": issue_text(context),
        "acceptance": list_text(acceptance(context.body)),
        "notes": notes_text(context),
        "plan": json_text(plan_view(plan, PLAN_FOR_REVIEW)) if plan else NONE,
        "decisions": decisions_text(context.decisions),
        "merge": merge_text(merge),
        "scope": FULL_SCOPE if files is None else INCREMENTAL_SCOPE.format(files="、".join(files) or NONE),
        "previous": list_text(previous),
        "diff": diff_text(diff),
        "results": results or NONE,
        "hints": list_text(hints),
    }


def deep(context: ImplementContext, *, diff: str, results: str, risk: Risk | None,
         merge: MergedMain | None = None, questions: Sequence[Mapping[str, Any]] = ()) -> dict[str, str]:
    """questions 为方案交给用户拍板的问题(userDecisions)：深度审查拿不到方案，只把它附在用户的决定之后。"""
    reasons = list(risk.reasons) if risk is not None else []
    return {
        "acceptance": list_text(acceptance(context.body)),
        "decisions": answered_text(context, questions),
        "merge": merge_text(merge),
        "risk": list_text(reasons),
        "diff": diff_text(diff),
        "results": results or NONE,
    }


def diff_text(diff: str) -> str:
    """被改的是项目代码，注释与字符串里可能有冒充指令的文字：当作数据包住。"""
    return external("diff", diff or "(没有改动)", limit=DIFF_LIMIT)


def answered_text(context: ImplementContext, questions: Sequence[Mapping[str, Any]]) -> str:
    """用户的决定与补充，之后附方案当时交给用户拍板的问题与推荐：用户的回答(如「第二点按 B」)只有对着问题才看得懂，
    确认方案时没另说的就是照推荐。没有用户的决定时不附(没有回答要核对)。"""
    text = decisions_text(context.decisions)
    asked = [f"- {item['question']}(方案推荐：{item.get('recommendation') or NONE})" for item in questions
             if isinstance(item, Mapping) and item.get("question")]
    if not context.decisions or not asked:
        return text
    return "\n".join([text, "", "方案当时交给用户拍板的问题(上面的回答针对这些；确认方案时没另说的，即照推荐)：", *asked])


def merge_text(merge: MergedMain | None) -> str:
    """合并进来的 main：两侧都改了的文件、人在发布阶段或用户在编码关卡上的取舍、main 一侧的提交与改动(项目代码，
    当作数据包住)；没有合并时写「无」。"""
    if merge is None:
        return NONE
    if merge.resolved_by_code:
        how = "合并时冲突，由编码解决" + ("；改文件解决不了的按用户选的一侧写成，见下一条" if merge.sides else "")
    else:
        how = "发布合并时合并，人解决过冲突的见下一条" if merge.sides else "发布合并时 git 自动合并"
    lines = [f"- 合并进来的 main：{merge.main[:12]}", f"- 两侧都改了的文件({how})：{'、'.join(merge.files)}"]
    if merge.sides:
        merged = (merge.merged or "HEAD")[:12]
        chosen = [f"{path} {SIDES.get(side, SIDES[BOTH]).format(merged=merged, path=path)}"
                  for path, side in merge.sides.items()]
        lines.append(f"- {CODE_CHOICE if merge.resolved_by_code else RELEASE_CHOICE}：{'；'.join(chosen)}")
    return "\n".join([
        *lines,
        f"- main 一侧改到这些文件的提交：{'；'.join(merge.commits) or NONE}",
        "- main 一侧对这些文件的改动(从合并前修复分支与 main 的分叉点到合并进来的 main)：",
        "",
        external("main 一侧的改动", merge.diff or "(没有改动)", limit=MAIN_DIFF_LIMIT),
    ])


def results_text(check: Mapping[str, Any]) -> str:
    """自检的实际结果：命令与结论、精简后的失败输出、本机运行检查、改动统计。"""
    counted = check.get("counted") or {}
    lines = [f"- 改动(不含测试与生成文件)：{counted.get('files', 0)} 个文件、{counted.get('lines', 0)} 行"]
    for item in check.get("commands") or []:
        lines.append(f"- `{item['command']}`：{item['result']}" + (f"({item['reason']})" if item.get("reason") else ""))
        if item.get("output"):
            lines.append(f"```\n{item['output']}\n```")
    for item in check.get("runtime") or []:
        lines.append(f"- {item['id']}：{item['result']}" + (f"({item['reason']})" if item.get("reason") else ""))
    return "\n".join(lines)
