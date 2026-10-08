"""实施各调用点共用的拼装：变量的文字、方案字段的裁剪、调用模型与用量累计。

提示文字都在 `src/tightrein/prompts/<控制键>.md`，这里只给变量的值；schema 放在各小步骤的文件夹。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tightrein.agents.call import call, params_for
from tightrein.agents.params import Access
from tightrein.agents.result import CallResult
from tightrein.assess.issue.body import post_deploy
from tightrein.knowledge.match import render
from tightrein.prompts.build import build
from tightrein.protocol.handoff import Metrics, Tokens, Versions, load_schema
from tightrein.protocol.records import versions
from tightrein.protocol.security import external

if TYPE_CHECKING:
    from tightrein.implement.context import Decision, ImplementContext
    from tightrein.protocol.runtime import Runtime

NONE = "无"
# 编码只拿实施要用的方案字段；分析、证据与估算只在方案与定案时用(#8：各步不再带全量方案)
PLAN_FOR_CODE = ("summary", "hypothesis", "steps", "files", "protectedTouches", "migration", "newDependencies",
                 "deletions", "notDoing")
ACCEPTANCE_HEADINGS = ("验收标准", "acceptance", "受け入れ基準", "受入基準")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s+)?(.*\S)\s*$")


@dataclass
class Usage:
    """一步之内全部模型调用的累计，写进 handoff 的量化数据与版本。"""

    calls: int = 0
    retries: int = 0
    tokens: Tokens = field(default_factory=Tokens)
    cost_usd: float | None = None
    cost_estimated: bool = False
    duration_ms: int = 0
    last: CallResult | None = None
    prompt_hash: str | None = None

    def add(self, result: CallResult, prompt_hash: str) -> None:
        self.calls += result.attempts
        self.retries += result.retries
        self.tokens.add(result.tokens)
        if result.cost_usd is not None:
            self.cost_usd = (self.cost_usd or 0.0) + result.cost_usd
            self.cost_estimated = self.cost_estimated or result.cost_estimated
        self.duration_ms += result.duration_ms
        self.last = result
        self.prompt_hash = prompt_hash

    def metrics(self, **extra: Any) -> Metrics:
        return Metrics(calls=self.calls, retries=self.retries, tokens=self.tokens, cost_usd=self.cost_usd,
                       cost_estimated=self.cost_estimated if self.cost_usd is not None else None, **extra)

    def versions(self, runtime: Runtime) -> Versions:
        result = self.last
        return versions(runtime.tool.root, runtime.settings.hash, prompt_hash=self.prompt_hash,
                        tool=result.tool if result else None, tool_version=None,
                        model=result.model if result else None)


@dataclass(frozen=True)
class Ask:
    """一次模型调用的输入：调用点、变量、schema 文件与权限。"""

    point: str
    variables: Mapping[str, str]
    schema: Path
    conditions: tuple[str, ...] = ()
    access: Access | None = None
    allowed_commands: tuple[str, ...] = ()
    resume_session: str | None = None
    round: int | None = None


def ask(runtime: Runtime, context: ImplementContext, request: Ask, usage: Usage) -> CallResult:
    """拼提示、按调用点取参数、调用模型；用量记进 usage。工作目录为修复 worktree。"""
    schema = schema_of(request.schema)
    model = runtime.settings.model_for(request.point, request.conditions)
    prompt = build(request.point, request.variables, language=runtime.language, schema=schema, tool=model.tool)
    workdir = context.worktree or runtime.git.repo
    params = params_for(request.point, settings=runtime.settings, run=runtime.run, subject=context.issue.id,
                        prompt=prompt.text, schema=schema, workdir=workdir, conditions=request.conditions,
                        access=request.access, allowed_commands=request.allowed_commands,
                        resume_session=request.resume_session, round=request.round, prompt_hash=prompt.hash)
    result = call(params, runtime.agents)
    usage.add(result, prompt.hash)
    return result


@cache
def schema_of(path: Path) -> dict[str, Any]:
    return load_schema(path)


def worktree_of(context: ImplementContext) -> Path:
    """准备之后的各步都在修复 worktree 上做；还没有 worktree 说明流程顺序错了。"""
    if context.worktree is None:
        raise ValueError(f"Issue {context.issue.id} 还没有修复 worktree，先完成准备")
    return context.worktree


def failure_text(result: CallResult) -> str:
    return f"模型调用没有给出结果({result.status.value}{'：' + result.error if result.error else ''})"


# 变量的文字


def issue_text(context: ImplementContext) -> str:
    """Issue 正文含日志、报错等外部原文：整体当作数据包住。正文已以「# <编号> <标题>」开头，不再另加标题。"""
    return external(f"issue {context.issue.id}", context.body.strip())


def notes_text(context: ImplementContext) -> str:
    return context.notes.render() if context.notes is not None and context.notes.entries else NONE


def decisions_text(decisions: Sequence[Decision]) -> str:
    """用户的决定与补充：必须遵守并在输出中回应；没有时写「无」。"""
    lines = [f"- {item.at}({item.point}，{item.verdict}{_option(item.option)})：{item.note or '无补充'}"
             for item in decisions]
    return "\n".join(lines) or NONE


def knowledge_text(runtime: Runtime, context: ImplementContext) -> str:
    """命中本次文件的知识库条目(程序按路径匹配并截取，不让模型自己搜)。"""
    return render(context.knowledge, limit_tokens=int(runtime.settings.section("implement")["knowledgeTokens"]))


def list_text(items: Sequence[str]) -> str:
    return "\n".join(f"- {item}" for item in items) or NONE


def json_text(value: Any) -> str:
    return f"```json\n{json.dumps(value, ensure_ascii=False, indent=2)}\n```"


def plan_view(plan: Mapping[str, Any], keys: Sequence[str]) -> dict[str, Any]:
    """方案中给某一步的字段；根因假说只留因果链与修改位置，不带证据。"""
    found = {key: plan[key] for key in keys if key in plan and plan[key] not in (None, [], {})}
    if "hypothesis" in found:
        hypothesis = found["hypothesis"]
        found["hypothesis"] = {key: hypothesis[key] for key in ("cause", "edits") if key in hypothesis}
    return found


def acceptance(body: str) -> list[str]:
    """Issue 正文「验收标准」一节中实施阶段要满足的条目原文(方案逐条对应、审查逐条核对)。

    只能在部署后确认的(观察类来源的「部署后的观察期内不再出现…」，assess/issue/body.post_deploy)不在其中：它是验收
    阶段的标准，由发布的验收按关联问题的出现确认(44 号计划 6「验收：只做实施阶段做不到的」)。"""
    found: list[str] = []
    level: int | None = None
    for line in body.splitlines():
        heading = _HEADING.match(line)
        if heading is not None:
            depth, title = len(heading.group(1)), heading.group(2).strip().lower()
            if level is not None and depth <= level:
                break
            if level is None and any(word in title for word in ACCEPTANCE_HEADINGS):
                level = depth
            continue
        if level is not None:
            bullet = _BULLET.match(line)
            if bullet is not None and not post_deploy(bullet.group(1)):
                found.append(bullet.group(1).strip())
    return found


def _option(option: int | None) -> str:
    return "" if option is None else f"，选项 {option}"
