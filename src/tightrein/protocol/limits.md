# 运行时限

原超时、防卡顿、防循环、重试四项合并。程序在同名的 `limits.py`(按失败类型处理、退避、熔断、没有进展就停)；超时由 `process.py` 执行，锁的心跳在 `store/locks.py`。

所有时长写成数字加单位(`30s`、`20m`、`2h`)，不再混用秒、分钟、毫秒。缺省值经上网查证，标「推断」的是没有找到权威数字、依出处推出的。全部在 `settings/defaults.json` 的 `limits` 与 `controls` 中，可在工作区覆盖。

## 模型调用

**在哪配置**：`controls.<控制键>.turns`、`timeout`、`idle`、`outputTokens`、`inputTokens`(按「小步骤 → 模块 → 阶段 → `*`」继承)。

| 项 | 缺省 | 出处 |
|---|---|---|
| 只读步骤(评估、定位、方案、审查) | 15 轮、10m | 推断：claude-code-action 社区示例的 PR 评审用 5 轮，分析只需少量工具调用 |
| 编码 | 30 轮(难的最多 50)、30m | Anthropic 官方修测试示例用 30 轮；SWE-agent 论文成功的中位数 12 步、失败的平均 21 步 |
| 单次输出 token | 16k | 推断：整体由用量上限管 |
| 单次输入 token | 50k | 程序拼提示时按此截取代码摘要与知识条目 |
| 每个 Issue 的用量上限 | 2M token(缓存读取按 1/10 计)，见 resources.md | 按订阅计费，不设美元上限；SWE-agent 论文发现投入超过一定量后基本不再提升 |

工具原生不支持轮数或输出上限的，由 `agents/call.py` 逐行兜底杀进程(见 `agents/README.md`「兜底规则」)。

## 轮数

**在哪配置**：`controls.<控制键>.rounds`。

| 项 | 缺省 | 出处 |
|---|---|---|
| 审查或自检不通过交回修改 | 3 | Aider 固定为 3；Self-Refine 收益集中在前几轮；另有「没有进展就停」兜底 |
| 方案被否决后重出 | 1 | 推断 |
| 评估证据不足交回重做 | 1 | 推断 |

超过上限停下，生成「出问题」文档。

## 没有进展就停

`no_progress(上一轮阻断项, 本轮阻断项, 上一轮 diff 哈希, 本轮 diff 哈希)`：本轮 diff 与上一轮相同，或连续两轮阻断项(按位置加类型比较，不计顺序)相同，直接停下，不耗满轮数。本轮没有阻断项时不算(已通过)。旧代码没有，新写。

## 重试与退避

只在一层重试，不层层叠加：模型调用在 `agents/call.py`，只读的 git、gh 命令在 `protocol/git/`；等待时间都取 `backoff_s`。

**在哪配置**：`limits.retry`。

| 项 | 缺省 | 出处 |
|---|---|---|
| 次数 | 2 次(共 3 次) | Anthropic SDK 缺省 2；AWS 共 3 次；Google SRE 每个请求最多 3 次 |
| 起始间隔 | 1s | AWS 限流用 1s；Anthropic SDK 0.5s |
| 最大间隔 | 20s，服务过载(529)时 60s | AWS 20s；Gemini SDK 60s |
| 抖动 | 全抖动(0 到当前间隔之间随机) | AWS Builders' Library |
| retry-after | 对方给了就照它等 | Google SRE |

`backoff_s(第几次重试, retry_after_s=…, overloaded=…, settings=…)` = 随机(0, min(上限, 起始 × 2^(n-1)))；给了 retry-after 就返回它。

临时错误的识别：工具失败且错误说明或 stderr 尾部含 `limits.transientPatterns` 中的片段(不分大小写)：`API error`、`request failed`、`stream was interrupted`、`connection reset`、`: EOF`、`Service Unavailable`、`UNAVAILABLE`、`ECONNRESET`、`ETIMEDOUT`、`overloaded`、`529`、`rate limit`、`stream disconnected`。其他失败(如 invalid API key)不重试。错误中含 `529` 或 `overloaded` 的按过载处理。

## 按失败类型处理

`decide(状态, 第几次出现, fallback_used=…, settings=…) -> Action`，是全仓库唯一一处：

| 状态(agents 的统一结果) | 处理 | Action |
|---|---|---|
| 临时错误(限流、5xx、网络) | 按上表退避重试同一次调用 | RETRY，超过次数 STOP |
| 格式不符 | 续接同一会话带原因重试 1 次(续接用得上缓存) | RESUME_WITH_REASON，再不符 STOP |
| 被拒绝(如安全分类)、工具不可用 | 换备用模型重试 1 次，再失败就停 | FALLBACK，已换过 STOP |
| 超时、轮数到限、用量到限 | 不重试，停下，复盘记一条 | STOP |
| 认证失败 | 停，并对该工具立即触发依赖熔断(`Breaker.trip`) | STOP |
| 订阅额度用完 | 全部停下，见 resources.md「额度用完」；不换到另一个工具 | HALT_ALL |
| 越界、其余失败 | 停 | STOP |
| 测试环境问题(端口、启动失败) | 就地重试 1 次；测试本身不稳定的不重试 | 由本机运行检查处理，不经模型调用 |

