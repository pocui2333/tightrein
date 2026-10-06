---
name: triage
description: 对 tightrein 聚合出的新发现与回归问题做分诊取证：判断是否成立、根因在哪、谁引入、值不值得修、多大规模，给出处理标签并定出去向；查看发现报告、处理人工队列、补充信息后重新分诊或改判。聚合之后、提 Issue 之前使用。
---

# triage

分诊只读代码、只出结论，不改代码、不对外发布，也不在运行中向用户提问。判断全部由 `tightrein triage` 完成：取证角色一次给出判定、证据、严重度、价值判断、任务类型、预估规模与修复方向；本工具用代码检查证据，高风险时做证伪复核，再按决策树给出处理标签并定去向。本 skill 只说明何时调用哪个命令、如何读结果，不自行判断问题是否成立。

## 命令

```
tightrein triage [--limit <n>] [--commit <commit>] [--select <问题编号>] [--dry-run]
                  [--input <aggregate 交接文档> --output <目录> [--ignore-state]] [--runner <工具>] [--model <模型>]
tightrein problem retriage <问题编号> [--note <补充信息>]
tightrein problem retriage <问题编号> --verdict <判定> --reason <原因> [--severity <P0-P3>] [--disposition <去向>]
tightrein triage queue
```

- `triage`：分诊待分诊的问题，每次最多 `thresholds.triage.perRun` 个，按「越权 → 5xx → 回归 → 其他运行时问题 → 静态巡检与任务外发现」排序，其余留到下一次。取证在 `origin/<主分支>` 的最新 commit 上进行(`--commit` 覆盖)。
- `problem retriage <问题>`：对已分诊的问题重新分诊；`--note` 把用户补充的事实加进主张。
- `problem retriage <问题> --verdict`：用户改判，不调用任何角色；判定取 `confirmed`、`conditional`、`refuted`、`insufficient`。
- `triage queue`：列出人工队列中的问题、缺少的信息与发现报告路径。
- 不确定会做什么时先加 `--dry-run`，它只列出选中的问题与将调用的角色。

## 解读结果

读 `data/runs/<运行编号>/handoff/triage-<问题编号>.json` 与发现报告 `data/findings/<问题编号>.md`：

| `outputs.verdict` | 含义 |
|---|---|
| `confirmed` | 确认成立，能说清触发条件 |
| `conditional` | 只在某个角色、数据状态或时序下成立 |
| `refuted` | 不成立，报告中写明挡住它的代码或现象的真实来源 |
| `insufficient` | 证据不足，`missingInfo` 写明缺什么 |

| `outputs.treatment` | 处理标签(决策树 `triage.treatment.rules`，项目可覆盖) |
|---|---|
| `immediate` | 立即修：提 Issue，运行摘要置顶 |
| `scheduled` | 排期修：提 Issue |
| `observe` | 观察：留在问题列表，再出现或严重度升级时重新分诊 |
| `wont-fix` | 不修：忽略 |

| `outputs.disposition` | 下一步 |
|---|---|
| `create-issue` | 由 `tightrein issue create` 自动建 Issue；P0 在运行摘要中置顶 |
| `awaiting-deploy` | main 上已修复，等部署后由聚合判定已解决 |
| `problem false-positive` | 判为误报并生成带到期日期的抑制规则 |
| `accepted-tradeoff` | 命中已接受的取舍，已忽略 |
| `deferred` | 暂不修，再出现若干次或严重度升级时重新分诊 |
| `manual-queue` | 需要用户：交接文档 `status` 为 `blocked`，`blockedReason` 写明原因 |

- `taskType`(缺陷、安全、数据、前端、功能、重构、依赖、文档配置)与 `sizeTier`(按 `estimate` 的预估文件与行数，不含测试，门槛 `thresholds.tiers`)决定修复走哪条通道；`worth` 是价值判断与修复方向。
- `mergedInto` 不为空：查重判为同一根因，已并入该问题，没有单独的结论。
- `flags` 中为真的项(根因在设计本身、要动数据结构或存量数据、会改变公共实现或接口契约)修复前须由用户定夺。
- `scores` 是分诊评分表五项的代码检查结果；`attempts` 是各角色的调用次数与状态。
- `report` 是取证角色写给维护者的 Issue 报告(按 `project.language` 书写)，`severity` 优先取 `report.severity`；为 null(不成立、证据不足或本功能之前的结论)时严重度按影响类别映射。
- 取证输出中只写文件名的代码位置由本工具按代码快照补全；补不全(不存在或有多个同名文件)时交回重做。
- `status` 为 `failed` 是程序错误，`blockedReason` 有异常摘要，修正后用 `problem retriage` 重跑。

## 人工队列

问题进入人工队列的原因有三种：证据不足(常见是只有用户知道的信息，如业务量级、现象细节)、证据检查重做后仍不合格、证伪复核与取证不一致。证伪复核只在高风险时运行(`triage.refute`：严重度 P0、P1，安全类，权限与数据归属类；预估为 P0 而判为不成立时也运行)。处理方式二选一：

1. 补充信息后重新分诊：`tightrein problem retriage <问题> --note "<补充的事实>"`。
2. 直接改判：`tightrein problem retriage <问题> --verdict <判定> --reason "<依据>"`。原来的结论保留；原结论为不成立而改判为成立时，会记为「误判为不成立」。

每周的运行摘要会汇总自动判为误报的问题，抽查发现判错时同样用 `problem retriage --verdict` 改判。

## 约束

不修改代码与只读 worktree，不手写分诊结论或发现报告，不编辑 `suppressions.yaml`；判断以命令给出的结论为准。

## 参考资料

- `references/evidence-standard.md`：分诊各角色共用的取证底线，由本工具附在取证任务说明中；判断一条结论是否可信时阅读。
- `references/severity.md`：P0 到 P3 的含义与取证输出 `report`(Issue 标题、问题、触发步骤、期望与实际、验收标准、严重度与理由)的写法，由本工具附在取证任务说明中，项目在 `project.yaml` 的 `triage.severityGuide` 追加本项目的语境；对严重度有疑问时阅读。
