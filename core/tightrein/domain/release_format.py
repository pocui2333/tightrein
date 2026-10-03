"""分支名、提交信息、PR 标题与描述的格式(redesign/07-release.md 第 1 节)，纯函数。

格式串的占位符：分支 `{prefix}`(个人前缀，带结尾的 /，没有时为空)、`{type}`、`{issue}`(不补零的 Issue 编号)、
`{slug}`；提交 `{type}`、`{scope}`(带括号，没有范围时为空)、`{summary}`。通用格式：分支 `{prefix}{type}/{issue}-{slug}`，
提交 `{type}{scope}: {summary}`，空一行后写为什么这样改。类型由任务类型映射(git.branchTypes、git.commitTypes)，
立即修的 P0 分支类型为 hotfix。PR 描述不分固定小节：问题、为什么这样做、局限三段，之后是关联，最后是程序生成的验证结果；
项目有 PR 模板时按模板的二级标题把同样的内容放进对应的段落(fill_template)。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from tightrein.domain.enums import Severity, TaskType, Treatment

GENERIC_BRANCH = "{prefix}{type}/{issue}-{slug}"
GENERIC_COMMIT = "{type}{scope}: {summary}"
HOTFIX = "hotfix"
DEFAULT_TYPE = "default"
BRANCH_PLACEHOLDERS = ("prefix", "type", "issue", "slug")
COMMIT_PLACEHOLDERS = ("type", "scope", "summary")
_PLACEHOLDER = re.compile(r"\{(\w+)\}")
_SEGMENT = r"[a-z0-9]+(?:-[a-z0-9]+)*"


class FormatError(ValueError):
    """格式串含有不认识的占位符。"""


def placeholders(text: str) -> list[str]:
    return _PLACEHOLDER.findall(text)


def check_format(text: str, allowed: Sequence[str]) -> None:
    unknown = sorted(set(placeholders(text)) - set(allowed))
    if unknown:
        raise FormatError(f"格式 {text} 含有不认识的占位符：{'、'.join(unknown)}；可用 {'、'.join(allowed)}")


def kebab(text: str) -> str:
    """小写、非字母数字换成短横线、去掉首尾与重复的短横线。"""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def mapped(task_type: TaskType | None, table: Mapping[str, str]) -> str:
    key = task_type.value if task_type is not None else DEFAULT_TYPE
    return str(table.get(key, table[DEFAULT_TYPE]))


def branch_type(task_type: TaskType | None, treatment: Treatment | None, severity: Severity | None,
                table: Mapping[str, str]) -> str:
    if treatment is Treatment.IMMEDIATE and severity is Severity.P0:
        return HOTFIX
    return mapped(task_type, table)


def branch(fmt: str, *, kind: str, issue_id: str, slug: str, prefix: str | None = None) -> str:
    check_format(fmt, BRANCH_PLACEHOLDERS)
    number = issue_id.lstrip("0") or "0"
    return fmt.format(prefix=f"{prefix}/" if prefix else "", type=kind, issue=number, slug=kebab(slug))


def branch_pattern(fmt: str) -> re.Pattern[str]:
    """符合分支格式的分支名(类型与描述为小写短横线词，编号为数字，个人前缀可有可无)。"""
    check_format(fmt, BRANCH_PLACEHOLDERS)
    parts = {"prefix": rf"(?:{_SEGMENT}/)?", "type": _SEGMENT, "issue": r"\d+", "slug": _SEGMENT}
    regex = "".join(parts[piece] if index % 2 else re.escape(piece)
                    for index, piece in enumerate(_PLACEHOLDER.split(fmt)))
    return re.compile(rf"^{regex}$")


def commit_message(fmt: str, *, kind: str, scope: str | None, summary: str, why: str | None) -> str:
    check_format(fmt, COMMIT_PLACEHOLDERS)
    first = fmt.format(type=kind, scope=f"({scope})" if scope else "", summary=summary.strip())
    return first + (f"\n\n{why.strip()}" if why and why.strip() else "") + "\n"


@dataclass(frozen=True)
class PullText:
    """PR 描述的内容：正文三段、关联与程序生成的验证结果；缺少的段落为空。"""

    problem: str
    approach: str
    limitations: str
    relations: tuple[str, ...]
    verification: tuple[str, ...]


def _list(items: Sequence[str]) -> str:
    return "\n".join(f"- {item}" for item in items)


def pull_body(text: PullText, verification_title: str) -> str:
    """通用的 PR 描述：不分固定小节。"""
    parts = [part.strip() for part in (text.problem, text.approach, text.limitations) if part.strip()]
    if text.relations:
        parts.append("\n".join(text.relations))
    if text.verification:
        parts.append(f"---\n\n{verification_title}\n\n{_list(text.verification)}")
    return "\n\n".join(parts) + "\n"


# PR 模板的标题按关键词归到内容(小写比较，先命中先用)
TEMPLATE_KEYS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("verification", ("test", "verif", "测试", "验证", "テスト", "検証")),
    ("relations", ("related", "issue", "link", "关联", "相关", "関連")),
    ("limitations", ("limitation", "risk", "caveat", "known", "局限", "风险", "不足", "注意", "制限", "リスク")),
    ("approach", ("why", "approach", "how", "implementation", "solution", "change", "方案", "实现", "做法", "改动", "変更",
                  "実装")),
    ("problem", ("problem", "summary", "description", "what", "background", "context", "motivation", "问题", "背景",
                 "概要", "概括", "描述", "目的", "概要")),
)
_TEMPLATE_HEADING = re.compile(r"^(#{1,3}) +(.+?)[ \t]*$", re.MULTILINE)
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


def _template_key(title: str) -> str | None:
    lowered = title.lower()
    return next((key for key, words in TEMPLATE_KEYS if any(word in lowered for word in words)), None)


def fill_template(template: str, text: PullText, verification_title: str) -> str:
    """按模板的标题放进内容：认得出的标题下的注释与空白由内容取代，认不出的标题保留模板原文(复选框等)；
    没有任何一个认得出的标题时在模板之后附上通用格式的描述。"""
    contents = {"problem": text.problem.strip(), "approach": text.approach.strip(),
                "limitations": text.limitations.strip(), "relations": "\n".join(text.relations),
                "verification": _list(text.verification)}
    matches = list(_TEMPLATE_HEADING.finditer(template))
    if not matches or all(_template_key(match.group(2)) is None for match in matches):
        return template.rstrip() + "\n\n" + pull_body(text, verification_title)
    present = {_template_key(match.group(2)) for match in matches}
    prose = [key for key in ("problem", "approach", "limitations") if key in present]
    if prose:
        # 模板没有对应标题的正文段落并入第一个正文标题之下，不丢内容
        extra = [contents[key] for key in ("problem", "approach", "limitations")
                 if key not in present and contents[key]]
        contents[prose[0]] = "\n\n".join(part for part in (contents[prose[0]], *extra) if part)
    used: set[str] = set()
    parts = [template[:matches[0].start()].rstrip()]
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(template)
        original = template[match.end():end].strip()
        key = _template_key(match.group(2))
        if key is None or key in used:
            parts.append(f"{match.group(0)}\n\n{original}".rstrip())
            continue
        used.add(key)
        kept = _COMMENT.sub("", original).strip()
        body = contents[key] or kept
        if kept and kept != body and "- [" in kept:
            body = f"{body}\n\n{kept}"
        parts.append(f"{match.group(0)}\n\n{body}".rstrip())
    missing = [key for key in ("relations", "verification") if key not in used and contents[key]]
    tail = [contents["relations"]] if "relations" in missing else []
    if "verification" in missing:
        tail.append(f"---\n\n{verification_title}\n\n{contents['verification']}")
    return "\n\n".join(part for part in [*parts, *tail] if part) + "\n"
