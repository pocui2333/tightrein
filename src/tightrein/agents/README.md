# 调用 AI(agents)

所有模型调用只走一个入口 `call.call(params, context)`，参数是一张固定的表(`params.py:CallParams`)，结果是固定的几种状态(`result.py:CallResult`)。各工具的命令行写法、已验证版本、不支持的项写在 `tools/<工具>.md`，不支持的由程序兜底。子进程、超时、杀进程组由 `protocol/process.py` 负责；失败后重试、续接、换备用还是停，只由 `protocol/limits.py:decide` 决定。

```
agents/
  README.md        本文：统一参数、结果状态、兜底规则
  call.py          统一入口：AgentContext、call、params_for
  params.py        统一参数
  result.py        统一结果与状态
  tools/
    __init__.py    适配器协议(build、parse_line、parse)与共用的解析
    claude.py  claude.md
    agy.py     agy.md
    codex.py   codex.md
    replay.py      回放录制(整体测试用)
```

## 统一参数

`params_for(调用点, …)` 按调用点从 settings 取模型、备用模型、权限与上限(控制字段按「小步骤 → 模块 → 阶段 → `*`」继承)，调用方只给本次的输入。

| 类 | 参数 | 来源 |
|---|---|---|
| 身份 | `point`(调用点 = 控制键)、`run`、`subject`、`round` | 调用方 |
| 模型 | `model`(工具、模型、推理强度、价格)、`fallback`(被拒绝或不可用时换用) | `controls.<键>.model`、`modelWhen`、`fallback` → `models` 别名 |
| 输入 | `prompt`、`schema`、`workdir`、`read_paths`(额外可读目录) | 调用方(`prompts/build.py` 拼提示) |
| 权限 | `access`(read、write)、`allowed_commands`、`network` | `controls.<键>.access`、`network`；命令 = `boundaries.readCommands` 加调用方给的 |
| 上限 | `limits`：`timeout_s`、`idle_s`、`turns`、`input_tokens`、`output_tokens` | `controls.<键>.timeout`、`idle`、`turns`、`inputTokens`、`outputTokens` |
| 会话 | `resume_session`(续接)、`finalize`(结束后补要结构化结果) | 调用方 |
| 环境 | `language`、`prompt_hash` | 项目配置、提示哈希 |

环境变量不在参数里：一律经 `protocol/security.py:child_env` 按白名单给，`ANTHROPIC_API_KEY` 无论如何去掉(有它时 claude 改走 API 按量付费)；各工具必需的另几项写在 `tools/<工具>.md`。

费用不设上限：两个工具都按订阅计费，额度与每个 Issue 的用量上限见 `protocol/resources.md`。

## 统一结果

`CallResult`：状态、工具、模型、结构化结果 `output`、最终文本、错误说明、`tokens`(输入、输出、缓存读取、缓存写入)、费用(工具不报时按价格估算，`cost_estimated` 为真)、耗时、轮数、调用次数与重试次数、会话编号、额度信号、越界项、`raw_path`。

所有失败都以结果返回，不向上抛；只有编程或配置错误(未知工具、没有录制集、联网任务交给 codex、schema 有循环引用)才抛 `CallConfigError`。

| 状态 | 含义 | 怎样识别 | 处理(`protocol/limits.py`) |
|---|---|---|---|
| ok | 成功，结构化结果合 schema | | |
| timeout | 超时或流式输出太久没动静 | `process` 的 timeout、idle | 停 |
| turn_limit | 轮数到限 | claude `error_max_turns`；其他工具由程序数 tool-call | 停 |
| budget_limit | 用量到限 | claude `error_max_budget_usd`；单次输出超上限；对象的用量将超过每个 Issue 的上限 | 停 |
| refused | 被拒绝(安全分类等) | 各工具的拒绝报错、claude `stop_reason: refusal` | 换备用模型 1 次，再拒绝就停 |
| schema_invalid | 格式不符 | 结构化结果提取不到或不合 schema | 续接同一会话带原因重试 1 次 |
| transient | 临时错误(限流、5xx、网络) | 工具失败且错误或 stderr 含 `limits.transientPatterns` 中的片段 | 退避重试同一次调用，最多 2 次 |
| auth_failed | 认证失败 | 各工具的登录、令牌报错 | 停，并对该工具立即依赖熔断 |
| quota_exhausted | 订阅额度用完 | claude `rate_limit_event` 为 rejected、`You've hit your … limit · resets …`；agy `Individual quota reached … Resets in …`；codex `hit your usage limit` | 全部停下，不换工具；到重置时间前该工具的调用不启动 |
| unavailable | 工具不存在、无法启动或依赖熔断中且无备用 | 可执行文件找不到、`start_error` | 换备用模型 1 次 |
| boundary | 越界 | 启动前工作目录有凭据(不启动)、只读步骤工作目录有变化、可写步骤主仓库或 worktree 之外不可写的路径有变化、执行了不在白名单的命令、工具调用读了隐藏目录或凭据文件 | 停，不做格式重试 |
| failed | 其余工具失败 | | 停 |

## 一次调用的流程(call.py)

