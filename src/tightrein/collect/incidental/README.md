# 任务外发现(collect/incidental)

## 是什么

评估、实施中 agent 顺带发现的、与当前任务无关的问题，写在各自交接文档的 `facts.incidentalFindings` 里；本模块读取它们
转成信号，走正常的去重与评估。不调用模型、不读代码。

## 流程

`source.collect(runtime)`：找未读过或内容已变的交接文档(`handoffs.py`) → 取出发现、校验格式 → 按类别过滤并转成信号
(`mapping.py`) → 记录已读(路径 → 内容哈希)。

## 输入与输出

每条发现的字段(`finding.schema.json`，评估与实施的输出 schema 复制同样的结构)：

| 字段 | 含义 |
|---|---|
| `file` | 文件路径(相对仓库根) |
| `line` | 行号；不确定时为 null |
| `symbol` | 函数或方法名；不在函数里时为 null |
| `category` | 类别：`defect`(缺陷)、`security`(安全)、`performance`(性能)、`data`(数据)；其他类别(命名、风格、重构建议)由程序丢弃 |
| `confidence` | 把握程度：`confirmed`(确定)、`suspected`(疑似) |
| `evidence` | 一句证据 |
| `text` | 原文 |

信号：check_type 为 `incidental:<类别>`，location 为「文件:行号」(没有行号时为文件)，symbol 为发现给的符号，message 为
原文，occurred_at 为交接文档的生成时间，commit 为产生它的环节的基准 commit，group_key(指纹)为
`incidental:<文件>:<符号>:<类别>`；evidence 带把握程度、证据、来源交接文档、步骤、对象与运行。

已读记录：state 表 `collect.incidental:read`。覆盖范围永远为空。

## 配置

`controls."collect.incidental".points`：读哪些步骤的交接文档(含其下的小步骤)，值为该步骤 facts 中基准 commit 的字段名，
如 `{"assess.triage": "commit", "implement": "baseCommit"}`。

## 设计依据

- 只认结构化的发现：不再从原文猜位置、不导入旧报告，「定位不到、要用户补登」的情况随之消失。
- 统一字段与类别：评估能按类别与把握程度排序，非缺陷在源头挡掉；一份格式定义，所有角色共用。
- 指纹为「文件 + 符号 + 类别」，不用行号：行号变化不再拆成两个，同一处的不同问题不再并成一个。
- 路径和内容哈希都与已读记录相同的跳过；交接文档重跑后内容变了就按新内容重读，重复的发现由去重按指纹归并。
- 单个交接文档读不了或解析失败时跳过它、不记已读，下次重试，状态为 partial；没有新来源时为 skipped。
- 覆盖范围为空：任务外发现不会因为「覆盖运行里没出现」被判为已解决(本来就不是每次都会检查到)。
- 发现的 commit 取产生它的环节的基准，occurred_at 取交接文档的生成时间：回归与解决的判定才有可比的版本。
- 出处：旧 `sources/incidental/`(删去 `archive_import.py` 与 `locate.py`)。

## 不做什么

- 不收评审、静态巡检的审查、验收等角色的顺带发现(以后再定)；
- 不判断发现是否成立：交给评估。