## 熔断

`Breaker`，计数在 store 的 `counters` 表(跨进程、跨运行有效)。

**在哪配置**：`limits.breaker`。

| 项 | 缺省 | 出处 |
|---|---|---|
| 依赖熔断：连续失败次数 | 5 | 推断：Hystrix、resilience4j 按比例统计且要 20 到 100 次调用，调用量少用不上，改按连续失败计 |
| 依赖熔断：暂停时长 | 60s，之后放一次试探调用 | resilience4j 60s |
| 对象熔断：同一对象连续失败 | 3 | 推断 |

- `allow(依赖)`：未熔断放行；熔断中且未过暂停时长拒绝；过了就放一次试探(重新计时，试探结束前其余调用仍被挡住)；
- `record(依赖, ok)`：成功清零并关闭；失败累加，到次数即打开；
- `trip(依赖)`：立即打开(认证失败)；
- 依赖的失败指临时错误、工具不可用；超时说明不了工具本身是否可用，不计；
- 依赖熔断期间，模型调用改走备用模型(备用模型的工具未熔断时)，采集跳过该来源；
- `object_failed(对象)` 到 3 次返回真，不再处理这个对象；`object_progressed(对象)` 清零。

## 超时

**在哪配置**：`limits.timeouts`；模型调用的在 `controls.<控制键>.timeout`、`idle`。

| 项 | 缺省 | 出处 |
|---|---|---|
| HTTP 连接 | 5s | Anthropic SDK |
| HTTP 请求 | 30s | AWS：按延迟的高分位定 |
| 模型的单个请求 | 10m，流式 5m 没动静即超时 | Anthropic SDK 600s；Codex 空闲 300s |
| git | 每秒低于 1KB 持续 60s 即断开，整体 10m | git 低速设置；整体为推断 |
| 等 CI | 30m | GitHub 缺省 360 分钟，官方建议显式设 15 到 30 分钟 |
| 项目测试 | 15m | 推断：要比 CI 短 |
| Semgrep | 每条规则每个文件 5s，超时 3 次跳过该文件，整体 10m | Semgrep 文档 |
| 其他外部命令 | 5m | 推断 |
| 对象级 | 评估 30m、实施 2h | 推断 |
| 一次完整运行 | 4h | 推断 |

读取配置时程序检查：下级时限不超过上级(`settings/load.py`)。

## 锁(心跳)

**在哪配置**：`limits.lock`。

| 项 | 缺省 | 出处 |
|---|---|---|
| 心跳间隔 | 30s | Temporal |
| 失效 | 90s 没有心跳即失效，可以接管 | 心跳间隔的 3 倍 |

原来按「2 小时过期」，进程死了要等很久；改为心跳后最多 90 秒就能接管。运行锁同样按心跳。

## 出处

SmartBear/Cisco(smartbear.com/learn/code-review/best-practices-for-peer-code-review)；Google Small CLs(google.github.io/eng-practices/review/developer/small-cls.html)；Claude Agent SDK(code.claude.com/docs/en/agent-sdk/agent-loop)；SWE-agent 论文(arxiv.org/pdf/2405.15793)与配置(swe-agent.com/latest/reference/model_config)；Aider max_reflections(github.com/Aider-AI/aider/issues/3450)；Self-Refine(arxiv.org/pdf/2303.17651)；Anthropic SDK 常量(github.com/anthropics/anthropic-sdk-python 的 _constants.py)；AWS SDK 重试(docs.aws.amazon.com/sdkref/latest/guide/feature-retry-behavior.html)；AWS Builders' Library(aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter)；Google SRE(sre.google/sre-book/handling-overload)；Google Cloud 重试(docs.cloud.google.com/storage/docs/retry-strategy)；Hystrix(github.com/Netflix/Hystrix/wiki/configuration)；resilience4j(reflectoring.io/circuitbreaker-with-resilience4j)；Codex 配置(developers.openai.com/codex/config-reference)；GitHub Actions 限制(docs.github.com/en/actions/reference/usage-limits-billing-and-administration)；Semgrep(semgrep.dev/docs/kb/semgrep-code/semgrep-scan-troubleshooting)；Temporal(temporal.io/blog/activity-timeouts)。部分数值(resilience4j、Hystrix)来自二手资料。
