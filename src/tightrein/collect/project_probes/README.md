# 项目探针(collect/project_probes)

## 是什么

采集阶段留给项目的扩展口：只有项目自己知道的业务异常(定时任务没按时跑完、处理量为零、按项目日志规范才看得出的问题)，
由项目写一个只读的检查脚本(探针)，tightrein 按登记定时调用、校验输出、转成信号，之后与内置来源走同一套去重与评估。

tightrein 不只规定契约，还掌握每一种探针方法：每种方法一份统一格式的方法文档(`NN-<名称>.md`，格式见 `TEMPLATE.md`)，
接入时按下面的对照表选用、照文档落地，不再从零手写。

## 选用指南

### 对照表

| 方法 | 发现什么 | 项目要具备 | 不具备时 |
|---|---|---|---|
| [01 读取运行记录](01-run-records.md) | 定时流程跑了但失败或带警告 | 流程在本机写 JSON Lines 运行记录(时间、状态、摘要) | 先让流程写运行记录，或用 02 |
| [02 检查定时任务与业务状态](02-job-status.md) | 任务没按时完成、处理量为零 | 只读状态接口(或只读数据库账号)与只读凭据；sites.json 的 `target.baseUrl` | 先在项目里加只读状态接口 |
| [03 按项目日志规范统计](03-log-rules.md) | 权限校验失败突增、catch 后只记日志的异常 | 日志进了日志平台(Loki)且有规范字段；只读令牌 | 只要错误级别的日志用平台错误模块即可 |

### 选用步骤

1. 逐行对照项目现状，判断「需要吗」(项目有没有这类风险)、「能实现吗」(数据源与权限在不在)；
2. 选出要用的方法，按方法文档在工作区 `scripts/` 下写探针；
3. 在工作区 `settings.json` 的 `overrides.controls."collect.project_probes".probes` 登记：

   ```json
   {"name": "daily-import", "command": ["{python}", "scripts/daily_import.py"], "every": "1h",
    "secrets": ["demo.readonly"], "timeout": "2m"}
   ```

   `{python}` 是 tightrein 自己的解释器(脚本可以 `from tightrein.collect.project_probes import helpers`)；`secrets`
   为 secrets.json 中的条目名(只有登记的能读到)；`timeout` 省略时取 `probeTimeout`；
4. 接入清单 `setup.json` 中 `collect.project_probes` 写 `enabled`(探针的脚本与登记都在工作区，`method` 为 null)；
5. 接入试跑(`tightrein project check`)调用 `source.trial`：只校验输出并列出会产出的信号，不保存任何东西。

都不适用时：按 `TEMPLATE.md` 写一份新方法，加进本目录，编号递增。

## 流程

`source.collect(runtime)`：读登记 → 按 `every` 与上次成功运行时间选出到期的探针 → 并行运行(经 `protocol/scripts.py`，
标准输入给输入 JSON，标准输出读回) → 按 `output.schema.json` 校验 → 转成信号 → 每个成功的探针保存「上次运行时间与
输出的 state」(state 表 `collect.project_probes:<名>`，随信号在去重的同一事务保存)。

## 输入与输出

- 探针的输入、输出：见下面的「契约」，输出按 `output.schema.json` 校验；
- 信号：check_type 为 `probe`，location、message 取探针给的 location、symptom，severity_hint 取 severityHint，
  group_key(指纹)为「探针名:fingerprint」，evidence 带 sourceName(探针名)、probeFingerprint、facts(探针的 evidence)、
  details(探针的 context)；
- 覆盖范围：成功运行的探针名(该探针报过的问题在它不再报时累计「覆盖而没再出现」)。

### 契约

探针经标准输入收到一个 JSON(输入)，经标准输出返回一个 JSON(输出)；输出不合格时本次作废、状态不保存。

输入(`source.py` 组装)：