1. 写 started 标记(`<序号>-<调用点>-started.json`，`endedAt` 为空即正在调用；watch 据此显示已跑多久，模型重试或等待时不像卡死)；
2. 依赖熔断：主模型的工具熔断中就改走备用模型(备用模型的工具未熔断时)；
3. 启动前检查：该工具额度用完且未到重置时间、该对象已到每个 Issue 的用量上限、熔断且无备用 → 直接返回，不启动；工作目录中有未跟踪(含被忽略)的凭据文件或仓库本地配置带凭据(`security.credentials_present`) → boundary，不启动；
4. 全局同时进行的模型调用数受 `resources.concurrency.modelCalls` 限制(`Slots`)；
5. 调用前记边界快照：只读步骤记工作目录，可写步骤记主仓库(工作目录之外)的 git 快照，另记工作区配置与 tightrein 自身代码的文件快照(`AgentContext.tool`)；不是 git 仓库的不做 git 比对；
6. 适配器生成命令，经 `protocol/process.py` 运行；逐行回调：数 tool-call、累计 token、检查命令、检查工具调用是否读了隐藏目录或凭据文件(`boundaries.hidden_reads`)，超了或越界即返回原因杀进程；
7. 每次工具调用后比对快照，有越界即停；
8. 解析输出，提取结构化结果并按 schema 校验；
9. 不成功时按 `decide` 处理(上表)；
10. 无论怎样结束都在 finally 中把用量累计到对象、额度信号写进 Quota；中断等异常时也比对快照并记一条；
11. 失败或调试(`AgentContext.debug`)时保存 `prompt.md` 与 `raw.jsonl`(写入前脱敏)，记一条事件。

## 兜底规则

工具原生不支持的，由程序兜底，不能省：

| 项 | claude | agy | codex |
|---|---|---|---|
| 轮数上限 | 原生 `--max-turns` | 程序数完成的工具步骤，超了杀进程 | 程序数 `item.started` 的工具调用，超了杀进程 |
| 单次输出上限 | 环境变量 `CLAUDE_CODE_MAX_OUTPUT_TOKENS` | 程序按每步用量检查 | 程序按每轮用量检查 |
| 每个 Issue 的用量上限 | 程序逐行累计(按消息编号去重)，将超时杀进程 | 同左 | 同左 |
| 命令白名单 | 原生 `--allowedTools Bash(<前缀> *)` | 原生 `permissions.allow`(admin install 补齐只读命令) | 程序逐条检查(`boundaries.command_allowed`)，不在白名单即越界并杀进程 |
| 只读 | `--permission-mode dontAsk` 加快照比对 | `--sandbox` 加快照比对 | `--sandbox read-only` 加快照比对 |
| 额度信号 | `rate_limit_event`；rejected 时立即停下 | 只有报错 | 只有报错 |
| 费用 | 工具报 `total_cost_usd` | 按价格估算 | 按价格估算 |
| 不弹浏览器登录 | 不需要 | PATH 前置只 `exit 1` 的 open、xdg-open | 不需要 |

其他：

- **token 口径统一**：`input` = 未缓存 + 缓存写入 + 缓存读取，缓存读取另记在 `cache_read`(每个 Issue 的用量上限按 1/10 计，必须单列)。claude 三项相加；agy 的 `input_tokens` 不含缓存读取，要加上；codex 的 `input_tokens` 已含缓存读取，不再相加。
- **费用估算**：工具不报费用时按别名中的价格(每百万 token 美元)估算：未缓存部分按输入价、缓存读写按各自价格(没有单独价格时按输入价)、输出按输出价；没有价格时为空。只用于对比优化前后，不作预算。
- **结构化结果的提取顺序**：工具原生 structured 优先 → 整段文本 → 最后一个 ` ```json ` 代码块 → 最后一个括号配平的顶层对象(字符串中的括号与转义不计)；不是对象时报错。
- **格式重试**：续接同一会话(用得上缓存)，说明中逐条列出错误的 JSON 路径与原因，附上一次原输出(截到 8000 字；工具给了 structured 时取 structured，不显示为空的最终文本)；拿不到会话编号时新开一次，把说明附在提示末尾。
- **临时错误重试**：重试的是同一次调用(不续接)，不算格式重试；失败那次的用量照常累计；等待时间取 `protocol/limits.py:backoff_s`(全抖动，retry-after 照办，529 时上限 60s)。
- **补要结构化结果**(`finalize`)：先不带 schema 把事做完，再以无人值守方式续接同一会话、只要求按 schema 输出一个 JSON；claude 为 `-p --resume <id> --output-format json --json-schema`，codex 为 `exec … --sandbox read-only --output-schema … resume <id>`，agy 为 `--conversation <id> --json-schema <文件>`。
- **可执行文件**：settings `tools.<工具>.path` 配了就用配的(文件不存在即不可用，不悄悄改用 PATH)，否则按子进程的 PATH 查找。
- **raw**：每次工具调用前一行 `{"tightrein": {attempt, tool, model, exitCode, stoppedBy, stderr}}`，其后是工具原样输出；JSON 行脱敏后单个字符串超过 16384 字截断，不认识的行脱敏后原样保留，不丢弃。
- **回放**(`tools/replay.py`)：`AgentContext.replay` 给出时全部调用读录制，按(调用点、对象、轮次、第几次调用)取录制时的工具原始输出，交给那个工具的适配器解析；提示、schema、访问级别的哈希不符即拒绝回放；录制的 `changes.patch` 第一次调用时 `git apply` 到工作目录，边界检查照常生效。

## 设计依据

- 旧 `runner/` 把被拒绝、认证失败、临时错误都归为 failed/tool-error，上层无法区分「再试会好」与「再试也没用」；新状态表与 limits.md 的「按失败类型处理」一一对应。
- 旧代码没有读订阅额度信号：额度用完时还在重试，或换到另一个工具把它的额度也用完。现在读 `rate_limit_event` 与额度报错，到重置时间前不启动。
- 0018 号 Issue 一次修复用了 1450 万 token：每个 Issue 2M 的上限按逐行累计实时拦住，不等调用结束。
- agy 无人值守时没有原生轮数上限，曾同一文件读 5 次、单次 340 万 token 超时：程序数工具步骤，并在提示里写明上限让它留出输出结论的余量(见 `tools/agy.md`)。
