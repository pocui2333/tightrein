# 资源

原并发、预算两项合并。程序在同名的 `resources.py`：`Quota`(订阅额度)、`IssueBudget`(每个 Issue 的用量上限)、`Slots`(并发)。

## 并发与限流

**在哪配置**：`resources.concurrency`。`Slots(settings).hold("<项>")` 为每项一个信号量；`platformRps` 是速率，不是并发数，不建信号量。

| 项 | 键 | 缺省 | 出处 |
|---|---|---|---|
| 同时跑的 agent 会话(同时处理的 Issue) | sessions | 1(只能是 1–3) | 两个工具都按订阅计费，缺省只占一个会话的额度，给用户自己留用；调大后实施阶段同时推进这么多个会改到的文件互不重叠的 Issue，各在自己的线程、worktree 与分支里(protocol/schedule `_issues_to_implement`)；为什么最多 3 个见下「并行实施时补人」 |
| 空位补人(并行实施时) | refillWhenFree、refillAfter、refillMax(都在 resources 下，不在 concurrency 里) | 见下 | 见下「并行实施时补人」 |
| 全局同时进行的模型调用 | modelCalls | 4 | 推断；`agents/call.py` 每次调用持有一个 |
| 采集时并行的来源 | collectSources | 4 | 推断：大多是查询外部平台，不占本机资源 |
| 测试并行 | tests | 按 CPU 核数，最多 8 | pytest-xdist |
| 每个外部平台的接口 | platformRps | 每秒 5 个请求 | 推断；对方返回 429 时按 limits 的退避 |

### 并行实施时补人

`sessions` 固定在 1–3，读取配置时校验，写 0、4 或不是整数的直接报错，不悄悄截到范围内(`settings/load.py` 的 `SESSIONS`)。同时做得再多，多出来的主要是改到共用文件的合并冲突(发布时退回编码重做)和停在关卡等人的空位，实际运行中 3 个最划算。

`sessions` 大于 1 时，实施阶段先开一批(至多 `sessions` 个会改到的文件互不重叠的 Issue)；同批有先做完的、还有没做完的，就从队列里补会改到的文件不与正在做的重叠的进来。会改到的文件取根因所在的文件、评估预估要改的文件、方案的文件清单与各步的文件、修复 worktree 中已经改动的文件之和(只认列出来的，不按源文件推测测试文件)；还在等前置 Issue 合入的不挑(`assess/issue/blockers.py`)。同批全做完时不凑 refillWhenFree、不等 refillAfter，直接按 `sessions` 个空位从队列里再开：先前因为会改到同一文件而没挑的这时就能做，不用等下一次运行(曾有两个 Issue 都改同一个文件，一次运行只做了一个就结束实施)。队列里挑不出能做的、或一轮开始的到了 refillMax，就结束实施进入发布：做完的 Issue 要等实施阶段结束才合并。

| 键 | 缺省 | 含义 |
|---|---|---|
| `resources.refillWhenFree` | null，即 max(1, sessions ÷ 2 取整)：sessions 在 1–3 内都是 1，空一个就补 | 同时空出这么多个位置就一起补满所有空位；设了就要在 1 到 sessions 之间 |
| `resources.refillAfter` | 3m | 兜底：空位不到 refillWhenFree 个时，单个空位等过这个时间也补上。0 为一空就补 |
| `resources.refillMax` | null，即 sessions 的 2 倍 | 一轮之内开始的 Issue(含第一批)到这个数就不再补，等手上的做完进入发布；设了就不能小于 sessions(第一批就要开 sessions 个) |

取值的道理：refillWhenFree 太小(1)接近完全滚动，空一个补一个，这一轮迟迟结束不了，做完的修复一直等着合并；太大(等于 sessions)接近一批一批地做，先做完的位置空等最慢的那个；取一半是两者的折中。sessions 不超过 3 时一半取整都是 1，这一轮靠 refillMax 收尾；sessions 为 3 时想凑齐两个空位再一起补，就把 refillWhenFree 设成 2。refillAfter 防止一个位置因为凑不够数而长时间空着；refillMax 给一轮封顶：曾经一空就补，一轮开了 11 个 Issue，7 个做完的一直等发布。

## 预算：按订阅额度，不按美元

用户的 claude 是 Claude Max($200)、agy 是 Google AI Pro，都按订阅计费，美元预算没有意义；真正的限制是两个订阅的用量额度(Claude 为 5 小时窗口加每周额度，另有 Opus、Sonnet 的分模型额度；agy 为 5 小时刷新加每周额度)。官方都没有公布具体额度。量化数据中照常记按价格估算的费用，只用于对比优化前后。

看真实额度，不靠自己估算：

| 工具 | 读什么 | 出处 |
|---|---|---|
| Claude | 每次调用开头的 `rate_limit_event`(状态：正常、快到上限、已拒绝；已用比例；重置时间)；额度用完时的报错 `You've hit your session limit · resets 3:45pm` 及 weekly、Opus、Sonnet 几种 | Claude Code 文档 errors、agent-sdk |
| agy | 额度用完时的报错 `Individual quota reached … Resets in 143h57m55s` | agy 的 GitHub issue |
| codex | 额度用完时的报错 `You've hit your usage limit … try again in …` | Codex CLI 报错 |

