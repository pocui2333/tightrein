# agy(Antigravity CLI)

适配器：`agy.py`。已验证版本：1.2.17 实测(命令白名单、只读模式的联网行为)；本机 1.3.0 的 `--help` 核对参数。Gemini CLI 已于 2026-06-18 停用，agy 是继任者。

## 统一参数对照

| 统一参数 | 命令行 | 说明 |
|---|---|---|
| 无人值守 | `--output-format stream-json -p <提示>` | 事件：init、step_update、result |
| 提示 | `-p` 的参数 | 只能这样传，不读标准输入 |
| 模型、推理强度 | `--model <模型>`、`--effort <强度>` | |
| schema | `--json-schema <schema 文件>` | 写到本次调用的临时目录 |
| 工作目录 | 子进程的 cwd | |
| 额外可读目录 | 每个目录一个 `--add-dir <目录>` | |
| 只读 | 缺省权限模式加 `--sandbox` | 终端写入被拦住 |
| 可写 | `--mode accept-edits` | 两种都**不用** `--dangerously-skip-permissions` |
| 允许的命令 | 无参数；按 `~/.gemini/antigravity-cli/settings.json` 的 `permissions.allow` | `command(git grep)` 放行以它开头的命令；其余需要确认的动作被自动拒绝，拒绝后本轮随即结束 |
| 网络 | 无开关 | 只读模式下 `search_web` 放行，`read_url_content` 被拒并结束本轮(实测) |
| 轮数、输出上限 | 无 | 程序兜底，见下 |
| 续接(格式重试) | `--conversation <会话> -p <说明>` | |
| 补要结构化结果 | `--conversation <会话> --json-schema <文件> -p <说明>` | |

## 提示末尾的工具说明(第一次调用)

- 列出本次放行的命令(`allowed_commands`)与 agy 白名单的交集，要求先 `git grep -n` 定位、再打开命中的文件，读过的文件不重复打开；一条都没有时说明 shell 命令会被自动拒绝，只用内置工具；
- **每次只执行一条命令**，不用 `&&`、`||`、`;`、`|` 连接：agy 按整条命令匹配白名单，其中一条不在白名单整条被拒、整轮作废(`#7`)；
- 写明工具调用上限，让 agy 自己留出输出结论的余量；
- 联网任务另说明只依据搜索摘要作答并注明来源。

原提示要求不调用 shell 时，agy 勘察只能逐个打开文件，同一文件读 5 次、单次 340 万 token 超时(`#5`)。`tightrein admin install` 为 agy 的白名单补上只读命令表(`boundaries.readCommands`)，只补缺的并记下，卸载只删这些。

## 输出

- `init`：`conversation_id`；
- `step_update`：`tool` 步骤在 DONE 或 ERROR 时算一次工具调用；`agent_response` 等步骤完成时带本步 `usage`；
- `result`：`status`(SUCCESS、ERROR)、`response`、`structured_output`、`error`、`denied_actions`(被自动拒绝的动作，记进错误说明)、`usage`；
- `--output-format json` 时整个输出是一个跨多行的对象，按一个对象解析，不逐行误判。

**续接后 `result.usage` 是整个会话的累计**：按各步骤的 usage 求和，只计本次调用；只有 json 格式的单个对象才用 `result.usage`。

token：`input_tokens` 不含缓存读取，统一的输入 = `input_tokens` + `cache_read_tokens`；不报缓存写入，不报费用(按价格估算)。agy 每次请求自带约 25k token 的系统开销，只给它范围窄、轮数少的任务。

## 失败的识别

| 状态 | 依据 |
|---|---|
| quota_exhausted | `Individual quota reached … Resets in 143h57m55s`(重置在 5 小时之后的记为每周额度)；每周额度被锁时可能要等五六天，期间用到 agy 的步骤都不跑 |
| auth_failed | `not logged in`、`UNAUTHENTICATED`、`authentication failed`、`login required` 等 |
| refused | `blocked due to safety`、`safety filter`、`finish_reason SAFETY`、`prohibited content` |

## 不支持的项与程序兜底

- 没有轮数上限：程序数完成的工具步骤，超过 `turns` 杀进程(turn_limit)；
- 没有输出上限：程序按每步用量检查单次输出；每个 Issue 的用量逐行累计；
- 没有联网开关：不能关掉 `search_web`；需要严格不联网的调用点不要路由到 agy；
- 不能预先指定会话编号、会话记录不在可读位置：不支持交互会话。

## 不弹浏览器登录(`#9`)

PATH 最前面放一个只 `exit 1` 的 `open`、`xdg-open`(系统临时目录的 `tightrein-no-browser/`)：中断后 agy 读凭据失败时直接失败，不弹出浏览器登录页。

## 必需的环境变量

白名单之外不需要其他变量(凭据在 `HOME` 下)。超额付费开关(AI Credit Overages)保持关闭，只用订阅额度。
