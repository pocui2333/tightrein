# codex(Codex CLI)

适配器：`codex.py`。已验证版本：本机 codex-cli 0.153.4 的 `codex exec --help` 核对参数；输出事件格式按旧适配器的录制(`tests/agents/tools/fixtures/codex/`)。

## 统一参数对照

| 统一参数 | 命令行 | 说明 |
|---|---|---|
| 无人值守 | `exec --json` | JSONL 事件 |
| 提示 | 最后一个位置参数 | |
| 工作目录 | `-C <目录>` | |
| 模型、推理强度 | `-m <模型>`、`-c model_reasoning_effort=<强度>` | |
| schema | `--output-schema <schema 文件>` | 最终消息即符合 schema 的 JSON |
| 最终消息另存 | `-o <文件>` | 写到本次调用的临时目录 |
| 额外可读目录 | 每个目录一个 `--add-dir <目录>` | |
| 只读 | `--sandbox read-only` | |
| 可写 | `--sandbox workspace-write` | |
| 不等确认 | `-c approval_policy=never` | 0.153.4 的 `exec --help` 不列 `--ask-for-approval`，改用配置项覆盖 |
| 允许的命令 | 无参数 | 程序兜底，见下 |
| 网络 | 无开关 | 联网任务交给 codex 视为配置错误(`CallConfigError`) |
| 轮数、输出上限 | 无 | 程序兜底 |
| 续接(格式重试) | `exec … resume <会话> <说明>` | |
| 补要结构化结果 | `exec … --sandbox read-only --output-schema <文件> … resume <会话> <说明>` | |

## 输出

- `thread.started`：会话编号；
- `item.started` 中 `command_execution`、`file_change`、`mcp_tool_call`、`web_search` 各算一次工具调用；命令写成 `bash -lc '<命令>'`，程序取出里面的命令再检查；
- `item.completed` 的 `agent_message`：**没有 result 事件，以最后一条 agent_message 作结果**；
- `turn.completed`：本轮用量；`turn.failed`、`error`：错误。命令自己退出码非 0 只是该命令失败，不算工具失败。

token：`input_tokens` 已含 `cached_input_tokens`，不再相加；缓存读取另记；不报缓存写入，不报费用(按价格估算)。

## 失败的识别

| 状态 | 依据 |
|---|---|
| quota_exhausted | `You've hit your usage limit … try again in 2 days 3 hours`(或 `try again at <时刻>`)；认不出重置时间时按 `resources.quota.unknownResetWait` |
| auth_failed | `Not logged in`、`401 Unauthorized`、`refresh token`、`codex login` |
| refused | `content_filter`、`flagged as potentially violating`、`violates our usage policy` |

## 不支持的项与程序兜底

- 没有逐条放行命令的参数：命令范围靠沙箱，加上程序逐条按白名单(`boundaries.command_allowed`)检查，不在白名单即越界并杀进程；
- 没有轮数上限：程序数工具调用，超过 `turns` 杀进程；
- 没有输出上限：程序按每轮用量检查；每个 Issue 的用量逐行累计；
- 没有联网开关：联网任务不交给 codex。

## 必需的环境变量

白名单之外：`CODEX_HOME`(用户改过配置目录时)。