| 字段 | 类型 | 说明 | 示例 |
|---|---|---|---|
| `name` | 字符串 | 探针名(登记中的 name) | `"daily-import"` |
| `lastRunAt` | 时间或 null | 上次成功运行的时间；第一次运行为 null | `"2026-10-01T01:00:00Z"` |
| `state` | 对象或 null | 上次输出的 state，原样交回；第一次运行为 null | `{"lastBatch": "2026-10-01"}` |
| `window.since` | 时间 | 本次检查的起点(含)：上次成功运行的时间，第一次为现在减 `lookback` | `"2026-10-01T01:00:00Z"` |
| `window.until` | 时间 | 终点(不含)：现在 | `"2026-10-02T01:00:00Z"` |
| `workspace` | 字符串 | 工作区的绝对路径 | `"/Users/me/tightrein/workspaces/demo"` |
| `environment` | 字符串或 null | 被测环境(sites.json 的 `target.environment`)；没有配置时为 null | `"production"` |
| `baseUrl` | 字符串或 null | 被测地址(sites.json 的 `target.baseUrl`)；没有配置时为 null | `"https://demo.example.com"` |

时间一律是 UTC 的 `YYYY-MM-DDTHH:MM:SSZ`。

输出(`output.schema.json`，字段表也在 `docs/reference/handoff.md`)：

| 字段 | 必填 | 说明 |
|---|---|---|
| `signals[]` | 是 | 每个异常一条；没有异常时为空数组 |
| `signals[].location` | 是 | 异常所在的位置：业务对象、任务名、接口或代码位置 |
| `signals[].symptom` | 是 | 现象，一句话，写观察到的事实不写原因(最长 1000 字) |
| `signals[].evidence` | 是 | 证据，至少一条：可核对的事实(数值、时间、查询与结果)，不含凭据 |
| `signals[].severityHint` | 是 | `P0` 到 `P3`，不确定时为 null；只是提示，评估会重新判断 |
| `signals[].fingerprint` | 是 | 同一个问题每次相同(「检查项:对象」)，与探针名一起作为问题指纹 |
| `signals[].occurredAt` | 否 | 发生时间；省略时为本次运行的时间 |
| `signals[].context` | 否 | 给评估看的补充数据(不含凭据与个人信息) |
| `state` | 否 | 留给下次运行的状态，下次原样放进输入 |
| `notes` | 否 | 写进运行摘要的说明 |

脚本里用 `helpers.read_input()` 读输入、`helpers.signal(...)` 组装信号、`helpers.emit(...)` 校验后写出。

## 配置

`controls."collect.project_probes"`(缺省在 `settings/defaults.json`)：

| 键 | 含义 |
|---|---|
| `probes` | 探针登记：name、command、every(时长)、secrets、timeout |
| `lookback` | 第一次运行的回看时长 |
| `probeTimeout` | 登记里没写 timeout 时每个探针的时限 |

被测地址与环境取 sites.json 的 `target.baseUrl`、`target.environment`(交给探针的输入)。

## 设计依据

- 探针失败(退出码非 0、超时、输出不是 JSON 或不合契约)时本次作废：不产出信号、不保存状态，下次到期重试；标准错误
  脱敏后写进原始输出。只保存成功运行的状态，失败不会让窗口前进而漏掉一段。
- 子进程环境只给白名单，另外只传工作区、探针名与登记过的凭据(`TIGHTREIN_SECRET_<条目>`)；`helpers.secret` 只读得到
  这些，读到的值立刻登记进脱敏器：凭据不会进输出、日志与信号。
- 指纹为「探针名 + 探针给的 fingerprint」：探针最清楚什么是「同一个问题」，tightrein 不猜。
- 方法文档格式统一(十一个小节对应 tightrein 已有的约定)：同一类探针不再每个项目各写一份、质量参差。
- 出处：旧 `sources/project_probes/`；方法 1 提炼自 ai-interview-collector 的 local-runs 探针，方法 2、3 来自旧的
  「如何编写项目探针」文档的两个示例；旧的探针契约文档并入上面的「契约」。

## 不做什么

- 不内置通用实现：方法文档加参考代码由项目照着写(每个项目的数据源、字段、阈值都不同)；
- 不调用模型、不读代码；
- 探针不写被测系统、不登录服务器；
- 不替探针判断严重度：severityHint 只是提示，评估会重新判断。
