# 协议层总览

> 本文件由 `overview.py` 从 `settings/defaults.json` 生成，不要手改；
> 改了缺省值后运行 `python -m tightrein.protocol.overview`。

协议层定跨所有阶段的全局规则：每一方面一个 md(是什么、怎么做、在哪配置、缺省值、设计依据)，旁边是执行它的同名程序。
协议层只放规则逻辑，读写一律调用 `store/`；实际的取值都在 `settings/`(见 `settings/README.md`)。

## 协议层与配置的分工

- `protocol/` 定义协议：统一的控制字段各是什么意思、怎么生效、缺省值多少，键名规则与继承顺序；
- `settings/` 放取值：`defaults.json`(随 tightrein 提交) → `controls.json`(本机) → 工作区 `settings.json`(项目)，
  后者覆盖前者；
- 控制键写成「阶段.模块.小步骤」，控制字段按「小步骤 → 模块 → 阶段 → `*`」继承(`naming.md`)。

## 文件

| 文件 | 程序 | 管什么 | 取值在 settings 的哪里 | 主要缺省值 |
|---|---|---|---|---|
| `boundaries.md` | `boundaries.py` | 边界与关卡：各阶段能读写什么、命令白名单、改动量上限、受保护文件(两级)、必须人工的关卡 | `boundaries.readCommands`<br>`boundaries.changeCap`<br>`boundaries.autoApprove`<br>`boundaries.protected.forbidden`<br>`boundaries.protected.highRisk`<br>`boundaries.gates` | `readCommands`：12 项<br>`changeCap`：files 10，lines 400<br>`autoApprove`：files 3，lines 100<br>`forbidden`：7 项<br>`highRisk`：18 项<br>`gates`：issue auto_low_risk，design auto_within_threshold，merge auto_ci_passed |
| `naming.md` | `naming.py` | 文件命名与排版：目录、文件名、编号、时间与时长的写法，Markdown 与 JSON 的排版 | 不可配置 | — |
| `handoff.md` | `handoff.py` | 交接文档：结论、必填事实、量化数据、备注四部分；哪些落盘；给人看的三种文档；校验 | 不可配置 | — |
| `limits.md` | `limits.py` | 运行时限：三级超时、轮数、没有进展就停、按失败类型处理、重试与退避、熔断、锁的心跳 | `limits.retry`<br>`limits.breaker`<br>`limits.timeouts`<br>`limits.lock` | `retry`：attempts 2，base 1s，max 20s，overloadMax 60s<br>`breaker`：dependencyFailures 5，pause 60s，objectFailures 3<br>`timeouts`：httpConnect 5s，http 30s，git 10m，gitLowSpeedBytes 1000，gitLowSpeedTime 60s，ci 30m，tests 15m，semgrepRule 5s，semgrep 10m，command 5m，run 4h<br>`lock`：heartbeat 30s，stale 90s |
| `resources.md` | `resources.py` | 资源：并发与限流、订阅额度与给用户留的余量、每个 Issue 的用量上限 | `resources.concurrency`<br>`resources.quota`<br>`resources.issueTokens`<br>`resources.cacheReadWeight` | `concurrency`：sessions 1，modelCalls 4，collectSources 4，tests 8，platformRps 5<br>`quota`：reserveFiveHour 0.7，reserveWeekly 0.6，unknownResetWait 1h<br>`issueTokens`：2000000<br>`cacheReadWeight`：0.1 |
| `recovery.md` | `recovery.py` | 恢复与控制：检查点续跑、写操作幂等、中断时就地收尾、启动时恢复、暂停、急停、接管 | `limits.lock.stale`<br>`limits.shutdownGrace` | `stale`：90s<br>`shutdownGrace`：30s |
| `security.md` | `security.py` | 安全：凭据、脱敏、子进程环境变量白名单、只读副本与快照比对、网络、外部内容注入、外部代码 | 不可配置 | — |
| `records.md` | `records.py` | 记录：事件记录链、版本、保留期与清理 | `records.retention` | `retention`：runs 90d，raw 30d，operations 30d |
| `schedule.md` | `schedule/` | 调度：三种触发、按状态推进到关卡、运行锁、跑完必复盘 | `schedule.tick`<br>`schedule.window`<br>`schedule.every`<br>`schedule.advanceTo` | `tick`：15m<br>`window`：from 00:00，to 07:00，days 7 项<br>`every`：collect.project_probes 1h，collect.platform_errors 1h，collect.access_log 1d，collect.alerts 15m，collect.api_fuzz on_deploy，collect.static on_commit，collect.incidental 1h<br>`advanceTo`：release |
| `git.md` | `git/` | git 规范：格式怎么确定(项目约定 → 历史推断 → 通用)、分支、提交、PR、不带 AI 署名 | `git.branch`<br>`git.commit`<br>`git.types`<br>`git.pr` | `branch`：{prefix}{type}/{issue}-{slug}<br>`commit`：{type}: {summary}<br>`types`：bug fix，feature feat<br>`pr`：{summary} |
| `coding.md` | — | 代码规范：给目标项目写代码时的通用最低要求 | `boundaries.changeCap` | `changeCap`：files 10，lines 400 |

## 统一的控制字段

每个调用点按控制键取下面的字段；全局缺省在 `controls.*`，各阶段、模块、小步骤只写与上级不同的。

| 字段 | 全局缺省 |
|---|---|
| `model` | opus |
| `modelWhen` | 无 |
| `fallback` | sonnet |
| `timeout` | 10m |
| `idle` | 5m |
| `turns` | 15 |
| `inputTokens` | 50000 |
| `outputTokens` | 16000 |
| `rounds` | 1 |
| `access` | read |
| `network` | false |
