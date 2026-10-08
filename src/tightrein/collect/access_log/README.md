# 访问日志(collect/access_log)

## 是什么

按接口统计请求，与历史基线比较，发现耗时变长(p95)或 5xx 比例上升的接口，每个退化的接口一条信号。不调用模型。

**先看项目有哪种数据源再选取法：**

| 数据源 | 取法 | 接入清单 |
|---|---|---|
| 访问日志已进日志平台(Loki) | 程序：`log_platform/fetch.py` | `enabled`，`method: "loki"` |
| 项目自己的日志文件、数据库 | 方法文档：`project_sources/`，项目按文档写取数据的脚本 | `custom`，`script` 写脚本路径，`guide` 指向所用的方法文档 |

## 流程

`source.collect(runtime)`：取数据(两种取法交出同样的东西：访问日志的行，或已解析的请求) → 解析成请求(`parse.py`：
方法、路由、状态码、耗时) → 按接口统计(`stats.py`) → 与基线比较、每个退化的接口一条信号 → 更新基线。

## 输入与输出

- 读取位置(state 表 `collect.access_log`)：窗口终点与基线(每个接口的请求数、p95、5xx 比例)；
- 信号：check_type 为 `latency` 或 `error_rate`，location 为「方法 路由」，message 写本次与基线的数值，evidence 带
  sourceName(`access_log`)、窗口、本次与基线的统计；
- 覆盖范围：读到请求时为 `access_log`。
- 项目脚本的输入、输出：见 `project_sources/README.md` 与 `project_sources/output.schema.json`。

## 配置

`controls."collect.access_log"`(缺省在 `settings/defaults.json`)：

| 键 | 含义 |
|---|---|
| `lookback` | 第一次读取往前回看的时长 |
| `query` | 日志平台上取访问日志的查询(用日志平台时必填) |
| `limit` | 每次读取的条数上限 |
| `fields` | JSON 行的字段位置(method、route、status、durationMs；嵌套以 . 连接) |
| `pattern` | 纯文本访问日志的正则(命名分组 method、route、status、durationMs)，给出时优先 |
| `minRequests` | 本次与基线的请求数都不少于它才比较 |
| `latencyRatio` | p95 超过基线的这么多倍为耗时退化 |
| `errorRateDelta` | 5xx 比例比基线高出这么多为错误率退化 |
| `baselineWeight` | 基线指数平均中本次窗口的权重 |
| `scriptTimeout` | 项目取数脚本的时限 |
| `loki` | 日志平台方法的参数(地址取 sites.json 的 `loki`，令牌取 secrets.json 的 `loki.token`) |

## 设计依据

- 基线存在读取位置里，按指数平均更新，和信号在同一事务保存；取数失败时位置与基线都不前进；第一个窗口只建基线、不报退化。
- 本次和基线的请求数都不少于 minRequests 才比较：样本少时 p95 与比例都不稳，不报。
- p95 按最近秩取；没有耗时的请求不参与 p95 但计入错误率。
- 路由去掉查询串、方法转大写：同一个接口不因参数不同而拆开；取不到方法、路由、状态码的行计入无法解析。
- 读满条数上限时读取位置停在实际读到的最后时间，其余下次接着读。
- 两种取法交出同样的东西，后面的解析、统计、基线比较是同一套程序：每个项目的文件位置、表结构不同，只有取数这一步按项目写。
- 出处：旧 `sources/access_log/`。

## 不做什么

- 不分析单个慢请求、不找原因：只报接口级的退化，交给评估；
- 不登录服务器读文件：项目自己的数据源由项目脚本只读取出。