官方没有查询剩余额度的接口，只能读这些信号。解析在 `agents/tools/<工具>.py`，结果为 `RateLimit(工具, 窗口, 状态, 已用比例, 重置时间)`；窗口为 five_hour、weekly、opus、sonnet。

`Quota` 把信号存进 store 的 `state` 表(键 `quota`：工具 → 窗口 → 状态、已用比例、重置时间、看到的时间)，新信号覆盖同一窗口的旧信号；过了重置时间的信号不再算。重置时间认不出的，看到之后 `resources.quota.unknownResetWait`(缺省 1h)内有效，之后由下一次调用重新探明。

## 给用户自己留余量

`Quota.reserve_reached()`：任一条件满足即不再开始新的 Issue，手上这一步做完(由调度判断；`reserve_reasons()` 给出原因)。

**在哪配置**：`resources.quota`。

| 条件 | 键 | 缺省 | 依据 |
|---|---|---|---|
| 5 小时窗口已用 | reserveFiveHour | ≥ 70% | 推断 |
| 每周额度(含分模型的)已用 | reserveWeekly | ≥ 60% | 推断：每周额度更稀缺、周中不恢复；官方提醒并发多时会在 5 小时窗口重置前用完一周的额度 |

另外：缺省同时只跑一个会话；定时运行放在夜间(时段在调度的 settings 中设)。

## 额度用完

1. 当前这一步的进度落盘，释放锁；
2. 停掉所有在跑的工作(调用结果为 quota_exhausted，`limits.decide` 给出 HALT_ALL；claude 的 `rate_limit_event` 一出现 rejected 就立即杀进程)；
3. 生成「出问题」文档：哪个工具、哪种额度(5 小时、每周、Opus 或 Sonnet)用完、重置时间、接着跑的命令；
4. 到重置时间后，下一次定时运行从检查点接着做。到重置时间前，`Quota.halted_until(工具)` 不为空，该工具的调用不启动(`agents/call.py`)。

不换到另一个工具。agy 每周额度被锁时可能要等五六天，期间用到 agy 的步骤都不跑。

## 每个 Issue 的用量上限

`IssueBudget`：counters 表按对象(键 `issue_tokens.<对象>`)累计 `Tokens.weighted(cacheReadWeight)`；达到 `issueTokens` 即 `exceeded`。

**在哪配置**：`resources.issueTokens`(缺省 2M)、`resources.cacheReadWeight`(缺省 0.1)。

- 2M token，缓存读取按 1/10 计(官方只说缓存「占用更少」，1/10 依 API 价格比推断)；
- 每次调用结束(含失败与中断)把各次工具调用的用量(输入、输出、缓存写入、缓存读取)累加到该对象；
- 调用进行中逐行累计，将超过余量时杀进程(budget_limit)；已达上限的对象，调用不启动；
- 超过时停下，生成「出问题」文档。原 0018 用了 1450 万 token，这个上限能拦住。

单次调用：输入 50k token(程序拼提示时按此截取代码摘要与知识条目)，输出 16k token(`controls.<键>.inputTokens`、`outputTokens`)。

## 防护

- 调用 claude 时去掉环境变量 `ANTHROPIC_API_KEY`：有它时 claude 优先走 API 按量付费，会悄悄产生账单(`security.py:child_env` 写死)；
- agy 的超额付费开关(AI Credit Overages)保持关闭，只用订阅额度。

## 省额度(保住缓存)

- 一个会话只用一个模型、一个推理强度(换模型或强度会让缓存失效)；格式重试续接同一会话；
- 提示的固定部分不变，代码摘要作为固定前缀复用；
- 不开子 agent(子 agent 缓存只保留 5 分钟，主会话 1 小时)；
- agy 每次请求自带约 25k token 的系统开销，只给它范围窄、轮数少的任务。

## 条款风险

Anthropic 的用户条款禁止「以脚本等自动方式访问」，除非用 API key 或官方明确允许；官方提供了给脚本用的 `claude setup-token`，但说明订阅额度按「普通个人使用」设定，2026 年 2 月也澄清过禁止第三方工具借用订阅登录。因此：

- 只用官方原版的 `claude` 命令与用户自己的登录；
- 只处理用户自己的项目；
- 不直接用订阅的登录凭据调 API 或 SDK；
- 用量控制在个人使用的范围；如果以后要全天候跑，改用 API key 按量付费。

## 出处

Claude Max 计划(support.claude.com/en/articles/11049741)；Claude Code 错误与额度(code.claude.com/docs/en/errors)；statusline 的 rate_limits(code.claude.com/docs/en/statusline)；Agent SDK 事件(code.claude.com/docs/en/agent-sdk/typescript)；用量计算与缓存(support.claude.com/en/articles/9797557、code.claude.com/docs/en/prompt-caching)；订阅登录与条款(code.claude.com/docs/en/authentication、code.claude.com/docs/en/legal-and-compliance、anthropic.com/legal/consumer-terms)；Antigravity 计划(antigravity.google/docs/plans)；agy 额度报错(github.com/google-antigravity/antigravity-cli/issues/789)；agy 请求开销(github.com/google-gemini/gemini-cli/discussions/27307)。
