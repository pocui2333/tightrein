# 能力层：runner、guards、vcs

本篇描述能力层中与外部程序打交道的三个组件：`runner` 调用 agent 工具，`guards` 在工具之外再检查一遍边界，`vcs` 封装 git 与 gh。编号、枚举、表、路径与配置的名称沿用 `01-foundation.md`。

各工具命令行参数于 2026-09-29 按官方文档核对：Claude Code 的 CLI 参考与 headless 文档、Codex CLI 的命令参考与非交互模式文档、gh 2.97.0 的 `--json` 字段清单。Antigravity CLI(`agy`，Gemini CLI 于 2026-06-18 停用后的继任者)按 agy 1.2.14 的 `--help` 与 2026-10-01 的实测核对(计划 24)。

## 1. 三个组件的关系

### 1.1 调用关系

| 调用方 | 调用 | 用途 |
|---|---|---|
| `collect`(静态巡检)、`triage`、`learn`(含 `learn improve`)、`evaluation` | `runner.run` | 无人值守的 agent 任务 |
| `fix` | `runner.run`、`runner.run_interactive`、`guards.check_diff`、`vcs` 的建分支与 worktree | 修复会话、各角色任务、确定性检查 |
| `release` | `vcs` 的只读查询与写操作 | 提交、同步主干、推送、提 PR、跟踪、清理 |
| `collect`、`verify`、`learn` | `vcs` 的只读查询 | blame、PR 与 commit 状态(部署记录经扩展点 `deploy-source` 读取) |
| `runner` | `guards.before`、`guards.after` | 每一次 agent 进程运行前后，不论任务来自哪个模块 |
| `guards` | `vcs` 的只读查询 | 取 git 状态快照 |

`guards` 由 `runner` 在每次启动 agent 进程时调用，调用方不需要也不能跳过；`fix` 另外直接调用 `guards.check_diff` 执行 5.2 第 4 步的 diff 规则。三个组件都属于能力层，相互依赖符合 00 中的依赖规则。

### 1.2 共同约定

| 约定 | 内容 |
|---|---|
| 启动外部程序 | 一律以参数列表启动，不经过 shell；工作目录显式传入；标准输出与错误输出分别捕获 |
| 环境变量 | agent 进程的环境由 `guards.credentials.build_env` 生成；`vcs` 自己执行 git 时固定设置 `GIT_TERMINAL_PROMPT=0`、`LC_ALL=C`，避免交互提示与本地化输出影响解析；git、gh 的标准输入为 `/dev/null`(有输入时为管道)，在新的会话中运行、没有控制终端，ssh 与凭证助手不会等待输入 |
| 时间 | 一律经 `Clock` 获取，超时计时使用单调时钟 |
| 事件 | agent 调用写 `invoke_agent` span；git、gh 与检查命令写 `run_script` span；边界判定写 `gate` 事件；用户确认写 `user_action` 事件 |
| 临时文件 | 传给外部程序的提示、schema、策略、提交信息、PR 描述等文件，都写在本次运行的 `raw/` 下，不写进任何 worktree |

## 2. runner

### 2.1 职责

- 接收统一格式的任务(9.4)，按配置选定工具与模型，交给对应适配器执行，交回统一格式的结果。
- 在工具之外保证输出格式、时间、轮数与预算(9.5)：输出按 `outputSchema` 校验，不合格重试一次；超时终止进程；超出预算不启动。
- 把各工具的会话输出转换为统一事件格式，写入会话记录。
- 以交互模式启动与续接修复会话。
- 提供 `replay` 适配器，使上层模块可以在不调用任何模型的情况下得到确定的结果。

`runner` 不做业务判断：不解读结构化结果的含义，不决定失败后下一步做什么。

### 2.2 文件划分

```
runner/
  task.py              RunnerTask、Limits、Access 数据类，与 runner/runner-task.schema.json 互转
  result.py            RunnerResult、Usage、RunnerStatus 数据类，与 runner/runner-result.schema.json 互转
  service.py           run、run_interactive、resume_interactive 的完整流程
  registry.py          按工具名取适配器；解析工具、模型与推理强度(配置、命令行覆盖、模型档)
  process.py           启动子进程(独立进程组)、逐行读取输出、超时与终止
  limits.py            预算检查与累计、按 token 估算费用、无原生轮数上限时的计数
  output.py            从文本中提取 JSON、按 schema 校验、生成重试说明
  transcript.py        统一事件的数据结构、写入与脱敏
  sessions.py          交互会话记录的读写(agent_sessions 表)
  recording.py         回放录制集的读取与校验
  adapters/
    base.py            Adapter 协议、Invocation、ParsedRun
    claude.py          Claude Code
    codex.py           Codex CLI
    agy.py             Antigravity CLI(agy)
    replay.py          回放录制结果
```

### 2.3 任务与结果

**任务 `RunnerTask`**：9.4 的字段加上定位与路由所需的字段。

| 字段 | 类型 | 说明 |
|---|---|---|
| `runId` | 运行编号 | 所属运行，决定会话记录与原始输出的目录 |
| `stage` | `Stage` | 所属环节，用于预算与配置查找 |
| `role` | 字符串 | agent 角色，例如 `claim-verifier`、`fix-executor`、`static-review` |
| `subject` | 对象引用 | `{ "type": "problem", "id": "P-0042" }`，与交接文档的 `subject` 相同 |
| `attempt` | 整数 | 同一角色对同一对象的第几次运行，由调用方给出 |
| `instructions` | 对象 | `prompt`(本次任务说明)、`skills`(要加载的 skill 名与参考资料路径)、`context`(retrieval 组装的上下文条目) |
| `workdir` | 路径 | 只读 worktree、修复 worktree 或工作区 |
| `outputSchema` | schema 引用 | `contracts/schemas/` 下的相对路径；交互任务可以为空 |
| `access` | `read-only`、`workspace-write` | 访问级别 |
| `allowedCommands` | 字符串列表 | 允许执行的命令前缀，例如 `git log`、`dotnet build` |
| `limits` | 对象 | `maxTurns`、`maxDurationMs`、`maxCostUsd`；缺省时取 `project.yaml` 中该环节的值 |
| `interactive` | 布尔 | 是否交互运行 |
| `tool`、`model` | 字符串，可空 | 显式指定；为空时按 `stages.<stage>` 与模型档解析 |
| `capability` | 字符串，可空 | 模型档(design 9.6)，例如 `strong`；为空时按 `stages.<stage>.roles.<角色>.capability` 与 `roleCapabilities.<角色>` 解析，档对应的模型与推理强度取自 `capabilities` |
| `effort` | 字符串，可空 | 推理强度；为空时取所选模型档在该工具上的 `effort`；适配器翻译成该工具的参数(Claude Code 与 agy 为 `--effort`，Codex CLI 为 `-c model_reasoning_effort=<强度>`)；实际使用的强度记在 `invoke_agent` span 的属性中 |
| `approvedProtectedPaths` | 路径列表 | 用户在确认修复计划时单独放行的受保护文件，只对 `fix-executor` 有意义 |
| `web` | 布尔 | 是否允许联网检索；核心当前没有角色设为真 |
| `readPaths` | 路径列表 | 工作目录之外允许读取的文件，例如截图评审要查看的截图 |

**结果 `RunnerResult`**

| 字段 | 类型 | 说明 |
|---|---|---|
| `status` | `RunnerStatus` | `ok`、`failed`、`limit-reached`、`schema-invalid`、`guard-violation` |
| `errorType` | 字符串，可空 | 细分原因，取值见 2.11 |
| `output` | 对象，可空 | 已按 `outputSchema` 校验的结构化结果；状态不是 `ok` 时为空 |
| `usage` | 对象 | `inputTokens`、`outputTokens`、`cachedInputTokens`、`costUsd`、`costEstimated`(费用是否为估算)；工具不提供的字段为空 |
| `durationMs` | 整数 | 全部尝试的总耗时 |
| `attempts` | 整数 | 实际调用次数，含格式重试 |
| `tool`、`model` | 字符串 | 实际使用的工具与模型 |
| `sessionId` | 字符串，可空 | 工具的会话 ID |
| `transcriptPath` | 路径 | 统一格式的会话记录 |
| `guardReport` | 路径 | 本次边界检查报告 |
| `violations` | 列表 | `status` 为 `guard-violation` 时的违规项摘要 |

### 2.4 接口

