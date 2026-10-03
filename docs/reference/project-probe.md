# 项目探针契约

<!-- 本文件由 core/dev/contract_reference.py 生成，不要手改；改 schema 或类型注册后重新生成。 -->

写法见[如何编写项目探针](../how-to/write-project-probe.md)。探针经标准输入收到输入 JSON，经标准输出返回输出 JSON；输出不合格时本次作废、状态不保存。试跑：`tightrein probe test <名称>`。

## 输入

核心经标准输入交给探针的 JSON(docs/how-to/write-project-probe.md)

schema：`data/project-probe-input.schema.json`

| 名称 | 类型 | 必填 | 说明 | 示例 |
|---|---|---|---|---|
| `name` | 字符串 | 是 | 探针名(project.yaml 中登记的 name) | `"daily-import"` |
| `lastRunAt` | 字符串(`^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$`) 或 null | 是 | 上次成功运行的时间；第一次运行为 null | `"2026-10-01T01:00:00Z"` |
| `state` | 对象 或 null | 是 | 上次运行输出的 state，原样交回；第一次运行为 null | `{"lastBatch": "2026-10-01"}` |
| `window` | 对象 | 是 | 本次应检查的时间范围：上次运行时间(第一次为 sources 的首次回看)到现在 |  |
| `window.since` | 字符串(`^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$`) | 是 | 起点(含) | `"2026-10-01T01:00:00Z"` |
| `window.until` | 字符串(`^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$`) | 是 | 终点(不含) | `"2026-10-02T01:00:00Z"` |
| `workspace` | 字符串(`^/`) | 是 | 工作区的绝对路径 | `"/Users/me/tightrein/workspaces/demo"` |
| `environment` | `staging` \| `production` \| `local` | 是 | 被测地址所在的环境(target.environment) | `"production"` |
| `baseUrl` | 字符串 或 null | 是 | 被测地址(target.baseUrl)；没有配置时为 null | `"https://demo.example.com"` |

## 输出

探针经标准输出返回的 JSON；不合格时本次输出作废、状态不保存

schema：`data/project-probe-output.schema.json`

| 名称 | 类型 | 必填 | 说明 | 示例 |
|---|---|---|---|---|
| `signals` | 数组 | 是 | 发现的异常，每条一个信号；没有异常时为空数组 |  |
| `signals[].location` | 字符串 | 是 | 异常所在的位置：业务对象、任务名、接口或代码位置 | `"job:daily-import"` |
| `signals[].symptom` | 字符串 | 是 | 现象，一句话，写观察到的事实不写原因 | `"昨天的导入任务没有在 02:00 前完成"` |
| `signals[].evidence` | 数组 | 是 | 证据：可核对的事实(数值、时间、查询与结果)，不含凭证 | `["最近一次完成时间 2026-09-30T01:58:00Z", "今天 03:00 查询 status=done 的记录为 0 条"]` |
| `signals[].severityHint` | `P0` \| `P1` \| `P2` \| `P3` 或 null | 是 | 严重度提示(P0 到 P3)，分诊参考；不确定时为 null | `"P1"` |
| `signals[].fingerprint` | 字符串 | 是 | 同一个问题每次给出相同的值(例如「检查项:对象」)，核心与探针名一起计算问题指纹 | `"missed-run:daily-import"` |
| `signals[].occurredAt` | 字符串(`^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$`) | 否 | 发生时间；省略时为本次运行的时间 | `"2026-10-02T02:00:00Z"` |
| `signals[].context` | 对象 | 否 | 给分诊看的补充数据(不含凭证与个人信息) | `{"expectedAt": "02:00"}` |
| `state` | 对象 或 null | 否 | 留给下次运行的状态，下次原样放进输入的 state | `{"lastBatch": "2026-10-02"}` |
| `notes` | 数组 | 否 | 写进运行摘要的说明 | `["跳过了 3 条测试数据"]` |
