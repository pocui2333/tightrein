"""组装写复现测试的执行器任务(redesign/05-fix.md 第 5 步)：fix-executor 的第一轮，或安全、数据类由 repro-writer 在另一会话写。

任务说明：角色说明 + 修复规则 + 任务文档(目标、验收标准、约束) + 写测试的要求(缺陷与新功能须在当前代码上失败，
重构写表征测试须通过；新建一个匹配 testPaths 的独立测试文件；command 以允许前缀开头并选中该测试) + 相邻的已有测试
(steps/sibling_tests) + 用户的决定与补充 + 上一次不合格的原因。
可写修复 worktree(只允许改测试文件，由边界检查保证)，允许项目检查命令与只读命令。
"""

from __future__ import annotations

from collections.abc import Sequence

from tightrein.domain.enums import Access
from tightrein.pipeline.fix.prompts.common import STAGE, FixPrompt, decisions_text, feedback_text
from tightrein.pipeline.fix.steps.context import FixContext
from tightrein.pipeline.fix.steps.sibling_tests import SiblingTest
from tightrein.runner.roles import READ_ONLY_COMMANDS, join
from tightrein.runner.task import Instructions, RunnerTask

EXECUTOR = "fix-executor"
WRITER = "repro-writer"
SCHEMA = "runner/roles/repro-test.schema.json"
FAIL_FIRST = ("写一个表达验收标准的测试：缺陷写复现测试，新功能按验收标准写测试。它必须在当前(未修改的)代码上失败，"
              "修复或实现之后通过。")
PASS_FIRST = "这是重构：写表征测试，固定必须保持不变的现有行为。它必须在当前(未修改的)代码上通过。"
SIBLING_NOTE = ("下面是离被改代码最近的已有测试的开头部分。沿用其中的导入路径、夹具与 mock 写法，"
                "不臆造模块路径，不另建测试基类或新的夹具体系；需要的夹具已有时直接复用。")
CODE_FENCE = "````"  # 四个反引号：测试代码中可能出现三个反引号
SIGNATURE_NOTE = ("`expectedSignature` 写预期的报错信息(例如 `KeyError: 'group_key'`)，程序在基准版本上运行测试时核对"
                  "输出包含此签名才算有效复现；写不出明确签名时留空。")


def siblings_text(siblings: Sequence[SiblingTest]) -> str:
    if not siblings:
        return ""
    parts = [f"### `{item.path}`\n\n{CODE_FENCE}\n{item.excerpt}\n{CODE_FENCE}" for item in siblings]
    return join("## 相邻的已有测试(照着写)", SIBLING_NOTE, *parts)


def task(prompt: FixPrompt, context: FixContext, task_text: str, attempt: int, *, role: str, expects_pass: bool,
         test_paths: Sequence[str], prefixes: Sequence[str], check_commands: Sequence[str],
         siblings: Sequence[SiblingTest] = (), feedback: Sequence[str] = (),
         conditions: Sequence[str] = ()) -> RunnerTask:
    """conditions 为调用点的条件：fix-executor 的这一轮与写代码同一会话，须与写代码一轮的条件相同。"""
    rules = join("## 这一轮只写测试", PASS_FIRST if expects_pass else FAIL_FIRST,
                 "新建一个独立的测试文件，不改动其他任何文件，也不改已有的测试文件；文件名与所在目录沿用项目已有测试的习惯，"
                 "名称中带上 Issue 编号以便识别。",
                 f"测试文件须匹配 testPaths：{'、'.join(test_paths) or '无(项目没有配置 testPaths，输出 cannot-write)'}",
                 "运行命令 `command` 以下列允许前缀之一开头并只选中这个测试：\n"
                 + ("\n".join(f"- `{item}`" for item in prefixes) or "- 无(项目没有配置 checks.commands)"),
                 "`location` 写被测代码的「文件路径:行号」；写不出合格的测试时 status 为 cannot-write 并写明原因。",
                 SIGNATURE_NOTE)
    body = join(prompt.role(role), prompt.rules(), task_text, rules, siblings_text(siblings),
                decisions_text(context.decisions), feedback_text(feedback))
    setting = prompt.role_setting(role, context.complexity, conditions)
    return RunnerTask(
        run_id=prompt.run_id, stage=STAGE, role=role, subject=prompt.subject(context.issue_id), attempt=attempt,
        instructions=Instructions(body), workdir=prompt.workdir, output_schema=SCHEMA, access=Access.WORKSPACE_WRITE,
        allowed_commands=(*dict.fromkeys(check_commands), *READ_ONLY_COMMANDS), limits=setting.limits,
        route=setting.route, conditions=setting.conditions, tests_only=True,
    )