```python
def run(task: RunnerTask, *, clock: Clock, runner_override: str | None = None,
        model_override: str | None = None) -> RunnerResult:
    """无人值守执行一次任务。runner_override 与 model_override 对应命令行的 --runner、--model。"""

def run_interactive(task: RunnerTask, *, clock: Clock, first_input: str) -> RunnerResult:
    """以交互模式启动会话，终端交给 agent 工具，会话结束后返回。task.interactive 必须为真。"""

def resume_interactive(stage: Stage, role: str, subject_id: str, *, clock: Clock,
                       first_input: str) -> RunnerResult:
    """续接该对象最近一次交互会话；工具不支持按会话续接时返回 errorType=resume-unsupported，由调用方新开会话。"""

class Adapter(Protocol):
    name: str
    def build(self, task: RunnerTask, files: InvocationFiles, *, retry: RetryContext | None) -> Invocation: ...
    def build_interactive(self, task: RunnerTask, files: InvocationFiles, first_input: str,
                          session_id: str | None) -> Invocation: ...
    def parse(self, raw_stdout: Path, exit_code: int) -> ParsedRun: ...
    def to_events(self, raw_stdout: Path) -> Iterator[TranscriptEvent]: ...
    def locate_session(self, workdir: Path, started_at: datetime) -> SessionRef | None: ...
    supports_schema: bool          # 工具能否按 schema 约束输出
    supports_turn_limit: bool      # 工具能否设置轮数上限
    supports_budget_limit: bool    # 工具能否设置费用上限
    supports_resume_by_id: bool    # 能否按会话 ID 续接
    supports_image_input: bool     # 能否读取图片，截图评审据此决定是否交给用户

@dataclass(frozen=True)
class Invocation:
    argv: list[str]
    cwd: Path
    stdin: bytes | None
    env: dict[str, str]            # 由 guards.credentials.build_env 生成后交给适配器补充工具需要的非凭证变量

@dataclass(frozen=True)
class ParsedRun:
    session_id: str | None
    final_text: str | None
    structured: dict | None        # 工具原生结构化结果；没有时为空，由 output.py 从 final_text 中提取
    usage: Usage
    turns: int | None
    ended_by: Literal["completed", "turn-limit", "budget-limit", "error"]
    error_message: str | None
```

### 2.5 适配器映射

**共同做法**

- 任务说明、skill 正文与上下文由核心拼成一个提示文件，交给工具，不依赖各工具自己发现 skill 的机制，保证换工具时模型看到的内容相同。
- 提示文件在「输出」之前有一节「输出语言」(`runner/prompt.build_prompt`，取 `project.language`，缺省 en)：所有给人读的文字按该语言书写，代码、路径、标识符、命令、配置键、JSON 字段名与引用原文保持原样。全部执行器任务都经这里拼装，角色说明中不重复。
- 工具与模型：`task.tool` 与 `task.model` 优先(角色与任务设置 `stages.<stage>.roles|tasks.<名称>`、评审类设置由组装任务时填入，01 篇 5.2)，其次是命令行覆盖，其次是各层合并后的 `stages.<stage>` 与 `capabilities` 映射(`project.yaml` 覆盖本机用户配置的 `agents` 段，环节没有工具时取 `defaultTool`，核心不给缺省工具；01 篇 5.1、5.3)；可执行文件路径取自本机用户配置。
- 工具自身的限制作为第一层照常启用，第二层由 `guards` 保证(第 3 节)。

**Claude Code**

| 统一字段 | 映射 |
|---|---|
| 无人值守 | `claude -p`，提示文件经标准输入传入 |
| 输出与会话记录 | `--output-format stream-json --verbose`：逐行事件即会话记录，最后一行 `result` 事件带最终文本、`session_id`、`usage`、`total_cost_usd` |
| `outputSchema` | `--json-schema <schema 文本>`，结果在 `result` 事件的 `structured_output` 中 |
| `access: read-only` | `--tools` 只列出读取、搜索与 Bash；`--allowedTools` 列出 `Read`、`Grep`、`Glob` 与每条允许命令(写成 `Bash(git log *)` 的形式)；`--permission-mode dontAsk`，未放行的调用一律拒绝 |
| `access: workspace-write` | 在上面基础上 `--tools` 加入 `Edit`、`Write`；`--permission-mode acceptEdits`；`--allowedTools` 只列出项目检查命令与启动脚本 |
| `allowedCommands` | 逐条转成 `Bash(<命令前缀> *)` 放入 `--allowedTools` |
| `limits.maxTurns` | `--max-turns` |
| `limits.maxCostUsd` | `--max-budget-usd` |
| `model` | `--model` |
| 交互启动 | `claude --session-id <核心生成的 UUID> --append-system-prompt-file <提示文件> ... "<首条输入>"`，不带 `-p` |
| 续接 | `claude --resume <会话 ID>` |

**Codex CLI**

| 统一字段 | 映射 |
|---|---|
| 无人值守 | `codex exec <提示文本>`；`-C <workdir>` 指定工作目录 |
| 输出与会话记录 | `--json` 输出 JSONL 事件：`thread.started`(带 `thread_id`)、`turn.started`、`turn.completed`(带用量)、`turn.failed`、`item.*`、`error`；`-o <文件>` 另存最终消息 |
| `outputSchema` | `--output-schema <schema 文件>`，最终消息即符合 schema 的 JSON |
| `access` | `--sandbox read-only` 或 `--sandbox workspace-write`；`--ask-for-approval never` |
| `allowedCommands` | 命令行上没有逐条放行命令的参数，由沙箱限制读写范围，命令范围由第二层检查兜底 |
| `limits.maxTurns` | 无原生上限，由核心计数(2.7) |
| `limits.maxCostUsd` | 无原生上限；返回值只有 token 用量，费用由核心按价格表估算 |
| `model` | `-m` |
| 交互启动 | `codex -C <workdir> --sandbox workspace-write --ask-for-approval on-request "<首条输入>"` |
| 续接 | 交互续接 `codex resume <会话 ID>`；无人值守续接 `codex exec resume <会话 ID>` |

**Antigravity CLI(agy)**

| 统一字段 | 映射 |
|---|---|
| 无人值守 | `agy -p <提示文本>`，工作目录即 `workdir`；提示只能作为 `-p` 的参数，首次调用的提示末尾附「shell 命令会被自动拒绝，只用内置工具」的说明 |
| 输出与会话记录 | `--output-format stream-json`：`init`(带 `conversation_id`)、`step_update`(`user_input`、`agent_response` 的文本片段与本步用量、`tool` 步骤的参数与输出、`system_message`、`finish`)，最后一行 `result` 带 `status`、`response`、`structured_output`、整个会话的累计 `usage`、`denied_actions`、`error`；没有费用字段 |
| `outputSchema` | `--json-schema <schema 文件>`，结果在 `result` 的 `structured_output` 中；接受 `$schema` 声明 |
| `access: read-only` | 缺省权限模式加 `--sandbox`，不跳过权限：文件写入与绝大部分 shell 命令被自动拒绝 |
| `access: workspace-write` | `--mode accept-edits`：文件编辑放行，shell 命令仍被自动拒绝 |
| `allowedCommands` | 命令行上没有逐条放行的参数：按 agy 自己的白名单(settings.json 的 `permissions.allow`)放行，`tightrein install` 补上只读命令；提示末尾列出其中已放行的只读命令与工具调用上限，要求先用 `git grep` 定位再读文件。只读任务在 `--sandbox` 中，终端写入被拦住；`--dangerously-skip-permissions` 放开全部命令，不使用 |
| `limits.maxTurns` | 无原生上限，由核心按完成的 `tool` 步骤计数(2.7) |
| `limits.maxCostUsd` | 无原生上限，费用由核心按各 `agent_response` 步骤的用量估算 |
| `model` | `--model`(`agy models` 列出，例如 `gemini-3.1-pro-high`、`claude-sonnet-4-6`) |
| 续接 | 无人值守续接(格式重试)为 `--conversation <会话 ID>` |
| 交互启动与收尾 | 不支持：不能预先指定会话 ID，会话记录在工具自己的数据目录中；启动交互会话时报配置错误，提示把 `stages.<环节>.session.tool` 设为 claude 或 codex |
| `web` | 没有开关。实测(1.2.14，只读模式)`search_web` 放行，读取网页(`read_url_content`)被拒绝并结束本轮；搜索结果只经模型回复体现，出处是 `vertexaisearch.cloud.google.com` 的跳转链接。`web` 为真时提示末尾另说明只依据搜索摘要作答；`web` 为假的只读任务同样可以搜索，不受核心控制 |

**三个工具在权限约束上的差异**：Claude Code 有工具与命令前缀白名单；Codex CLI 没有命令白名单，由沙箱限制读写；agy 两者都没有，只能做到「写入与命令全部拒绝」或「放开全部」，适配器取前者。被拒绝时 agy 立即结束本轮(`denied_actions` 记为一条 `error` 事件)，常没有输出，靠格式重试续接；只读角色查不了 git 历史，`fix-executor` 不能自己运行检查命令。第二层检查(第 3 节)对三个工具相同。

