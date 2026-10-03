"""提示文件的拼装(architecture/02 2.5 共同做法)：任务说明、skill 正文与参考资料、上下文条目由核心拼成一个文件交给工具，
不依赖各工具自己发现 skill 的机制，换工具时模型看到的内容相同。

skill 正文为 `skills/<名称>/SKILL.md`，参考资料路径相对该 skill 目录；工具不能按 schema 约束输出时，提示末尾附上
展开引用后的 schema 全文。给出 language(project.language)时，在「输出」之前加「输出语言」一节：所有执行器任务都经
这里拼装，语言要求只写这一处，不在各角色说明中重复。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tightrein.domain import language as languages
from tightrein.runner.output import CODE_FENCE
from tightrein.runner.result import RunnerConfigError
from tightrein.runner.task import RunnerTask
from tightrein.store.files.layout import ToolLayout, segment


def _read(path: Path, what: str) -> str:
    if not path.is_file():
        raise RunnerConfigError(f"{what}不存在：{path}")
    return path.read_text(encoding="utf-8").strip()


LANGUAGE_NOTE = ("输出中所有给人读的文字(结论、标题、问题描述、触发条件与复现步骤、证据说明、影响、验收标准、修复计划与"
                 "改动说明、PR 描述与评论的叙述)都用{name}书写；代码、文件路径、标识符、命令、配置键、JSON 字段名与引用的"
                 "原文保持原样，不翻译。")


def build_prompt(task: RunnerTask, tool: ToolLayout, schema: dict[str, Any] | None = None,
                 retry_note: str | None = None, language: str | None = None) -> str:
    # 提示词排布(38-external-techniques.md 第 7 项)：
    # 1. 固定部分(项目规范/skills、输出语言、输出 schema 说明)放在最前，且不含时间戳、运行编号等变化内容，以提高缓存命中率；
    # 2. 其后是本次任务的目标、验收标准与注意事项(task instructions)；
    # 3. 再后是背景与引用(上下文条目、重试说明)。
    sections = []

    # 固定部分：项目规范(skill 与参考资料)
    for skill in task.instructions.skills:
        skill_file = tool.skill(skill.name)
        sections += [f"# skill：{skill.name}", _read(skill_file, f"skill {skill.name} 的 SKILL.md ")]
        for reference in skill.references:
            parts = [segment(part) for part in Path(reference).parts]
            sections += [f"## 参考资料：{reference}", _read(skill_file.parent.joinpath(*parts), "参考资料")]

    # 固定部分：输出语言与 schema 说明
    if language:
        sections += ["# 输出语言", LANGUAGE_NOTE.format(name=languages.name(language))]
    if task.output_schema is not None:
        sections += ["# 输出", f"只输出一个符合 {task.output_schema} 的 JSON 对象，不要输出其他文字。"]
        if schema is not None:
            text = json.dumps(schema, ensure_ascii=False, indent=2)
            sections.append(f"{CODE_FENCE}json\n{text}\n{CODE_FENCE}")

    # 本次任务的目标、验收标准与注意事项
    sections += ["# 任务", task.instructions.prompt.strip()]

    # 背景与引用
    if task.instructions.context:
        sections.append("# 上下文")
        sections += [f"- [{item.id}] {item.type}：{item.summary}(路径 {item.path}；{item.reason})"
                     for item in task.instructions.context]
    if retry_note:
        sections += ["# 重试说明", retry_note]

    return "\n\n".join(sections) + "\n"
