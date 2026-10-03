# 10. 文档体系

## 10.1 三类文档

整条工作流产生的文档按「给谁用」分为三类，另有日志与内部知识两类支撑文档：

| 类别 | 回答的问题 | 读者 | 格式 | 是否作为下游输入 |
|---|---|---|---|---|
| 交接文档 | 这一步的结果是什么 | 下游模块、脚本 | JSON，带 schema 版本 | 是，唯一的下游输入 |
| 人读文档 | 需要人知道什么、决定什么 | 用户 | markdown + YAML frontmatter | 否(Issue 除外，见 10.3) |
| 中间过程文档 | 这一步是怎么做出来的 | 排障、重放、自我改进分析 | JSONL 与工具原始输出 | 否 |
| 日志 | 发生了什么、花了多少 | 监控、指标、自我改进 | JSONL，一行一个事件 | 否 |
| 内部知识 | agent 做事时该知道什么 | agent | skill、参考资料、经验库 | 作为 agent 的上下文 |

三类文档的分工：交接文档只记结果，要求可靠、可校验；人读文档只写人需要的结论与待决事项，由交接文档生成；中间过程文档保留全部细节，只在需要追查时读取。

## 10.2 交接文档

**原则**

- 模块之间只通过交接文档和数据库交接，下游不读上游的对话、日志或人读文档。
- 交接文档用 JSON。模型改写 JSON 时比改写 markdown 更不容易误改或覆盖已有内容。
- LLM 环节(分诊、静态巡检、修复)通过 agent 执行器产出结构化结果：支持 schema 约束的工具直接约束输出，不支持的由核心解析后重试(9.4)；核心先按 schema 校验，通过后才写入数据库。校验不通过就停止，不交给下一环节。
- 需要执行写操作的环节(创建 Issue、git 操作、提 PR)，LLM 只产出结构化的「变更请求」，由确定性脚本校验后执行，并限制次数，例如每个 Issue 同时最多一个 PR。
- 子 agent 可以花大量 token 探索，但交回的结果是精炼摘要，完整过程留在中间过程文档里。

**统一外层字段**

| 字段 | 说明 |
|---|---|
| `schemaVersion` | 交接文档格式的版本号 |
| `runId` | 本次运行 ID，与日志中的 `run_id` 相同 |
| `stage` | 产出环节：collect、aggregate、triage、issue、fix、verify、release、learn、improve |
| `subject` | 处理对象的类型与 ID，例如 `{ "type": "problem", "id": "P-0042" }` |
| `status` | `ok`、`blocked`(需要用户)、`failed`(出错) |
| `inputsRef` | 所用输入的引用：上游交接文档路径、数据库记录 ID、commit |
| `outputs` | 本环节特有的结果，每个环节有自己的 schema |
| `nextAction` | 建议的下一步，例如「交给 issue 创建」「进入人工队列」 |
| `blockedReason` | `status` 为 `blocked` 或 `failed` 时的原因 |
| `createdAt` | 生成时间(UTC) |

各环节 `outputs` 的 schema 放在核心的 `contracts/schemas/handoff/outputs/` 目录，与第 1 到 8 章定义的字段一一对应，例如分诊的 `outputs` 就是 3.8 的结构化结论。

**存放**：`data/runs/<runId>/handoff/<stage>-<对象ID>.json`(验证为 `verify-<阶段>-<Issue 编号>.json`)，同时写入数据库的 `handoffs` 索引。数据库是查询用的，文件是可追溯的原件。

## 10.3 人读文档

| 文档 | 生成时机 | 内容 |
|---|---|---|
| 运行摘要 | 每次运行结束 | 做了什么、产生了什么、等待用户的事项；以本机通知发出，全文存档 |
| 发现报告 | 分诊结束 | 判定、证据、根因、影响面、去向 |
| Issue | 分诊判为值得修 | 第 4 章的模板 |
| 验证报告 | 每次验证 | 每一步的命令与结果、三档结论 |
| 周报 | 每周 | 第 8 章的内容(result 类型交接文档 `data/reports/weekly-<周一日期>.md`) |
| 决定文档 | `learn` 生成控制措施或改进建议时 | 背景、选项、推荐与理由(12.3、14.4)，`data/improve/<建议编号>.md` |

**格式约定**