**从文本中提取 JSON(`output.py`)**：按顺序尝试整段文本、最后一个 ```` ```json ```` 代码块、最后一个括号配平的顶层对象；取到的第一个能解析的 JSON 交给 schema 校验。三种都失败按校验失败处理。

**replay**：不启动任何进程，见 2.12。命令行 `--runner replay` 时任务即使带了工具(角色或评审设置)也回放。

### 2.6 会话记录的统一事件格式

会话记录写入 `data/runs/<运行编号>/transcripts/<角色>-<对象编号>.jsonl`，一行一个事件，同一任务的重试追加在同一文件中，以 `attempt` 区分。

| 字段 | 说明 |
|---|---|
| `seq` | 文件内的序号 |
| `timestamp` | 事件时间(UTC)；工具不提供时取核心读到该行的时间 |
| `runId`、`role`、`subjectId`、`attempt` | 定位 |
| `tool`、`model`、`sessionId` | 工具、模型与会话 |
| `type` | `session-start`、`message`、`tool-call`、`tool-result`、`usage`、`error`、`result` |
| `actor` | `user`、`assistant`、`system` |
| `text` | 消息文本或错误说明 |
| `toolName`、`toolInput`、`toolCallId` | 工具调用 |
| `toolOutput`、`isError` | 工具结果；超过 16 KB 的截断，完整内容在原始输出中 |
| `usage` | 本事件携带的用量 |

| 统一类型 | Claude Code | Codex CLI | agy |
|---|---|---|---|
| `session-start` | `system` 类型的 `init` 事件 | `thread.started` | `init` |
| `message` | `assistant` 消息中的文本块 | `item.completed` 中的助手消息与推理条目 | `step_update` 的 `text_delta` |
| `tool-call` | `assistant` 消息中的 `tool_use` 块 | `item.started` 中的命令执行、文件修改、MCP 工具调用 | 完成或出错的 `tool` 步骤 |
| `tool-result` | `user` 消息中的 `tool_result` 块 | 对应条目的 `item.completed` | 同一 `tool` 步骤的输出或错误 |
| `usage` | `result` 事件的 `usage` | `turn.completed` 的用量 | 各 `agent_response` 步骤的 `usage`(`result` 中是会话累计，不用) |
| `error` | `system` 类型的 `api_retry` 事件；`result` 事件标记为错误时 | `turn.failed`、`error` | `result` 的 `status` 不为 `SUCCESS`；`denied_actions` |
| `result` | `result` | 最后一条助手消息 | `result` |

- 写入前经 `observability/redact.py` 脱敏。
- 工具的原始输出原样保存在 `raw/runner/<角色>-<对象编号>/stdout.jsonl`，只用于排障与适配器测试。
- 不认识的事件类型记为 `type: message`、`actor: system`，原文放在 `text` 中，不丢弃。

### 2.7 超时、轮数与预算

| 约束 | 做法 |
|---|---|
| 时间 | 核心用单调时钟计时。到达 `maxDurationMs` 时向进程组发送 SIGINT，等待 10 秒；仍未退出发送 SIGTERM，再等 5 秒后 SIGKILL。结果为 `limit-reached`，`errorType` 为 `timeout` |
| 轮数 | 工具支持的(Claude Code)以参数设置；不支持的，核心逐行读取事件时对 `tool-call` 计数，超过 `maxTurns` 按超时同样的方式终止，`errorType` 为 `turn-limit` |
| 单次费用 | 工具支持的(Claude Code)以参数设置；其余在读取到用量事件时累计估算，超过 `maxCostUsd` 时终止，`errorType` 为 `cost-limit` |
| 每日预算 | 启动前读取 `budget_usage` 中该环节当天的累计，已达 `stages.<stage>` 的上限时不启动，返回 `limit-reached`、`errorType` 为 `daily-budget`；运行结束后把本次费用累加进 `budget_usage`(11.3) |
| 费用估算 | 工具不返回费用时，按 `capabilities` 中该模型的每百万 token 价格计算，`costEstimated` 为真 |

- 格式重试的费用与耗时计入同一任务。
- 进程以独立进程组启动，终止时整组终止，agent 启动的子进程不会残留。

### 2.8 交互模式

交互模式只用于修复会话(5.4)。修复会话的 `access` 为 `read-only`：会话本身不改代码，只调用 `tightrein fix` 的子命令，代码改动由子命令内部以无人值守方式运行的 `fix-executor` 完成(07 篇 3.4)。

**启动(`run_interactive`)**

1. 检查每日预算；交互任务不设轮数与单次费用上限，也不因超时终止进程，因为用户在场并且随时可以结束。
2. 调用 `guards.before`。交互会话不做只读锁定，只读由工具自身的权限限制实现：会话中调用的 `fix apply` 会经 `fix-executor` 合法地改动同一个 worktree，文件系统层面的锁定会挡住这些改动。
3. 适配器生成交互启动命令，首条输入由 `fix` 给出(Issue 文件路径、发现报告路径)。Claude Code 由核心预先生成会话 ID；Codex CLI 在会话结束后由 `locate_session` 按工作目录与开始时间在工具本机保存的会话中识别。agy 不支持交互会话(2.5)，在登记会话之前即报配置错误。
4. 标准输入输出直接交给 agent 工具，核心等待进程结束。
5. 会话 ID 写入 `agent_sessions`，状态为 `open`。

**结束**

1. 适配器读取工具本机保存的该会话记录，转换为统一事件，写入会话记录文件。
2. 调用 `guards.after`，结果写入 `guardReport`。交互会话只检查 git 状态(3.6)与会话记录中对隐藏路径的读取(3.8 第 7 步)，不按改动范围判定：worktree 中的改动由会话内各次 `fix-executor` 运行的边界检查与 `fix done` 的改动哈希核对把关。
3. 从会话记录中汇总用量，累加进 `budget_usage`。
4. `outputSchema` 不为空时，执行一次收尾调用：以无人值守方式续接同一会话，要求按 schema 给出结构化结果(Claude Code 为 `claude -p --resume <会话 ID> --output-format json --json-schema ...`；Codex CLI 为 `codex exec resume <会话 ID>`)，后者的结果由核心提取与校验。修复会话的产出由 `fix done` 从 worktree 收集，不依赖这一步。
5. `agent_sessions` 状态改为 `closed`，记录结束时间。

**续接(`resume_interactive`)**

1. 从 `agent_sessions` 取该对象、该角色最近一次会话。
2. 工具支持按会话续接的，用续接命令启动，首条输入为用户这次给的要求；续接前后同样执行 `guards.before` 与 `guards.after`。
3. 找不到会话或工具无法按会话续接时返回 `resume-unsupported`，由 `fix` 新开会话，并在首条输入中提供已有的修复计划与当前 diff(15.10)。

### 2.9 读写的数据

| 数据 | 读 | 写 |
|---|---|---|
| `project.yaml`(`stages`、`capabilities`)、本机用户配置(`agents` 段、工具路径) | 是 | 否 |
| `budget_usage` | 启动前检查 | 结束后累加 |
| `agent_sessions` | 续接时 | 交互会话开始与结束 |
| `data/runs/<运行编号>/transcripts/<角色>-<对象编号>.jsonl` | 否 | 是 |
| `data/runs/<运行编号>/raw/runner/<角色>-<对象编号>/` | replay 时 | 提示文件、schema 文件、原始输出、`result.json` |
| `data/logs/events-<日期>.jsonl` | 否 | `invoke_agent` span |
| `contracts/schemas/` | 是 | 否 |

`runner` 不写交接文档，也不写任何业务表；结构化结果由调用方写入。

### 2.10 内部流程(`run`)

1. 按 `runner/runner-task.schema.json` 校验任务；解析工具与模型。
2. 检查每日预算，已超出时直接返回。
3. 开始 `invoke_agent` span。
4. 调用 `guards.before(task)`，得到检查上下文；失败(例如 worktree 中发现凭证文件)时不启动 agent，返回 `guard-violation`。
5. 在 `raw/runner/<角色>-<对象编号>/` 写入提示文件与 schema 文件；适配器生成 `Invocation`。
6. 启动进程，逐行读取输出写入原始输出文件，同时计时、计数、累计费用，到达上限时终止。
7. 进程结束后，适配器解析原始输出得到 `ParsedRun`，并转换出统一事件写入会话记录。
8. 调用 `guards.after(task, ctx)`。有违规时状态为 `guard-violation`，不做格式重试，直接进入第 11 步。
9. 取结构化结果：工具原生结果优先，否则从最终文本中提取；按 `outputSchema` 校验。
10. 校验不通过且这是第一次尝试：生成重试说明(原输出与逐条错误的 JSON 路径与原因)，工具支持按会话续接的续接同一会话，否则新开一次调用、在提示末尾附上重试说明；回到第 4 步。第二次仍不通过，状态为 `schema-invalid`。
11. 累加费用到 `budget_usage`；写 `result.json`；结束 span，记录用量、状态与 `errorType`；返回结果。

### 2.11 错误处理

| 失败 | `status` 与 `errorType` | 是否重试 | 上报 |
|---|---|---|---|
| 工具可执行文件不存在或未登录 | `failed`，`tool-unavailable` | 否 | 运行摘要中说明缺哪个工具、如何登录 |
| 进程非零退出且不是上限导致 | `failed`，`tool-error`，附标准错误输出的末尾 | 否 | span 状态与错误原文(脱敏后) |
| 工具报告的 API 重试 | 不改变状态 | 由工具自身重试 | 记为 `error` 事件 |
| 超时、轮数、单次费用 | `limit-reached`，`timeout`、`turn-limit`、`cost-limit` | 否 | 11.1 失败即停，写明卡点 |
| 每日预算 | `limit-reached`，`daily-budget` | 否 | 运行摘要中说明当天不再启动该环节 |
| 输出无法解析或不合 schema | 第一次重试；第二次 `schema-invalid` | 一次 | 两次的校验错误写入会话记录与事件 |
| 边界违规 | `guard-violation`，`errorType` 为第一项违规的类型 | 否 | 见 3.11 |
| 回放找不到录制 | `failed`，`replay-missing` | 否 | 测试直接失败 |
| 会话无法续接 | `failed`，`resume-unsupported` | 否 | 调用方新开会话 |

- 所有失败都返回 `RunnerResult`，不向上抛异常；只有任务本身不合 schema、配置缺项这类编程错误抛出 `RunnerConfigError`。
- 结果文件与会话记录在失败时同样写入，便于第 14 章的失败归类。

### 2.12 测试

**replay 适配器**

回放的输入是一个录制集目录，每条录制对应一次任务：

```
<录制集>/
  index.json                         (role, subjectId, attempt) 到录制目录的映射，以及录制时任务的哈希
  <角色>-<对象编号>.<attempt>/
    result.json                      RunnerResult 中除路径外的字段：output、usage、status、sessionId 等
    transcript.jsonl                 统一格式的会话记录
    changes.patch                    可选：workspace-write 任务在 worktree 中产生的改动
    stdout.jsonl                     可选：工具的原始输出，用于适配器解析测试
