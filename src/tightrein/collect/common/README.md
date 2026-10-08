# 采集共用(collect/common)

## 是什么

采集各来源真正共用的部分，与具体平台无关；只在一个来源里用的放在该来源的文件夹，跨阶段的放 protocol/ 或 store/。

| 文件 | 内容 | 用到的来源 |
|---|---|---|
| `source.py` | 来源结果 `SourceResult`、带类型的错误、`guarded`(超时与失败归类)、`each`(子来源并行、各自成败) | 全部 |
| `signals.py` | 信号 `Signal`、信号工厂(编号、脱敏、截断、证据过大移到原始输出)、按部署取 commit | 全部 |
| `window.py` | 按时间窗口增量读取、读取位置 | 平台错误、访问日志 |
| `methods.py` | 平台方法(程序加同名 `.yaml` 清单)的加载与参数合并校验 | 平台错误、访问日志、业务告警 |

只读 HTTP 请求、运行项目脚本、原始输出目录与外部失败的分类被实施、发布共用，放在 `protocol/http.py`、`protocol/scripts.py`、`protocol/raw.py`、`protocol/external.py`；本目录的 `SourceError` 等是 `protocol/external.py` 中类型的别名。

## 流程

每个来源 `collect(runtime) -> SourceResult`；调度(`collect/collect.py`)经 `guarded` 调用，超时或抛出 SourceError 的记为
failed；结果中的 state(读取位置等)由去重在写信号的同一个事务里保存。

## 输入与输出

- `SourceResult`：source、status(done、partial、skipped、failed)、signals、read、window、reason、state、metrics、coverage、notes；
  `guarded` 统一写 metrics.duration_ms 与 metrics.produced.signals，调度把 read 写进交接文档的 facts.read(status、watch 读它们)；
- `Signal`：见 44c，编号 `S-<ULID>`。

## 配置

`controls.collect` 下的 `messageChars`、`evidenceBytes`、`excerptChars`(各来源可在自己的控制键下覆盖)；HTTP 时限取
`limits.timeouts.http`；整个来源的时限是 `controls.<来源>.sourceTimeout`(调度按它截断，与模型调用的 timeout 分开)。

## 设计依据

- 窗口：起点取上次保存的终点，没有记录时往前回看；终点去掉秒以下；起点取 `min(上次终点, 现在)`，时钟回拨时窗口不倒置；
  读满条数上限时读取位置停在实际读到的最后时间，没读到的下次接着读；平台能查到的最早时间晚于起点时写「可能漏读」。
- 读取位置只随结果返回，与信号同事务保存：读取失败、超时或事务回滚时位置不前进，下次从原位置重读。
- 信号：message 先脱敏再截断(反过来截断可能切开密钥，规则就认不出了)；时间换成 UTC 去掉秒以下；evidence 逐项脱敏，
  产出方已换成 `<TOKEN>` 的 reproduce 不再处理；序列化后超过 evidenceBytes 时从最大的一项起移到原始输出
  `refs/<信号编号>-<键>.json`，原处改成 `<键>Ref`；location 给出时不能是空串。
- commit 取发生时间之前最近一次成功部署的 commit(部署时间缺失时用检测时间，同一时间按 commit 排序)：回归与解决都依赖它。
- 原始输出的路径一律相对本来源的 raw 目录，拒绝绝对路径、`..` 与反斜杠：外部输入不能把文件写到目录外。
- HTTP：4xx、5xx 照常返回状态码与响应体；没有响应时 status 为空并写明原因；耗时用单调时钟；有代理表时按表发送；
  令牌只在请求头里，错误信息不含令牌。
- 来源各自只抛带类型的错误，归类只在 `guarded` 一处；不各自吞掉、不各自重试。
- 出处：旧 `sources/common/`、`sources/base.py`、`extensions/methods/platforms.py`。

## 不做什么

- 不另写脱敏规则：用 `protocol/security.py` 的 Redactor；
- 不调子进程：项目脚本经 `protocol/process.py`；
- 不放只有一个来源用的东西。
