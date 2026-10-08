# 平台错误(collect/platform_errors)

**只对接两类平台：错误追踪 Sentry(及兼容其 API 的 GlitchTip)、日志平台 Loki。没有用这两个平台的项目用不了这个
模块，在接入清单里设为 disabled 即可，不影响其他采集模块。**

## 是什么

从错误追踪平台与日志平台的只读 API 取回应用运行报错(后端异常、浏览器端错误、错误级别的日志)，转成信号。不调用模型、
不读代码。

## 流程

`source.collect(runtime)`：

1. 接入清单 `collect.platform_errors.method` 决定读哪些平台：`sentry`、`loki` 或 `sentry+loki`；
2. 每个子来源按时间窗口增量读取(`collect/common/window.py`)：起点是上次保存的终点；
3. 两个子来源并行读取，各自成败：
   - 错误追踪(`error_tracking/sentry.py`)：窗口内有新事件的错误分组，每个分组一条信号(`tracking_signals.py`)；
   - 日志平台(`log_platform/loki.py`)：按 `logQuery` 取原文 → 日志解析(`log_parse/json_lines.py` 或 `regex.py`) →
     按级别筛选、截取本项目帧(`select.py`) → 每条一个信号(`log_signals.py`)；
4. 合成来源结果：两个都成功为 done，一个失败为 partial(失败的不保存位置)，都失败为 failed。

## 输入与输出

| 子来源 | 读取位置(state 表) | 信号 |
|---|---|---|
| 错误追踪 | `collect.platform_errors:error_tracking` | check_type `error`；location 为第一个本项目帧的「文件:函数」，没有时为 culprit、再没有时为标题；group_key 为 `sentry:<组织>/<编号>`；前端的带操作轨迹、页面与浏览器 |
| 日志平台 | `collect.platform_errors:log_platform`(含解析状态) | check_type `error`；location 为第一个本项目帧的「文件:类名.方法名」，没有时为日志类别；没有 group_key，由去重按异常类型与帧(或类别与消息)算指纹 |

evidence 都带 `sourceName`(子来源名)；覆盖范围为本次真正读到的子来源。

## 配置

- 接入清单：`collect.platform_errors` 为 enabled，`method` 写平台；
- `sites.json`：`sentry.url`、`sentry.organization`；`loki.url`、`loki.user`(Grafana Cloud 的实例编号)、`loki.tenant`；
- `secrets.json`：`sentry.token`(只需 event:read)、`loki.token`(没有时不带认证)；
- `controls."collect.platform_errors"`(缺省在 `settings/defaults.json`，项目在工作区 settings.json 覆盖)：

| 键 | 含义 |
|---|---|
| `lookback` | 第一次读取往前回看的时长 |
| `logQuery` | 日志平台上取报错的查询(LogQL)；用日志平台时必填 |
| `logLimit` | 每次从日志平台读取的条数上限 |
| `levels` | 产出信号的归一化级别(缺省 error、critical) |
| `projectFrames` | 每条信号保留的本项目帧数 |
| `logParse` | 日志解析方法：`json_lines` 或 `regex` |
| `sentry`、`loki`、`json_lines`、`regex` | 各方法的参数，可填的键见同名 `.yaml` 清单的 optionsSchema |

接新平台：在 `error_tracking/` 或 `log_platform/`(解析方法在 `log_parse/`)下加一对 `<方法>.py` 与 `<方法>.yaml`，
不改调用方。访问日志、项目探针取日志时也用这里的日志平台与日志解析方法。

## 设计依据

- 两个子来源各用一个读取位置、各自成败：一个平台出故障不拖累另一个，也不会让失败的那个跳过一段窗口。
- 读满条数上限时，读取位置停在已读片段中最晚的时间，没读到的部分下次接着读，不跳过(已知遗留：同一秒的条目可能重读
  一次，表现为多计一次出现)。
- 平台能查到的最早时间晚于窗口起点时写「可能漏读」，提醒两次运行的间隔要短于平台保留期。
- 只有读到数据的子来源记进覆盖范围：平台问题的「已解决」只按真正读过的来源判定，平台故障不会把问题误判为已解决。
- Sentry 翻页按 Link 头中 `rel="next"; results="true"` 的 cursor(`results="false"` 也带 cursor)；堆栈取链式异常的
  最外层，帧倒过来让出错处在前；平台上的版本号另存 `platformRelease`，信号的 commit 仍按部署时间取。
- Loki 按 `direction=forward` 分页，一页取满就从最后一条的纳秒时间加 1 继续；结果不是日志流(指标查询)时报错。
- JSON 行的时间可以是 ISO 字符串或 Unix 时间戳(大于 10^11 按毫秒)，布尔值不当时间；正则解析的只有时刻的时间按流
  补日期、跨午夜加一天，状态跨片段、跨运行传递。
- 令牌只放进请求头，不进输出、日志与错误信息；非 2xx 报不可用，错误信息不含令牌。
- 出处：旧 `sources/platform_errors/`、`extensions/methods/` 的 error_tracking、log_platform、log_parse 与 `platforms.py`。

## 不做什么

- 不回写平台(不改分组的解决状态)，只读 `is:unresolved` 的分组；
- 不解析技术栈特有的堆栈写法(通用的两个解析方法 frames 为空)；
- 不判断报错是否成立、是否要修：交给评估。