```

- 录制就是真实运行已经留下的 `raw/runner/` 与 `transcripts/` 下的文件，从历史运行复制到夹具目录即可，不需要单独的录制模式。命令行以 `--runner replay --replay-from <运行编号或目录>` 使用。
- 回放按 `(role, subjectId, attempt)` 取录制，不启动进程；有 `changes.patch` 的，用 `git apply` 应用到 `workdir`，使 `guards.after` 与 diff 规则在回放中照常生效。
- 回放同样经过 schema 校验、`guards` 与预算累计，只是跳过了进程；所以上层模块在回放下走的是与真实运行相同的代码路径。
- 任务哈希(提示、schema 与访问级别)与录制时不一致时报 `replay-task-changed`；评测提示改动的效果时应重新录制，不应回放旧结果。
- 录制中的结果可以手工编辑，用来构造边界情况：`schema-invalid` 后重试成功、`limit-reached`、违规改动等。

**其余测试**

| 对象 | 方式 |
|---|---|
| 各适配器的 `build` | 给定任务，断言生成的参数列表；覆盖只读、可写、带 schema、交互、续接 |
| 各适配器的 `parse` 与 `to_events` | `tests/unit/runner/fixtures/<工具>/` 中保存各工具真实输出的样本，断言解析结果与统一事件；工具升级后先更新样本 |
| `output.py` | 纯 JSON、代码块、夹杂说明文字、多个对象、无 JSON 的样例 |
| `process.py` | 用一个按脚本输出、按要求睡眠的假程序测试超时终止、进程组清理、逐行计数 |
| `limits.py` | 用固定 `Clock` 与临时数据库测试每日预算的拦截与累加、费用估算 |
| 上层模块 | `tests/replay/` 下给定输入交接文档与录制集，断言输出交接文档与数据库变化(15.8) |

## 3. guards

### 3.1 职责

在 agent 工具自身的限制之外，由核心再检查一遍边界(9.5、11.3)：

- 运行前：确认 agent 进程拿不到凭证；只读任务把 worktree 设为不可写；记录 git 状态与文件状态的快照。
- 运行后：恢复可写；比较 git 状态；检查改动范围、受保护文件与测试改动。
- 为 `fix` 提供 diff 规则检查(12.4)与改动量上限检查(5.5)。
- 进程异常退出后，恢复遗留的只读锁定。

`guards` 只判定与报告，不回滚 agent 的改动，也不修改 git 历史。

### 3.2 文件划分

```
guards/
  policy.py            GuardPolicy：由任务、project.yaml 与工作区布局推出本次的检查范围
  service.py           before、after、check_diff、recover
  credentials.py       build_env；worktree 中的凭证文件与 git 配置检查
  readonly.py          只读锁定与恢复，锁定标记文件
  git_state.py         git 状态快照与比较(经 vcs 只读查询)
  file_state.py        文件状态快照(大小、修改时间、内容哈希)与改动计算
  protected.py         受保护路径与受保护内容模式的匹配
  diff_rules.py        12.4 的 diff 规则与 5.5 的改动量上限
  report.py            GuardReport、Violation 与违规类型
```

### 3.3 接口

```python
def before(task: RunnerTask, *, clock: Clock) -> GuardContext:
    """运行前检查并建立快照。发现凭证等无法启动的情况时抛出 GuardBlocked，附 Violation 列表。"""

def after(task: RunnerTask, ctx: GuardContext, *, clock: Clock) -> GuardReport:
    """运行后检查。先恢复只读锁定，再比较快照。不抛异常，违规写在报告中。"""

def check_diff(worktree: Path, base_commit: str, *, issue_id: str,
               approved_protected_paths: list[str]) -> GuardReport:
    """对修复分支相对基准 commit 的全部改动执行 diff 规则与改动量上限检查。"""

def recover() -> list[Path]:
    """启动时调用：恢复上次异常退出遗留的只读锁定，返回已恢复的 worktree。"""

def build_env(base: Mapping[str, str], tool: str) -> dict[str, str]:
    """生成 agent 进程的环境变量：只保留白名单变量，并去掉一切疑似凭证的变量。"""

@dataclass(frozen=True)
class Violation:
    kind: ViolationKind        # 取值见 3.11
    path: str | None
    detail: str

@dataclass(frozen=True)
class GuardReport:
    ok: bool
    violations: list[Violation]
    changed_files: list[str]   # 本次运行中 agent 改动的文件(相对 workdir)
    lines_added: int
    lines_removed: int
    report_path: Path
