---
name: collect
description: 运行一种采集方法(platform-errors、access-log、alerts、project-probe、api-fuzz、static、incidental)采集信号并写入 tightrein 的存储；也用于编写与试跑项目探针、起草接口描述。需要读取平台上的运行报错与业务告警、对被测环境做接口巡检、做静态巡检或导入任务外发现时使用；采集完成后交给 aggregate 聚合。
---

# collect

每次调用运行一种采集方法，产生一次独立的运行 `R-<日期>-<时分秒>-collect-<方法>`。collect 只采集，不判断信号是否构成问题，也不去重。
采集只从外部只读观察被测系统：内部错误、访问日志与业务告警只经平台的只读查询 API 取得，采集中不跑页面用例。
每种方法配置了才启用；未启用的方法在交接文档的 `disabledSources` 与运行摘要中列出原因。方法的参数与示例配置见
`docs/reference/methods.md`。

## 命令

```
tightrein collect --probe <方法> [--level <档位>] [--select <选择器>]
                   [--target <地址>] [--commit <commit>] [--reparse <运行编号>] [--import-archive <目录>]
                   [--no-regressions] [--output <目录>] [--dry-run]
tightrein collect deployments
tightrein project probe new <名称> [--every 1h]      # 生成项目探针模板并登记到 sources.project-probes
tightrein project probe test <名称>                  # 单独试跑、校验输出、显示将产出的信号，不进入流程
tightrein project probe logs --query <查询> --since <时间> --until <时间> [--raw]   # 经日志平台取数(供探针调用)
tightrein project spec draft                          # 接口描述没有自动导出时由 AI 起草 openapi.draft.yaml，交用户确认
```

- `--probe`：`platform-errors`(错误追踪与集中日志平台上的运行报错、前端错误)、`access-log`(访问日志的性能与可用性退化，可选)、
  `alerts`(已触发的业务告警)、`project-probe`(工作区的项目探针)、`api-fuzz`、`static`、`incidental`。
- `--level`：api-fuzz 为 `shallow`(缺省)或 `deep`；static 为 `incremental`(缺省)、`full` 或 `baseline`(按目录模块分批整份审查已有代码；第一次静态巡检的 `full` 自动按 `baseline`)。
- `--select`(都可重复)：`role:<角色>`、`path:<路径>`(api-fuzz)；`name:<探针名>`(project-probe，不看间隔)；
  `pending:<疑点编号>|low`(static：只取证待处理清单中选中的疑点，不做审查)。
- `--reparse <运行编号>`：不调用外部工具，重新解析该运行的原始输出(api-fuzz)。
- 生产环境(`target.environment: production`)的 api-fuzz 只测 GET 且只测 `sources.api-fuzz.production.allow` 中的接口，配置不对时启动报错。
- 不确定会做什么时先加 `--dry-run`。`collect deployments` 只检测部署，不运行采集方法。
- 项目探针的写法见 `docs/how-to/write-project-probe.md`，输入输出的字段见 `docs/reference/project-probe.md`。

## 解读结果

读 `data/runs/<运行编号>/handoff/collect-<运行编号>.json`：

- `status` 为 `ok`：`nextAction` 为「交给 aggregate」，接着运行 `tightrein aggregate`。
- `status` 为 `blocked`：按 `blockedReason` 与 `nextAction` 处理后重跑，例如「staging 不可用」(健康检查不通过，本次跳过采集)时先确认环境，「今日静态巡检预算已用尽」时改天再跑。
- `status` 为 `failed`：`blockedReason` 是给出的原因，例如平台返回错误、钥匙串条目不存在；提示「先执行 tightrein project worktree sync」时照做后重跑；数据库写入失败时用 `--reparse <运行编号>` 恢复。
- `outputs.runStatus` 为 `skipped` 时看 `skippedReason`(例如「未启用：…」「没有到期的项目探针」)，这不是错误。
- `outputs.signalCount`、`signalsByCheck`：本次信号数；信号本身在 `signalsFile`。
- `outputs.coverage.sources`：本次读到数据的平台或项目探针；没读到的来源不能作为问题已解决的证据。
- `outputs.stats`：例如规则库(工作区 `rules/`，由 learn 的缺陷变规则维护)的命中数 `ruleHits`(命中直接成为 `rule:<规则编号>` 信号，不经审查与取证)、证据不足的主张 `insufficientClaims`、进入待处理清单的低级疑点 `pendingLow` 与超出上限的 `pendingOverLimit`、无法解析的日志行 `unparsedLines`、排除的基础设施告警 `excludedAlerts`。
- `outputs.notes`：例如「可能漏读」(两次运行的间隔超过了平台保留期)、项目探针本次作废的原因；`--reparse` 了已聚合的运行时需要再执行 `tightrein aggregate --rebuild`。
- `outputs.regressions`：复现检查的结果；`failed` 会产生回归信号，`not-run`、`invalid` 在 `notes` 中说明原因。

## 约束

不修改复现检查(`regressions/`)、规则库(`rules/`)与采集配置；只读 worktree 中不做任何修改；项目探针只从外部只读观察被测系统，不修改数据、不登录服务器。