- frontmatter 放机读字段：`type`、`id`、`status`、`summary`、`tags`、`runId`、`createdAt`、`updatedAt`，以及各类文档特有的字段；正文给人读。报告类文档的 `id` 与对应交接文档的编号相同，例如发现报告为 `triage-P-0042`、修复报告为 `fix-0007`；Issue 用自身编号。
- 正文第一节是结论，用户只读这一节也能知道要不要处理；证据带 `文件路径:行号`；日期写绝对日期。
- 人读文档由交接文档渲染生成，内容以交接文档为准。唯一的例外是 Issue：用户会直接编辑它，因此 Issue 文件本身是数据来源，数据库中只存索引(4.2)。

## 10.4 中间过程文档

| 内容 | 来源 | 存放 |
|---|---|---|
| agent 会话记录 | 执行器从所用工具的事件输出(如 Claude Code 与 Antigravity CLI 的 stream-json)转换而来的统一事件格式；记录工具的会话 ID，需要时可以续跑 | `data/runs/<运行编号>/transcripts/<角色>-<对象编号>.jsonl` |
| 工具原始输出 | Schemathesis 报告、Playwright trace 与截图、构建日志、服务日志 | `data/runs/<runId>/raw/` |
| 重试与校验失败记录 | 编排脚本 | 写入日志(10.5)，并在会话记录中保留原文 |

- 中间过程文档只用于排障、重放和自我改进分析(第 14 章)，不作为任何环节的输入。
- 保留 30 天，与 2.14 中原始报告的保留期一致。

## 10.5 日志

**一个事件一行**，写入 `data/logs/events-<日期>.jsonl`。一次运行是一个 trace，每个环节、每次 agent 调用、每次工具执行是一个 span，字段名参照 OpenTelemetry GenAI 语义约定：

| 字段 | 说明 |
|---|---|
| `timestamp` | 事件时间(UTC) |
| `run_id`、`trace_id`、`span_id`、`parent_span_id` | 运行与层级关系 |
| `stage` | 所属环节 |
| `operation` | 操作类型，取 `invoke_agent`、`execute_tool`、`run_script`、`gate`(关卡判定)、`user_action` |
| `agent`、`model` | agent 名称与所用模型 |
| `input_tokens`、`output_tokens`、`cost_usd` | 用量 |
| `duration_ms` | 耗时 |
| `status`、`error_type` | 结果与错误类型 |
| `decision`、`reason` | 关卡或 agent 做出的决定及理由摘要，例如「判为误报：上游已校验」 |
| `score` | 该步骤的评分结果，见第 12 章 |
| `artifact` | 相关交接文档、人读文档或中间过程文档的路径 |
| `attributes` | 各操作特有的补充信息，JSON 对象，例如检索的查询串与返回的编号；同样经过脱敏 |

**约定**

- 日志只记摘要和引用，不记完整的提示与输出，完整内容在中间过程文档中。
- 写盘前脱敏：token、密码、连接串、个人信息一律不记。
- 保留 90 天。指标(8.3)和自我改进(第 14 章)都从日志与数据库计算。

## 10.6 内部知识

| 类型 | 组织方式 |
|---|---|
| skill | `SKILL.md` 正文不超过 500 行；参考资料放在 `references/`，只从 `SKILL.md` 引用一层；超过 100 行的参考文件顶部加目录；确定性步骤写成脚本，不写在提示里 |
| 经验库(分诊经验、修复经验) | 与其他知识条目相同：每条经验是 `knowledge/triage-lesson/` 或 `knowledge/fix-lesson/` 下的一个文件 `<编号>-<简称>.md`，frontmatter 为 16.3 的字段加 `sourceRunId`；每个目录有一个自动生成的 `INDEX.md`，一行一条；命中次数只记录在数据库 `knowledge_meta` 中，不写进 frontmatter；写入经过去重(16.7)，只新增、更新、合并或改变状态，不整体重写 |
| 评测集 | 每个 skill 至少 3 个用例，取自真实运行记录(architecture/03 2.3)；修复部署后确认通过的真实修复另存为运行即评测的用例(8.7) |

经验库由 `learn` 定期整理(8.5)：经验类条目过了复核日期且长期未被命中的自动归档，仍被命中的续期；其他类型的过期条目与同类条目的矛盾、重复生成复核建议，由用户决定；条目只改为 `superseded` 或 `archived`，不删除文件。
