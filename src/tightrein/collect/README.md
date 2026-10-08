# 采集(collect)

## 是什么

流水线的第一个阶段：收集「疑似问题的线索」(信号)，不做判断；最后一步「去重」就地把信号整理成问题，
交给评估的直接是问题(新发现或回归)。入口 `collect.run(runtime) -> CollectOutcome`，由调度(protocol/schedule)调用。

## 七个来源

| 控制键 | 目录 | 形式 | 项目要做的 | 触发 |
|---|---|---|---|---|
| `collect.project_probes` | `project_probes/` | 方法文档 | 按方法文档写检查脚本并登记 | 每 1h |
| `collect.platform_errors` | `platform_errors/` | 程序 | 选平台(Sentry、Loki)，填地址、查询条件与日志格式，凭据放 `secrets.json` | 每 1h |
| `collect.access_log` | `access_log/` | 程序加方法文档 | 用日志平台的填查询条件；用自己的日志的按方法文档写取数脚本 | 每 1d |
| `collect.alerts` | `alerts/` | 程序 | 填监控系统(Alertmanager)的地址与筛选条件 | 每 15m |
| `collect.api_fuzz` | `api_fuzz/` | 程序(只查 5xx) | 提供接口描述；测生产环境时填允许清单 | 有新部署时 |
| `collect.static` | `static/` | 程序(调用模型) | 选规则集，可加项目规则 | 有新提交时 |
| `collect.incidental` | `incidental/` | 程序 | 无 | 每 1h |

每个来源一个模块 `collect/<来源>/source.py`，提供 `collect(runtime) -> SourceResult`(共用类型在 `common/`)；
除静态巡检外都不调用模型。以后再加 1.8 埋点分析。

## 流程

1. 补写上次中断的去重交接(`dedup.recover`)；
2. 刷新部署记录(接入清单启用了 `release.deploy` 时，`release/deploy.py`)：信号的 release 与「有新部署」的判断靠它；
   读不到时沿用已记的，写进说明；
3. 选出到点的来源(`select`)：接入清单中启用或自定义的 `collect.*`，按 `schedule.every`：
   - 间隔：从 state 表 `collect.schedule:<来源>` 记的上次时间算起；从未跑过的立即跑；错过多个时刻只补跑一次并记下错过次数；
   - `on_commit`、`on_deploy`：仓库 HEAD 或最近一次成功部署的 commit 与上次跑时不同才跑，否则跳过并写明原因；
   - 手动触发(`only`)只跑指定的来源，不看是否到点；
4. 并行运行(`run_sources`)：按 `tightrein.<来源>.source.collect` 动态调用；同时数受
   `resources.concurrency.collectSources` 限制；每个来源一个独立的数据库连接；每个来源的超时取
   `controls.<来源>.timeout`(缺省继承 `controls.collect.timeout`)，超时的记为失败、本轮不再等它；
   来源抛出的 `SourceError` 归类为失败，其余程序错误也只记为该来源失败，不拖垮整轮；
5. 每个来源写一份交接 `data/runs/<运行>/1x-collect.<来源>-handoff.json`(状态、读到条数、窗口、覆盖范围、全部信号)；
6. 去重(`dedup/`，见其 README)：一个事务写入问题、出现、各来源的读取位置与本次的调度记录，写自己的交接；
7. 返回 `CollectOutcome`：新发现与回归的问题编号、各来源结果、没跑的来源与原因、去重结果、说明。

失败的来源不记「已跑过」(调度记录随去重的事务保存，失败的结果不带)，下次醒来重新触发；读取位置也不前进。

## 输入与输出

- 输入：`Runtime`(接入清单、settings、数据库、git)；
- 输出：`CollectOutcome`；problems、occurrences、state 表；运行目录下各来源与去重的交接、去重的变更日志；
  每个送评估的问题目录下一份去重交接。

## 配置

- 接入清单 `setup.json`：每个来源启用、不启用还是自定义；
- `schedule.every.<来源>`：间隔或 `on_commit`、`on_deploy`；
- `resources.concurrency.collectSources`：同时跑几个来源；
- `controls.<来源>.timeout`：来源的超时；各来源自己的参数在 `controls.<来源>`；
- `controls."collect.dedup"`：去重的参数(见 `dedup/README.md`)。

## 设计依据

- 方法文档与程序的划分：需要按项目写具体内容(脚本、规则)的写成方法文档，项目照文档落地(项目探针、自建日志的访问日志)；
  tightrein 用通用程序能完成、项目只需配置的保留为程序。这样 tightrein 不必为每个项目写特例，项目也不必改 tightrein
  (44 号计划「1 采集」)。
- 并行、按事件跳过、每个来源设超时：原来一个接一个地跑，一个卡住拖住整轮；依赖代码或部署的来源在没有变化时跑也白跑
  (44 号计划「采集调度」)。
- 动态调用来源模块：新增来源只加模块与接入清单的一项，不改调度(实现原则 3「通用，不写特例」)。
- 守护线程跑来源：线程池的线程在进程退出时会被等待，卡住的来源会拖住整个进程。
- 每个来源一个数据库连接：SQLite 连接不能跨线程共用；来源只读数据库，写入都在去重的一个事务里。

## 不做什么

- 不判断信号是否成立(评估的事)；
- 不在来源里重试(重试只在 `protocol/limits.py`)；
- 不管能跑的时段与运行锁(调度的事)。
