# implement/prompts：组装各模型任务

## 是什么

实施各调用点的提示变量与调用参数。提示文字在 `src/tightrein/prompts/<控制键>.md`，输出格式在各小步骤文件夹的 `*.schema.json`，这里只给变量的值。

| 文件 | 调用点 |
|---|---|
| `common.py` | 共用：`ask`(拼提示、取参数、调用、累计用量)、`Usage`、Issue 正文(外部内容包住)、代码笔记、用户的决定、知识条目、验收标准解析、方案字段裁剪 |
| `locate.py` | `implement.locate`：笔记涉及前端文件时带 `frontend` 条件 |
| `design.py` | `implement.design`：风险判定、上限与受保护文件、重出原因；用户已同意按设计层面修复时加一句说明 |
| `frontend.py` | `implement.design.frontend` |
| `code.py` | `implement.code` 与续接 `implement.code.continue`；允许的检查命令 |

审查的提示(`review.py`)归审查一侧。

## 流程

各小步骤调用 `common.ask(runtime, context, Ask(...), usage)`：按调用点与条件取模型(`settings.model_for`) → `prompts.build` 拼提示(模板 + 变量 + schema) → `agents.call.params_for` 取参数(工作目录为修复 worktree) → `agents.call.call` → 用量记进 `Usage`，由步骤写进交接的量化数据与版本。

## 输入与输出

- 输入：`ImplementContext`(Issue、正文、代码笔记、知识条目、用户的决定、worktree)与步骤给的方案、修正说明等；
- 输出：模板变量(字符串)、调用条件(`high_risk`、`frontend`)、允许的命令；调用结果 `CallResult` 原样交回步骤。

## 配置

`controls.implement.knowledgeTokens`(知识条目的 token 上限)、各调用点的 `model`、`modelWhen`、`access`、`timeout`；`project.commands`(允许的检查命令)、`boundaries.changeCap`、`boundaries.protected.*`(方案的上限说明)。

## 设计依据

- 按步骤裁剪方案字段(旧 `PLAN_FOR_EXECUTOR`、`plan_view`)：编码不带根因证据与分析；
- 代码笔记与方案已在提示里，不再列交接文件的路径，免得模型再读一遍(旧 `_task_document`)；
- 调用点按条件选模型：高风险带 `high_risk`，前端带 `frontend`，取值在 settings 的 `modelWhen`；
- Issue 正文含外部原文(日志、报错)，经 `protocol.security.external` 整体当作数据包住，防注入。

## 不做什么

不写提示文字，不直接调用工具(一律经 `agents.call.call`)。