```

### 3.4 运行前检查(`before`)

| 检查项 | 做法 | 不通过时 |
|---|---|---|
| 环境变量 | `build_env` 生成 agent 进程的环境(3.7) | 不会不通过；被去掉的变量名写入报告，值不记录 |
| worktree 中的凭证文件 | 在 `workdir` 中查找未被 git 跟踪、且匹配 `credentialFiles` 模式的文件(如 `.env`、`*.pem`、`*.pfx`、`secrets*.json`) | `GuardBlocked`，`credential-present` |
| worktree 的 git 配置 | 读取 worktree 生效的 `remote.*.url` 与 `credential.*`：远程地址中含用户名密码或 token、配置了凭证助手的 | `GuardBlocked`，`credential-present` |
| git 状态快照 | 见 3.6 | 读取失败时 `GuardBlocked`，`git-unreadable` |
| 文件状态快照 | 对 `workdir` 中已跟踪与未忽略的未跟踪文件记录大小、修改时间与内容哈希；对工作区中 agent 不可写的目录(3.5)同样记录 | — |
| 只读锁定 | `access` 为 `read-only` 时执行(3.5) | 锁定失败时先恢复已锁定的部分，再 `GuardBlocked` |

快照与报告写入 `data/runs/<运行编号>/raw/guards/<角色>-<对象编号>.json`。

### 3.5 只读锁定与恢复

**锁定**

1. 写锁定标记文件 `data/guards/readonly-<worktree 名>.json`：worktree 路径、运行编号、进程号、开始时间，以及原有权限与原本就不可写的文件清单。标记先于任何权限修改写入，保证中途崩溃也能恢复。
2. 对 worktree 中的所有目录与文件去掉写权限(相当于 `chmod -R a-w`)，不跟随符号链接。worktree 根目录的 `.git` 文件同样去掉写权限。
3. agent 进程的环境中设置 `GIT_OPTIONAL_LOCKS=0`，使 `git status` 等只读命令不尝试刷新索引，不会因为目录不可写而报错。

**不锁定的范围**：主仓库的 `.git/worktrees/<名称>/` 元数据目录不在 worktree 内，不做修改；对它的写入由 git 状态比较(3.6)兜底。

**恢复**

1. `after` 的第一步就是恢复：按标记文件中的记录逐一还原权限，原本就不可写的文件保持不可写。
2. 恢复完成后删除标记文件。
3. `runner` 在 `try/finally` 中调用 `after`，进程被终止、解析失败时同样恢复。
4. 核心进程自身崩溃时，下次启动由 `recover()` 读取遗留的标记文件：标记中的进程已不存在的，按记录恢复并写一条事件；进程仍在运行的不处理。

**锁定期间的检查**：恢复权限后比较文件状态快照，只读 worktree 中出现任何改动(包括 agent 自己把权限改回可写后写入)都判为 `readonly-modified`。

**工作区中 agent 不可写的目录**：`evals/`、`regressions/`、`project.yaml`、`normalize.yaml`、`suppressions.yaml`，以及 `~/Projects/tightrein/` 下的 `skills/`、`core/`(11.2、14.6)。这些路径不做权限锁定(用户与核心要写)，只在前后快照中比较，出现改动即判为 `forbidden-path-modified`。

### 3.6 git 状态的前后比较

快照由 `vcs` 的只读查询取得，内容与比较规则：

| 快照项 | 取得方式 | 运行后发生变化时 |
|---|---|---|
| `HEAD` 指向的 commit 与当前分支 | `git rev-parse HEAD`、`git symbolic-ref -q HEAD` | `git-commit-created` 或 `git-branch-switched` |
| 本地分支与标签 | `git for-each-ref refs/heads refs/tags` | `git-ref-changed` |
| 远程配置 | `git config --get-regexp '^remote\.'` | `git-remote-changed` |
| stash | `git stash list` | `git-stash-changed` |
| worktree 列表 | `git worktree list --porcelain` | `git-worktree-changed` |
| 是否处于合并、变基、拣选中 | worktree 的 git 目录下对应的状态文件是否存在 | `git-operation-started` |

- 比较的是快照前后的差异，不是绝对状态：修复 worktree 中上一轮留下的未提交改动不算违规。
- `release` 自己执行 git 写操作时不经过 `runner`，也就不经过这项比较。

### 3.7 凭证清理(`build_env`)

1. **白名单**：只从当前环境中保留 `PATH`、`HOME`、`USER`、`LOGNAME`、`SHELL`、`TMPDIR`、`TERM`、`LANG`、`LC_*`、标准代理变量(`http_proxy`、`https_proxy`、`all_proxy`、`no_proxy` 及其大写形式；需要经代理访问模型服务的网络中缺少它们会让请求被拒，代理地址带用户名或密码时按凭证去掉)，以及各工具读取自身配置目录所需的非凭证变量(由适配器声明)。
2. **黑名单兜底**：白名单保留下来的变量，名称含 `TOKEN`、`SECRET`、`PASSWORD`、`PASSWD`、`KEY`、`CREDENTIAL`、`AUTH`、`COOKIE`、`SESSION`，或值匹配 `observability/redact.py` 中凭证格式的，一律去掉。
3. **追加**：`GIT_TERMINAL_PROMPT=0`；只读任务追加 `GIT_OPTIONAL_LOCKS=0`。
4. agent 工具的登录依靠各工具自己的本机登录状态，不经环境变量传入模型服务的 API 密钥；`GH_TOKEN`、`GITHUB_TOKEN` 这类变量不会传给 agent。
5. 钥匙串中读取的测试账号密码只存在于需要登录的探针与验证进程中，从不进入 agent 进程(本目录 01 的 5.4)。

### 3.8 运行后检查(`after`)

按顺序执行，前一项不影响后一项的执行，所有违规一并报告：

1. 恢复只读锁定(3.5)。
2. 比较 git 状态(3.6)。
3. 计算改动：对比文件状态快照，得到本次新增、修改、删除的文件；改动行数用 `git diff --numstat` 对这些文件计算。
4. 改动范围：
   - `read-only`：任何改动都是 `readonly-modified`。
   - `workspace-write`：`workdir` 以外的改动(工作区中不可写的目录，见 3.5)是 `forbidden-path-modified`；`workdir` 内部的改动进入下一步。
5. 受保护文件：改动的文件匹配 `project.yaml` 的 `protectedPaths`，或改动的行匹配 `protectedPatterns`(例如 `[Authorize]`、`[AllowAnonymous]` 的增删)，且不在 `task.approvedProtectedPaths` 中的，为 `protected-modified`。
6. 测试与复现检查：改动的文件匹配 `testPaths`(如 `*.test.js`、`tests/` 目录)的，为 `test-modified`。复现检查与验证脚本在工作区中，已由第 4 步覆盖。
7. 隐藏路径的读取：会话记录中出现读取 `regressions/`、`evals/` 或匹配 `credentialFiles` 的文件的工具调用，为 `hidden-path-read`。凭证文件按文件名模式匹配，只有解析后的路径确实存在时才算读取(搜索文本或代码片段中形如 `it.key` 的 token 不指向文件)；只提交最终结果的工具(Claude Code 的 `StructuredOutput`)不检查。
8. 写报告，写 `gate` 事件：`decision` 为通过或违规，`reason` 为违规项摘要。

### 3.9 diff 规则(`check_diff`)

`fix` 在修复第 6 步写代码之后调用(design 5.2)，对象是修复分支相对基准 commit 的全部改动(已跟踪文件的 diff 加未跟踪的新文件)，不只是最后一次 agent 运行的改动：

| 规则 | 判定方式 | 结果 |
|---|---|---|
| 改动量上限 | 不计匹配 `testPaths` 的文件，改动文件数、增删行数之和与 `thresholds.change.maxFiles`、`thresholds.change.maxLines` 比较(单个 PR 上限，缺省 5 个文件、200 行；计划检查、`fix apply` 的程序检查与评测评分共用)；当前档的上限另由修复按 `thresholds.tiers` 检查(07 篇 4) | 超出为 `size-exceeded` |
| 受保护文件 | 同 3.8 第 5 步 | `protected-modified` |
| 测试改动 | 同 3.8 第 6 步 | `test-modified` |
| 新增跳过测试、关闭告警、抑制类型检查的标记 | 新增行匹配 `skipMarkers`，例如 `.skip(`、`xit(`、`[Fact(Skip`、`[Ignore]`、`@unittest.skip`、`pytest.mark.skip`、`#pragma warning disable`、`@ts-ignore` | `skip-marker-added` |
| 针对复现输入写死 | 读取该 Issue 复现检查中的请求参数与期望值，新增的字符串或数字字面量与其完全相同的，逐条列出 | `suspected-hardcode`，不直接判不通过，交 `fix-reviewer` 判断(12.2) |
| 工作区中不可写的目录 | 同 3.5 的快照比较 | `forbidden-path-modified` |

除 `suspected-hardcode` 外，任何一项命中都判为不通过。

### 3.10 读写的数据

| 数据 | 读 | 写 |
|---|---|---|
| `project.yaml` 的 `protectedPaths`、`protectedPatterns`、`testPaths`、`skipMarkers`、`credentialFiles`、`thresholds.fix` | 是 | 否 |
| `regressions/<Issue 编号>/` | `check_diff` 读取复现输入 | 否 |
| `data/guards/readonly-<worktree 名>.json` | `recover` 时 | 锁定时写，恢复后删除 |
| `data/runs/<运行编号>/raw/guards/<角色>-<对象编号>.json` | 否 | 快照与报告 |
| `data/logs/events-<日期>.jsonl` | 否 | `gate` 事件 |

### 3.11 错误处理与违规的处理

**违规类型**：`credential-present`、`git-unreadable`、`readonly-modified`、`forbidden-path-modified`、`git-commit-created`、`git-branch-switched`、`git-ref-changed`、`git-remote-changed`、`git-stash-changed`、`git-worktree-changed`、`git-operation-started`、`protected-modified`、`test-modified`、`skip-marker-added`、`size-exceeded`、`suspected-hardcode`、`hidden-path-read`。

| 情况 | 处理 |
|---|---|
| `before` 抛出 `GuardBlocked` | `runner` 不启动 agent，返回 `guard-violation`；运行摘要中说明需要用户处理什么(例如删除 worktree 中的 `.env`) |
| `after` 报告违规 | `runner` 返回 `guard-violation`，不做格式重试；调用方停止该对象的处理(11.1)，对象状态不前进 |
| 违规的改动 | 保留在 worktree 中不回滚，供用户检查；报告列出每个违规文件与原因 |
| agent 新建了提交或分支 | 不自动撤销(撤销需要 `reset` 等须用户主动提出的操作)；报告中给出新提交的 commit 与撤销方法，由用户决定 |
| `check_diff` 不通过 | 交回 `fix`，按 5.2 第 6 步的性质分类处理：改动量超限、受保护文件停下交用户，测试改动与跳过标记作为局部问题交回 `fix-executor` |
| 恢复权限失败 | 保留标记文件，写错误事件，运行摘要中列出 worktree 路径与恢复命令；下次启动由 `recover` 重试 |
| 快照读取失败 | 视为不通过，不在无法比较的情况下放行 |

`guards` 不重试任何检查；检查本身是确定的，重试不会得到不同结果。

### 3.12 测试

- 每个用例在临时目录中新建 git 仓库与 worktree，用普通函数模拟 agent 的行为，夹在 `before` 与 `after` 之间：
  - 只读锁定后写文件，断言写入失败；先改回权限再写，断言 `readonly-modified`。
  - 提交、建分支、改远程地址、`stash`，各自断言对应的违规类型。
  - 改受保护文件、增删 `[Authorize]`、改测试文件、新增跳过标记、超出改动量，各自断言；受保护文件在 `approvedProtectedPaths` 中时断言通过。
  - 修复 worktree 中事先存在未提交改动，agent 不再改动时断言通过。
  - 会话记录中出现读取 `regressions/`、`evals/` 的工具调用，断言 `hidden-path-read`。
- `build_env`：构造含 `GH_TOKEN`、`ANTHROPIC_API_KEY`、`MY_SERVICE_PASSWORD` 与普通变量的环境，断言只剩白名单变量。
- `recover`：写入一个进程号不存在的标记文件与已锁定的目录，断言恢复后可写且标记删除。
- 配合 replay：带 `changes.patch` 的录制在回放中触发同样的检查，上层模块的违规处理路径可以确定地测试。

## 4. vcs

### 4.1 职责

- git 与 gh 的只读查询，供各模块取状态、历史、blame、diff、部署记录与 PR 状态。
- git 与 gh 的写操作：不直接执行，而是生成「待确认操作」，用户确认后才执行(7.10)。
- 修复 worktree 的创建与清理；只读 worktree 的初始化与切换。
- 写操作的幂等(15.7)。

写操作只能由 `fix`(建分支与 worktree)与 `release` 调用(00 依赖规则 4)。只读 worktree 的切换不改动任何分支与用户的工作区，由编排层、`collect` 与 `triage` 调用；只读 worktree 的首次创建是写操作，由 `tightrein worktree init` 生成待确认操作。

### 4.2 文件划分

```
vcs/
  process.py           执行 git、gh：参数列表、固定环境变量、不继承终端、超时(超时终止整个进程组)、错误分类
  errors.py            VcsError 及各子类
  parse.py             porcelain 与 --json 输出的解析
  git_read.py          git 只读查询
  gh_read.py           gh 只读查询
  worktrees.py         worktree 列表；只读 worktree 的切换；修复 worktree 的创建与清理操作的构造
  operations.py        PendingOperation 数据类；各类写操作的构造函数
  executor.py          确认、前置条件复核、幂等、逐条执行、结果记录
```

### 4.3 只读查询

所有只读函数不需要确认，失败时抛出 `VcsError` 的子类。`repo` 是项目主仓库或某个 worktree 的路径。

| 函数 | 执行的命令 | 返回 | 使用者 |
|---|---|---|---|
| `status(repo) -> RepoStatus` | `git status --porcelain=v2 --branch -z` | 分支、上游、领先落后数、每个文件的暂存与工作区状态、是否干净 | `release`、`guards` |
| `head(repo) -> HeadState` | `git rev-parse HEAD`、`git symbolic-ref -q HEAD` | commit 与分支(游离时分支为空) | `guards` |
| `refs(repo) -> dict[str, str]` | `git for-each-ref --format=... refs/heads refs/tags` | 引用名到 commit | `guards` |
| `remotes(repo) -> dict[str, str]` | `git config --get-regexp '^remote\.'` | 远程配置 | `guards` |
| `log(repo, rev_range, paths=None, limit=None) -> list[Commit]` | `git log -z --format=<字段以 %x00 分隔>` | commit、作者、时间(UTC)、标题 | `collect`(静态巡检的增量范围)、`release`(推送说明中列出的提交、从历史推断约定) |
| `blame(repo, path, line_start, line_end, rev="HEAD") -> list[BlameLine]` | `git blame --porcelain -L <起>,<止> <rev> -- <path>` | 每行的 commit、作者、时间 | `collect`、`triage`(引入者) |
| `diff(repo, base, head=None, paths=None) -> Diff` | `git diff --numstat -z` 与 `git diff --unified=0` | 每个文件的增删行数与改动块 | `guards`、`release` |
| `diff_hash(repo, base) -> str` | 同上的完整 diff 加未跟踪文件内容 | 改动内容的哈希(提交的幂等键) | `release`、`executor` |
| `is_ancestor(repo, commit, of) -> bool` | `git merge-base --is-ancestor` | 是否已包含 | `release`、部署跟踪(`pipeline/common/deploys.py`) |
| `branches_containing(repo, commit, remote=True) -> list[str]` | `git branch -r --contains <commit>` | 包含该 commit 的远程分支 | `release`(7.8 生产发布记录) |
| `has_commit(repo, ref) -> bool` | `git rev-parse --verify --quiet <ref>^{commit}` | 本地是否已有该 commit(不访问远程) | 只读 worktree 的初始化与切换 |
| `fetch(repo, prune=False) -> None` | `git fetch origin`，超时取 `runtime.vcs.fetchTimeoutSeconds`(其余 git、gh 命令取 `runtime.vcs.timeoutSeconds`) | — | `release`(7.3)、只读 worktree 的初始化与切换 |
| `worktree_list(repo) -> list[WorktreeInfo]` | `git worktree list --porcelain` | 路径、HEAD、分支、是否锁定 | `worktrees.py`、`guards` |
| `required_checks(repo, slug, branch) -> bool` | `gh api repos/<仓库>/branches/<分支>` 与 `gh api repos/<仓库>/rules/branches/<分支>` | 主分支的分支保护或规则集是否要求必需检查 | `release`(自动合并由谁执行，07 篇 19.6) |
| `pr_view(repo, ref) -> PullState` | `gh pr view <编号或分支> --json number,url,state,mergeable,mergedAt,mergeCommit,closedAt,reviewDecision,headRefName,comments,reviews` | PR 状态、评审意见 | `release`(7.6) |
| `pr_for_branch(repo, branch) -> PullState \| None` | `gh pr list --head <分支> --state all --json number,url,state` | 该分支已有的 PR | `operations.py`(提 PR 的幂等)、`release` |
| `pr_for_commit(repo, commit) -> PullState \| None` | `gh pr list --search <commit> --state merged --json number,url,mergedAt` | 包含该提交的已合并 PR | `triage`(归因) |

部署记录不由 `vcs` 读取：经扩展点 `deploy-source` 按平台读取(10 篇 3.9)，包含合并提交的部署由 `pipeline/common/deploys.py` 判断(07 篇 19.7)。

### 4.4 待确认操作

每个写操作先构造成一个 `PendingOperation`，写入 `pending_operations` 表(列见 01 篇 4.2)，交用户确认，确认后才由 `executor` 执行。`vcs` 构造的操作 `executor` 都为 `vcs`。

```python
@dataclass(frozen=True)
class PendingOperation:
    id: str                          # OP-<四位序号>
    stage: Stage                     # 发起的模块
    subject_id: str                  # 对象，通常为 Issue 编号
    kind: OperationKind              # 取值见下表
    executor: OperationExecutor      # vcs 或 user
    commands: list[Step]             # 逐条命令，Step = (argv, cwd, 说明)
    description: OperationDescription  # 仓库与分支、按文件名排序的文件清单、是否影响远程、撤销方法、说明全文
    impact: str                      # 对工作区、历史与远程的影响
    reversible: bool
    preconditions: Preconditions     # 构造时的状态：HEAD、分支、改动内容的哈希、origin/main 的 commit、PR 是否存在等
    idempotency_key: str
    confirmations_required: int      # 1，或删除类操作的 2
    confirmations_given: int
    status: OperationStatus          # pending、confirmed、rejected、executed、failed、expired
    created_at: datetime
    decided_at: datetime | None
    executed_at: datetime | None
    result: dict | None
```

```python
def plan_create_fix_worktree(issue_id: str, branch: str, base: str = "origin/main") -> PendingOperation
def plan_init_readonly_worktree(repo: Path) -> PendingOperation
def plan_commit(issue_id: str, worktree: Path, message: str, files: list[str]) -> PendingOperation
def plan_merge_main(issue_id: str, worktree: Path) -> PendingOperation
def plan_commit_merge(issue_id: str, worktree: Path, conflict_files: list[str]) -> PendingOperation
def plan_abort_merge(issue_id: str, worktree: Path) -> PendingOperation
def plan_push(issue_id: str, worktree: Path, branch: str) -> PendingOperation
def plan_pull_request(issue_id: str, worktree: Path, branch: str, title: str, body: str,
                      base: str = "main") -> PendingOperation | None
def plan_cleanup(issue_id: str, worktree: Path, branch: str) -> PendingOperation
def plan_merge_pull_request(issue_id: str, number: int, slug: str, branch: str, head: str,
                            method: str) -> PendingOperation

def describe(op: PendingOperation) -> str                    # 渲染给用户看的说明
def confirm(op_id: str, *, confirmed_by: str, clock: Clock) -> PendingOperation
def reject(op_id: str, *, note: str | None, clock: Clock) -> PendingOperation
def execute(op_id: str, *, clock: Clock) -> OperationResult
def run_unattended(op_id: str, *, reason: str, clock: Clock) -> OperationResult  # 不等用户确认，见 4.5
```

**各类写操作**

| `kind` | 步骤 | 影响 | 能否撤销 | 幂等键 |
|---|---|---|---|---|
| `create-fix-worktree` | `git fetch origin`；`git worktree add -b <分支> <worktree 路径> origin/main`；按 `git.worktreeLinks` 建立链接 | 新建本地分支与 worktree 目录，不影响远程 | 能：`git worktree remove` 加 `git branch -d` | `worktree:<Issue 编号>` |
| `init-readonly-worktree` | 本地没有 `origin/main` 时先 `git fetch origin`；`git worktree add --detach <只读 worktree 路径> origin/main` | 新建一个游离 HEAD 的 worktree，不建分支；说明中写明此后每次运行会把它切换到目标 commit | 能：`git worktree remove` | `readonly-worktree` |
| `commit` | `git add -- <文件…>`；`git commit -F <提交信息文件>` | 修复分支新增一个提交，不影响远程 | 能，推送前由用户执行 `git reset --soft HEAD~1` 撤销该提交 | `commit:<Issue 编号>:<diff_hash>` |
| `merge-main` | `git fetch origin`；`git merge --no-ff --no-edit origin/main` | 修复分支新增合并提交；有冲突时停在合并中 | 能：合并中可以 `git merge --abort`，完成后推送前可以回到合并前的 commit | `merge:<分支>:<origin/main 的 commit>` |
| `commit-merge` | `git add -- <冲突文件…>`；`git commit --no-edit` | 用户解决冲突后完成合并提交 | 能，推送前可以回到合并前的 commit | `merge-commit:<分支>:<MERGE_HEAD>` |
| `abort-merge` | `git merge --abort` | 放弃进行中的合并，丢弃合并中的全部改动 | 不需要 | `abort:<分支>:<MERGE_HEAD>` |
| `push` | `git push -u origin <分支>` | 远程新建或更新该分支 | 远程上的提交不做强制撤销；可以关闭 PR 或推送新的修正提交 | `push:<分支>:<HEAD commit>` |
| `pull-request` | 没有已存在的 PR：`gh pr create --base main --head <分支> --title <标题> --body-file <描述文件>`；已存在打开的 PR：`gh pr edit <编号> --body-file <描述文件>` | GitHub 上新建或更新 PR | 能：关闭 PR | `pr:<分支>` |
| `cleanup` | `git worktree remove <worktree 路径>`；`git branch -d <分支>`；`git fetch --prune` | 删除 worktree 目录与本地分支 | `git branch -d` 只删除已合并的分支；删除后可按远程分支或合并提交重建 | `cleanup:<Issue 编号>` |
| `merge-pull-request` | `gh pr merge <编号> --squash\|--merge [--auto] --delete-branch --match-head-commit <头部 commit> --repo <owner/name>`；`--auto` 开启 GitHub 原生自动合并 | 主分支新增合并提交(开启自动合并时由 GitHub 在必需检查通过后合并)，删除远程修复分支；带 `--repo` 时 gh 不删除本地分支 | 不做强制撤销：需要时提撤销该合并的 PR(`release revert`)，远程分支可从 PR 页面恢复 | `merge-pr:<编号>:<头部 commit>`(开启 GitHub 自动合并时另加 `:auto`) |
| `pr-comment` | `gh pr comment <编号> --body-file <评论文件> --repo <owner/name>` | GitHub 上 PR 新增一条评论(AI 评审结论，只作说明) | 能：在 GitHub 上删除该评论 | 调用方给出，评审结论为 `review-comment:<PR 编号>:<评审轮次>` |
| `revert-pull-request` | 构造时 `git fetch origin`(只读)；`git worktree add -b <分支> <临时 worktree> <origin/main 的 commit>`；`git revert --no-edit [-m 1] <合并提交>`；`git push -u origin <分支>`；`gh pr create --base main --head <分支> --title <标题> --body-file <描述文件>` | 远程新建撤销分支与一个撤销 PR，主分支不变；PR 不合并，交用户决定 | 能：在 GitHub 上关闭撤销 PR，删除远程与本地分支、临时 worktree | `revert:<Issue 编号>:<合并提交>` |
| `github-issue` | GitHub Issue 镜像的一次对齐(06 篇 10.8)：`gh label create --force`；`gh issue create --body-file`(可带 `--parent`、`--blocked-by`)或 `gh issue edit --add-label/--remove-label`、`gh issue edit --parent/--add-blocked-by`、`gh issue comment --body-file`、`gh issue close --reason`、`gh issue reopen`，都带 `--repo` | GitHub 上新建或更新镜像 Issue | 能：在 GitHub 上关闭或编辑 Issue、删除评论 | `github:<Issue 编号>:<命令、正文与 Issue 更新时间的哈希>` |

- 提交信息、PR 描述写成文件放在 `data/runs/<运行编号>/raw/vcs/<操作编号>/`，以 `-F`、`--body-file` 传入，不经命令行转义。
- 操作说明(`describe`)包含：将执行的每条命令、作用的仓库与分支、按文件名排序的文件清单、对工作区与历史的影响、是否影响远程、能否撤销与撤销方法。
- `pull-request` 在构造时就查询 `pr_for_branch`，据此决定是创建还是更新，说明中写明是哪一种，用户确认的就是实际要执行的那一种；已有 PR 且描述没有变化时返回 `None`，不生成操作。
- `github-issue` 由 issue 模块经 `plan_github_issue` 构造(发起环节 `issue`)，没有前置状态；执行结果为每步的标准输出(`{"outputs": [...]}`)，由 issue 登记的后续处理记下 GitHub 编号、标签、已发评论与开关状态。关卡 `gates.mirror-writes: auto` 时镜像写入不构造待确认操作，直接经 `VcsProcess.gh` 执行；这个关卡只覆盖镜像，不影响其余写操作的确认。
- `merge-pull-request` 只由 `release track` 在 `gates.merge` 为 `auto` 且满足全部合并条件时构造并立即经 `run_unattended` 执行(07 篇 19.6)，没有前置状态；`--match-head-commit` 保证合并的就是判断时的头部 commit。
- `pr-comment` 由 release 在 PR 创建后构造(`release.reviewComment`)，`revert-pull-request` 由 `release revert` 构造(07 篇 19.12)；两者都没有前置状态。
- `fix-plan`、`local-migration` 不是 git 写操作，由发起模块自己构造，同样写入 `pending_operations`(07 篇 1.1)。

### 4.5 确认与执行

**确认(`confirm`)**

1. 确认只来自用户：终端中 `tightrein confirm <操作编号>`，或 agent 工具中由用户明确同意后 `loop` skill 调用同一命令。无人值守运行从不调用 `confirm`，待确认操作只进入运行摘要；项目声明不逐次确认的操作经下文的直接执行处理。
2. `confirmations_required` 为 2 的操作(删除 worktree 与分支，7.9)，第一次确认后状态仍为 `pending`，`confirmations_given` 记为 1 并提示风险，第二次确认才改为 `confirmed`。
3. 每次确认写 `user_action` 事件，`decided_at` 记录确认时间。
4. 确认只对这一个操作有效，不连带确认同一 Issue 的后续操作。

**拒绝(`reject`)**：`tightrein reject <操作编号> [--note <说明>]` 把操作改为 `rejected`，写 `user_action` 事件，按操作的 `stage` 调用发起模块登记的后续处理。

**执行(`execute`)**

1. 状态必须为 `confirmed`，否则拒绝。
2. **复核前置条件**：重新读取当前状态，与构造时记录的 `preconditions` 比较：分支、`HEAD`、改动内容的哈希(提交)、`origin/main` 的 commit(合并)、PR 是否已被他人创建(提 PR)。任何一项不同，操作改为 `expired`，不执行，提示重新生成；用户确认的内容必须就是实际执行的内容。
3. **幂等检查**：在 `idempotency_keys` 中查该键。已完成的，操作改为 `executed`，`result` 取上次的结果并注明为已执行过；处于进行中且已超时的，先核对实际状态(分支是否已存在、提交是否已在、PR 是否已建)再决定继续还是直接记为已执行。
4. 写入幂等键(进行中)，操作保持 `confirmed`。
5. 逐条执行步骤，每条写一个 `run_script` span，输出保存到 `raw/vcs/<操作编号>/`。任何一条失败即停止，不执行后续步骤。
6. 成功：幂等键标记完成并记录结果(新提交的 commit、PR 链接等)，操作改为 `executed`，记录 `executed_at`。失败：操作改为 `failed`，`result` 记录失败的步骤、退出码与错误输出，幂等键保持未完成。
7. 返回 `OperationResult`；Issue 回写与状态变化由发起模块登记的后续处理完成。

**直接执行(`run_unattended`)**：项目声明不逐次确认的操作与按规则自动决定的操作不等用户确认，构造后立即记为 `confirmed`(`confirmations_given` 取所需次数)、写一条 `gate` 事件(decision `auto-confirm`，reason 为理由)，再按上面的执行步骤执行；前置条件复核、幂等、逐步日志、失败分类与后续处理完全相同。只接受 `vcs/unattended.py` 的白名单：

- `gates.release-writes: auto`(缺省 `user`)：`create-fix-worktree`、`commit`、`merge-main`、`push`、`pull-request`、`pr-comment`、`revert-pull-request`；
- `gates.plan-confirm: auto` 且计划满足规则(07 篇 4.7)：`fix-plan`；
- `gates.merge: auto` 且满足合并条件(07 篇 19.6)：`merge-pull-request`。

是否放行由关卡表决定(`config/gates.py`，09 篇 3.8)；删除分支、worktree 或数据属于必须交用户的关卡 `delete`。

需要两次确认的操作(`cleanup`)、`commit-merge`、`abort-merge` 一律拒绝；`commit`、`merge-main`、`push` 作用于主分支或游离 HEAD 时拒绝(不直接向主分支提交或推送)。force push、`reset --hard`、`rebase`、`commit --amend` 本工具从不生成。GitHub 镜像的写入由关卡 `gates.mirror-writes` 单独决定。

**过期**：`executor` 为 `vcs` 的待确认操作超过 7 天未确认，下次运行时改为 `expired`，运行摘要中说明需要重新生成。

### 4.6 worktree 的创建、切换与清理

| 操作 | 位置 | 做法 |
|---|---|---|
| 创建修复 worktree | `workspaces/<项目>/worktrees/fix-<Issue 编号>/` | `issue approve` 经 `fix prepare` 构造 `create-fix-worktree`，用户确认后执行(5.3)。分支从最新的 `origin/main` 创建。幂等键为 Issue 编号：分支与 worktree 已存在且对应时复用，不重复创建 |
| 初始化只读 worktree | `workspaces/<项目>/worktrees/readonly/` | 首次使用前由 `tightrein worktree init` 构造 `init-readonly-worktree`，确认后执行；本地已有 `origin/main` 时不 fetch |
| 切换只读 worktree(`sync_readonly(commit)`) | 同上 | 目标 commit 在本地已存在时不 fetch；本地没有或没有给出 commit(取 `origin/main`)时先 `git fetch origin`，fetch 失败或超时以 `NetworkError` 结束并写明需要 fetch 的 commit。然后在只读 worktree 中执行 `git checkout --detach <commit>`。只移动这个 worktree 的游离 HEAD，不建分支、不动用户的工作区，不需要逐次确认；命令行入口为 `tightrein worktree sync [--commit <commit>]`。执行前检查它是干净的；不干净说明有东西写入了只读 worktree，停止并报告，不清理 |
| 清理修复 worktree | 修复 worktree 与分支 | 用户执行 `fix cleanup <Issue 编号>` 时由 `release` 的清理步骤构造 `cleanup`，二次确认后执行(7.9)。`git worktree remove` 不加强制参数：worktree 中还有未提交改动时 git 会拒绝，此时停下报告 |

- 两类 worktree 都在工作区的 `worktrees/` 下，路径由 `store/files/layout.py` 给出，不纳入本工具仓库的版本管理。
- `sync_readonly` 在 `guards` 的只读锁定之外执行：切换前确认没有锁定标记文件，存在时先 `recover`。
- 构建产物留在只读 worktree 的 `bin/`、`obj/`、`node_modules/` 中，它们被项目的 `.gitignore` 忽略，不影响干净检查(1.6.4)。

### 4.7 读写的数据

| 数据 | 读 | 写 |
|---|---|---|
| `project.yaml` 的 `project`(仓库路径、主分支)、`git`(分支、提交与 PR 的约定) | 是 | 否 |
| `pending_operations` | 是 | 构造、确认、执行时更新 |
| `idempotency_keys` | 执行前 | 执行中与完成后 |
| `data/runs/<运行编号>/raw/vcs/<操作编号>/` | 否 | 提交信息、PR 描述、命令输出 |
| `data/logs/events-<日期>.jsonl` | 否 | `run_script` span、`user_action` 事件 |

`deployments`、`pulls`、`issues` 等业务表由调用方根据 `vcs` 的返回值写入。

### 4.8 错误处理

| 错误类 | 触发 | 是否重试 | 上报与后续 |
|---|---|---|---|
| `GitNotFound`、`GhNotFound` | 可执行文件不存在 | 否 | 运行摘要说明缺少的程序 |
| `GhAuthError` | gh 未登录或权限不足 | 否 | 提示用户执行 `gh auth login` |
| `NetworkError` | fetch、push、gh 请求因网络失败或超时 | 只读查询重试 2 次，间隔 5 秒与 20 秒；写操作不重试 | 只读查询仍失败时该模块本次跳过依赖它的步骤；写操作失败后由用户决定重新执行 |
| `RefNotFound` | commit、分支不存在 | 否 | 调用方处理，例如部署跟踪中部署的 commit 尚未 fetch 时先 fetch 再查一次 |
| `PushRejected` | 远程拒绝(通常是远程有新提交) | 否，不使用 `--force` | 说明原因，回到 7.3 同步主干 |
| `MergeConflict` | 合并 `origin/main` 出现冲突 | 否 | 操作记为 `failed`，返回冲突文件清单(`git diff --name-only --diff-filter=U`)，worktree 停在合并中，交用户逐个文件处理；需要放弃时构造 `abort-merge` |
| `WorktreeDirty` | 清理或切换只读 worktree 时有未提交改动 | 否 | 停下报告，不强制删除 |
| `GitCommandError` | 其他非零退出 | 否 | 附命令、退出码、错误输出末尾(脱敏后) |

- 错误分类依据退出码与 porcelain 状态，例如合并冲突由 `status` 中的未合并条目判定，不解析错误消息文本做分支判断；推送被拒绝由 `git push --porcelain` 输出中各引用的状态标记判定。
- **换路重试**：访问远程的 git 命令(`fetch`、`push`、`pull`、`ls-remote`)与 gh 命令超时，或非零退出且错误输出匹配 `runtime.network.errorPatterns`(超时、连接重置或拒绝、TLS 握手失败、无法解析主机、代理连接失败等，正则不区分大小写)时，`process.py` 立即换另一条路重试一次：按 `runtime.network.rerouteHost`(`github.com`)判断当前路线，`network.noProxy` 使其直连的改为经代理，经代理的改为直连(`config/network.py`)；环境中没有代理地址时不换。认证失败、推送被拒等不匹配模式，不换路。每次换路记入 `VcsProcess.reroutes` 并写 `gate` 事件(decision `network-reroute`)，运行摘要「网络换路」列出；之后仍失败按上表分类，只读查询的间隔重试照旧。第三方 skill 的源码归档下载(没有得到响应且原因匹配模式)按同一规则换路一次，换路写到命令的错误输出。这是唯一一处按错误输出文本判断的地方：网络类错误没有可靠的退出码区分。
- 所有 `VcsError` 都保留原始命令、退出码与错误输出，并以原异常为 `__cause__` 向上抛出。

### 4.9 测试

| 对象 | 方式 |
|---|---|
| git 只读查询 | 临时目录中 `git init` 一个仓库，另建一个裸仓库作为 `origin`，构造提交、分支、合并、冲突，断言解析结果 |
| gh 只读查询 | `process.py` 接受 gh 可执行文件路径；测试中换成一个按参数返回预置 JSON 的假程序，覆盖 PR 的各状态与分支保护、规则集的必需检查 |
| 待确认操作 | 构造 → 确认 → 执行的完整路径；构造 → 拒绝；未确认执行被拒绝；删除类操作只确认一次时不执行 |
| 前置条件 | 构造后修改文件或移动 `origin/main`，断言执行时变为 `expired` 且没有执行任何命令 |
| 幂等 | 同一操作执行两次，第二次不执行任何命令，直接记为 `executed` 并返回上次的结果；模拟执行中崩溃(幂等键为进行中)后重跑，断言按实际状态继续或直接记为已执行 |
| 错误分类 | 远程拒绝推送、合并冲突、worktree 不干净，各自断言错误类型与状态 |
| 只读 worktree 切换 | 切换到指定 commit 后 HEAD 正确；有未提交改动时拒绝切换 |

本篇用到的基础层定义(编号、枚举、表、路径、配置)统一见 01-foundation.md。
