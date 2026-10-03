# 流水线层：collect、aggregate

本篇描述流水线的前两个模块：`collect` 调用探针产出信号，`aggregate` 把信号归并成问题并维护问题状态。业务规则见 `docs/explanation/design/01-collect.md`、`02-aggregate.md`，单独运行的约定见 `15-standalone-run.md`，探针本身见 `04-probes.md`。编号、枚举、实体、表与路径沿用 `01-foundation.md`。

## 1. 公共约定

### 1.1 模块内部结构

两个模块都按 00 篇 3.3 组织：

| 部分 | collect | aggregate |
|---|---|---|
| `service` | 解析参数、检查前置条件、创建运行、依次调用步骤 | 获取全局锁、选出待聚合的运行、依次调用步骤、落库 |
| `steps` | 目标解析、前置条件、调用采集方法、复现检查、写入、交接 | 3.4 的各步，外加落库与交接 |
| `prompts` | 静态巡检与起草接口描述的执行器任务 | 无：聚合不调用 LLM |
| `render` | 运行摘要中的采集片段 | 运行摘要中的聚合片段 |

`skills/<模块>/` 只告诉 agent 调用哪个命令、如何解读结果，不含执行逻辑。

### 1.2 运行与交接文档

| 项 | collect | aggregate |
|---|---|---|
| 运行编号 | 每个探针一次运行：`R-<日期>-<时分秒>-collect-<探针>` | 每次调用一次运行：`R-<日期>-<时分秒>-aggregate` |
| 交接文档 | `collect-<运行编号>`，`subject` 为 `{ "type": "run", "id": <运行编号> }` | 运行级：`aggregate-<本次运行编号>`；问题级：本次变为「新发现」或「回归」的每个问题一份 `aggregate-<问题编号>`，`subject` 为 `{ "type": "problem", "id": ... }` |
| 存放 | `data/runs/<运行编号>/handoff/` | 同左，运行编号为 aggregate 自己的运行 |

交接文档的写入、覆盖与旧版本保留由 `store/files/handoff_files.py` 完成，同时写入 `handoffs` 表。

### 1.3 三种输出模式

| 模式 | collect | aggregate |
|---|---|---|
| 正常 | 写信号、运行、交接文档与数据库 | 写问题、状态变化、交接文档与数据库 |
| `--output <目录>` | 探针照常执行；信号写入 `<目录>/signals.ndjson`，交接文档与原始输出写入该目录；不写数据库，不更新读取位置与已读记录 | 只读数据库，计算得到的 `ChangeSet` 写入 `<目录>/changeset.json`，交接文档写入该目录；不写数据库，不改 Issue 文件与抑制规则 |
| `--dry-run` | 列出将要运行的方法、目标、档位与外部命令(凭证以占位符显示) | 列出将要处理的运行、各运行的信号数与健康检查结果 |

## 2. collect

### 2.1 职责

按命令行或编排层的要求运行一种采集方法(04 篇)，把 `ProbeOutcome` 写入存储：信号、运行记录与覆盖范围、复现检查结果、
平台来源的读取位置、项目探针的状态、静态巡检的待处理疑点。交接文档与运行摘要列出未启用的采集方法(`sources/enabled.py`)。
collect 不判断信号是否构成问题，也不去重。

### 2.2 文件划分

```
core/tightrein/pipeline/collect/
  service.py              CollectService：参数解析后的入口，编排以下步骤
  steps/
    target.py             解析 ProbeTarget：部署记录、--target、--commit、只读 worktree
    preconditions.py      15.6 的前置条件与预算检查
    run_probe.py          按选择器组装参数并调用采集方法；为 static 注入 StaticReviewer 与待处理疑点
    regressions.py        执行本方法对应类型的复现检查，失败的生成回归信号
    persist.py            在一个事务中写入信号、运行、复现检查结果、读取位置、探针状态、待处理疑点与已读记录
    handoff.py            组装 outputs，写交接文档与 signals.ndjson
  prompts/
    tasks.py              组装 static-review、baseline-review、variant-scan、claim-verifier 与 spec-drafter 的执行器任务
    static_reviewer.py    StaticReviewer：实现 sources/static 的 Reviewer 协议
  render/
    summary.py            运行摘要中的采集片段
```

```
skills/collect/
  SKILL.md
  references/
    roles/
      static-review.md
      baseline-review.md
      variant-scan.md
      spec-drafter.md
```

| 文件 | 内容要点 |
|---|---|
| `SKILL.md` | 何时运行哪种采集方法与档位；命令与参数；前置条件不满足时的提示与应先执行的命令；如何读 `collect-<运行编号>.json` 的 `outputs`(信号数、证据不足的主张、待处理疑点、未执行的复现检查、未启用的方法)；项目探针的 new、test；不得修改复现检查与采集配置 |
| `references/roles/static-review.md` | 增量审查角色的说明：输入是 diff 范围、确定性工具结果、预取的缺陷模式与已接受取舍的摘要；逐条执行缺陷模式中的检出方法并使用 `differential-review`；输出候选主张，命中取舍的写入 `excluded`；只报告位置、疑似问题与触发条件，不下结论 |
| `references/roles/baseline-review.md` | 基线审查角色的说明：输入是一批文件、其中的确定性工具结果与预取知识；审查现有代码本身而不是 diff，按列出的检查项找疑点，每条带严重度；不报风格与重构建议；输出格式同上 |
| `references/roles/variant-scan.md` | 全量扫描角色的说明：以一个缺陷模式为种子使用 `variant-analysis` 查找同类实例，输出格式同上 |
| `references/roles/spec-drafter.md` | 起草接口描述的角色说明：只读，读路由与控制器代码，输出 OpenAPI 3 文档、路由的来源文件与需要用户确认的地方 |

除 `roles/` 下由核心加载的角色说明外不另写参考文件：命令、参数与交接文档的读法已写在 `SKILL.md` 中；各方法的检查项与扩展点分工见 architecture/04、10 与 `docs/reference/methods.md`，项目探针的写法见 `docs/how-to/write-project-probe.md`(17b 按精简原则取舍)。`claim-verifier` 的角色说明不在本 skill 中复制，直接使用 `skills/triage/references/roles/claim-verifier.md`。

### 2.3 命令行

```
tightrein collect --probe <方法> [--level <档位>] [--select <选择器>]
                   [--target <地址>] [--commit <commit>]
                   [--reparse <运行编号>] [--import-archive <目录>]
                   [--no-regressions]
                   [--output <目录>] [--dry-run] [--runner <工具>] [--model <模型>] [--now <时间>]
tightrein collect deployments
```

| 参数 | 作用 |
|---|---|
| `--probe` | 必填：`platform-errors`、`access-log`、`alerts`、`project-probe`、`api-fuzz`、`static`、`incidental` |
| `--level` | 档位，见 04 篇 1.3；缺省取该方法的默认档位 |
| `--select` | 缩小范围(都可重复)：`role:<角色>`、`path:<路径>`(api-fuzz)，`name:<探针名>`(project-probe)，`pending:<疑点编号>\|low`(static，只取证待处理清单中选中的疑点) |
| `--target`、`--commit`、`--now` | 15.8 的外部依赖替换 |
| `--reparse <运行编号>` | 不调用外部工具，重新解析该运行 `raw/` 下的原始输出(api-fuzz) |
| `--import-archive <目录>` | 仅 incidental：从迁移归档一次性导入 |
| `--no-regressions` | 本次不执行复现检查 |
| `--output`、`--dry-run` | 1.3 |
| `--runner`、`--model` | 仅 static 的 agent 调用使用；`replay` 回放录制的审查与取证结果 |

`collect deployments` 只做部署检测：经扩展点 `deploy-source`(10 篇 3.9，`pipeline/common/deploys.py`)只读读取部署记录，按 commit 写入或更新 `deployments`，不运行任何探针；编排层每次运行的第 2 步调用它(09 篇 3.1)。没有配置部署来源时不查询，命令与编排的运行说明记为「未配置部署来源」；探针运行的目标版本为空(没有 `--commit` 时)，`notes` 中同样写明。

`--input` 对 collect 不适用：collect 是流水线的起点，没有上游交接文档；需要以既有材料单独运行时使用 `--reparse`。传入 `--input` 时报错并给出这一说明。

### 2.4 前置条件

| 探针 | 条件 | 不满足时 |
|---|---|---|
| api-fuzz | `target.healthcheck` 在 `target.healthTimeoutSeconds` 内返回 2xx | 跳过本次采集：运行记为 `blocked`，不产出信号，交接文档 `status` 为 `blocked`，提示「staging 不可用」；没有配置 `healthcheck` 时不检查，`notes` 中写明「未配置 target.healthcheck，未做健康检查」 |
| api-fuzz | 有目标地址(`target.baseUrl` 或 `--target`)；没有 `accounts` 时以匿名身份运行(04 篇 1.6)；生产环境只测 GET 与允许清单(04 篇 2.4) | 没有目标地址时由方法返回 `skipped` 并写明原因，不视为错误；生产环境的限制配置不对时启动报错 |
| api-fuzz | `spec-export` 有实现(10 篇 1.4) | 运行记为 `skipped`，原因「未提供 spec-export 扩展」 |
| api-fuzz | `data/specs/<release>/openapi.json` 已缓存且缓存键一致，或只读 worktree 的 HEAD 等于 `release` | 「先执行 `tightrein worktree sync --commit <release>`」 |
| static | 只读 worktree 的 HEAD 等于 `origin/main`(经 `vcs` 只读查询) | 「先执行 `tightrein worktree sync`」 |
| static | `budget_usage` 中 `collect` 当天的累计费用未超过 `stages.collect` 的上限 | 「今日静态巡检预算已用尽」 |
| platform-errors、access-log、alerts、project-probe | 已配置(04 篇第 4 节) | 运行记为 `skipped`，原因「未启用：…」，不视为错误 |
| incidental | 无 | — |

`--reparse` 时不做健康检查与 worktree 检查。健康检查的结果写入运行的 `environment_detail` 与运行摘要。

### 2.5 处理步骤

| 步骤 | 函数 | 说明 |
|---|---|---|
| 1 | `steps.target.resolve(args)` | `release` 取部署来源(`deploy-source`)中最近一次成功部署，并以 commit 为键写入 `deployments`；`--commit`、`--target` 覆盖 |
| 2 | `steps.preconditions.check(probe, target)` | 2.4；不满足时写运行与交接文档后结束 |
| 3 | `service.start_run()` | 在 `runs` 插入 `status=running`，开启 trace |
| 4 | `steps.run_probe.execute(probe, target, level, options)` | static 时注入 `StaticReviewer`；`--reparse` 时传入 `from_raw` |
| 5 | `steps.regressions.execute(probe, target)` | api-fuzz 执行 `api` 类，static 执行 `static` 类(页面类只在验证环节运行)；只选完成的 Issue；失败的产出回归信号并入本次信号 |
| 6 | `steps.persist.commit(outcome, results)` | 一个事务内：批量插入信号；更新运行的 `ended_at`、`status`、`coverage`、`level`、`environment_detail`；更新 `regressions`；保存 `source_cursors`、`probe_states`、`pending_claims`、`incidental_sources` |
| 7 | `steps.handoff.write(run, outcome)` | 写 `signals.ndjson` 与交接文档；`render.summary` 生成运行摘要片段(含未启用的方法) |

**静态巡检的执行器任务**(`prompts/tasks.py`)

| 任务 | `instructions` | 上下文 | `outputSchema` | `access` |
|---|---|---|---|---|
| 增量审查 | `roles/static-review.md`，加载 `differential-review`、`sharp-edges` | diff 范围、改动文件、确定性工具结果；`retrieval.context_for` 预取的缺陷模式与已接受取舍的编号与摘要 | `runner/roles/static-review.schema.json` | `read-only` |
| 基线审查 | `roles/baseline-review.md`，加载 `sharp-edges` | 一批文件与行数、本批的确定性工具结果；按本批文件预取的缺陷模式与已接受取舍 | 同上 | `read-only` |
| 全量扫描 | `roles/variant-scan.md`，加载 `variant-analysis` | 一个缺陷模式的全文、已接受取舍的摘要 | 同上 | `read-only` |
| 取证 | `skills/triage/references/roles/claim-verifier.md` | 只有主张与位置 | `runner/roles/claim-verifier.schema.json` | `read-only` |
| 起草接口描述(`tightrein spec draft`) | `roles/spec-drafter.md` | 被测地址 | `runner/roles/spec-drafter.schema.json` | `read-only` |

增量审查与基线审查都用强档(`roleCapabilities.static-review`、`baseline-review` 为 `strong`)：增量审查是新代码缺陷的主要发现者。

各类任务的 `workdir` 都是只读 worktree，`allowedCommands` 为只读 git 命令，`limits` 取 `stages.collect`；会话记录写入 `transcripts/static-review-<运行编号>.jsonl`、`transcripts/baseline-review-<批次序号>-<运行编号>.jsonl`、`transcripts/variant-scan-<运行编号>-<模式编号>.jsonl`、`transcripts/claim-verifier-<运行编号>-<序号>.jsonl`。

### 2.6 读写的表与文件

| 对象 | 读 | 写 |
|---|---|---|
| `runs` | 上一次成功的 static 运行的 `target_commit` | 本次运行 |
| `signals` | — | 本次信号 |
| `deployments` | 最近一次成功部署 | 新发现的部署 |
| `issues`、`regressions` | 完成的 Issue 与其检查 | 检查的最近结果 |
| `handoffs` | incidental 的来源 | 本次交接文档索引 |
| `source_cursors`、`probe_states`、`pending_claims`、`incidental_sources` | 读取位置、探针状态、待处理疑点与已读记录 | 同左 |
| `knowledge_meta`、`knowledge_fts` | 经 `retrieval` 预取缺陷模式与已接受取舍 | 命中次数(由 `retrieval` 记录) |
| `budget_usage` | 当天累计 | 由 `runner` 记录本次费用 |
| 文件 | `project.yaml`、`e2e/`、`regressions/`、`data/specs/`；经 `extensions` 调用的扩展(工作区 `extensions/` 与 `extensions/stacks/<技术栈>/`) | `data/runs/<运行编号>/raw/`(含 `raw/extensions/`)、`transcripts/`、`handoff/`、`signals.ndjson`；`data/specs/<commit>/`；`data/reports/run-<运行编号>.md` |

### 2.7 交接文档 outputs

| 字段 | 说明 |
|---|---|
| `probe`、`level` | 探针与档位 |
| `target` | `environment`(取 `target.environment`，缺省 `staging`)、`baseUrl`、`release`、`worktreeHead` |
| `runStatus` | `RunStatus` 取值 |
| `environment` | `health`(状态码与耗时)、`failedRoles`、`reportComplete` |
| `coverage` | 各项的数量，完整清单在 `runs.coverage` |
| `signalsFile` | `signals.ndjson` 的相对路径 |
| `signalCount`、`signalsByCheck` | 信号总数与按 `check` 的计数 |
| `stats` | 方法的统计，例如 `insufficientClaims`、`pendingLow`、`pendingOverLimit`、`excludedByTradeoff`、`unlocated`、`unjudgedAuthFailures`、`unparsedLines`、`issues`、`excludedAlerts`、`seed`；另有 `extensions`：本次用到的每个扩展点的实现层(`project`、`stack`、`default`)与是否命中缓存 |
| `regressions` | 每条检查的 `issue`、`checkId`、`result`(`passed`、`failed`、`not-run`、`invalid`) |
| `disabledSources` | 未启用的采集方法及原因 |
| `rawDir` | 原始输出目录 |
| `skippedReason` | `skipped` 时的原因 |
| `notes` | 探针的 `notes` |

`nextAction` 固定为「交给 aggregate」；`blocked` 与 `failed` 时写明原因与建议执行的命令。

### 2.8 幂等与重跑

- 每次采集都是一次新的观察，生成新的运行与信号，不覆盖以前的运行；重复的现象由 `aggregate` 按指纹归并。
- 信号、运行结果与读取位置在同一个事务中提交：进程在第 6 步之前中断时数据库中没有该运行的任何信号，读取位置、探针状态与已读记录不前进，下次重读；遗留的 `running` 运行在下一次 collect 启动时超过 `stages.collect` 的时间上限即改为 `failed`。
- `--reparse <运行编号>`：该运行尚未被聚合(`aggregated_at` 为空)时，在一个事务中替换其信号与覆盖范围，交接文档按 15.7 覆盖并保留旧版本；已被聚合时新建一次运行承载重新解析的结果，并提示执行 `tightrein aggregate --rebuild`。
- 复现检查的结果按「Issue 编号 + 检查编号」更新，不追加。

### 2.9 错误处理

| 情况 | 处理 |
|---|---|
| 前置条件不满足 | 2.4；进程退出码非零，交接文档 `blocked` |
| 探针返回 `failed` | 运行记为 `failed`，不写信号；交接文档 `failed`，`blockedReason` 取探针原因；本机通知附日志路径 |
| 探针返回 `partial` | 写入已产出的信号，覆盖范围只含完成的部分；运行摘要列出缺失的部分 |
| 扩展返回错误或超时 | 由探针按 04 篇各节的错误处理决定 `failed` 或 `partial`；扩展的错误码、`message`、`hint` 写入 `notes`，运行摘要按「扩展点、实现层、错误码」列出 |
| 事务失败 | 回滚，运行记为 `failed`，原始输出保留，可用 `--reparse` 恢复 |
| 执行器 `schema-invalid`、`limit-reached` | 由探针按 04 篇 5.6 处理；每次调用写一条 `invoke_agent` 事件 |

每一步写一个 span；关卡判定(前置条件)写 `gate` 事件，`decision` 与 `reason` 写明判定依据。

### 2.10 测试

| 对象 | 方式 |
|---|---|
| `steps.target`、`steps.preconditions` | 替身 `vcs` 与健康检查；覆盖部署记录缺失、`--commit` 覆盖、worktree 不在目标、预算用尽 |
| `service` | 以实现 `Probe` 协议的假探针运行，临时数据库；断言信号、运行、覆盖范围与交接文档一致；断言 `partial` 与 `failed` 的写入差别 |
| 事务与中断 | 在第 6 步中途注入异常，断言没有信号写入、读取位置未前进 |
| 回放 | `tests/replay/collect/<用例>/`：录制的原始输出加 `--runner replay` 的审查与取证结果，断言产出的 `signals.ndjson` 与交接文档；static 的全部 agent 路径在此覆盖 |
| `--output` | 运行后数据库文件的校验和不变，输出目录中有完整的信号与交接文档 |
| `--dry-run` | 输出中不含任何凭证，数据库与文件系统无变化 |

## 3. aggregate

### 3.1 职责

把尚未聚合的采集运行及其信号归并成问题(redesign/02-aggregate.md)：去重、复现确认、跟踪状态、抑制误报，把本次变为「新发现」或「回归」的问题交给分诊。聚合是确定性的：不调用 LLM；同样的数据库状态与输入得到同样的 `ChangeSet`。人工操作问题的命令(2.9)也由本模块实现，因为它们与聚合共用问题状态机与抑制规则。

### 3.2 文件划分

```
core/tightrein/pipeline/aggregate/
  service.py              AggregateService：全局锁、选择运行、按运行依次执行各步、落库、交接
  changeset.py            ChangeSet：本次要写入的全部变化(运行登记、信号字段、问题、事件、别名、Issue 副作用)
  steps/
    select.py             选出待聚合的运行与其信号
    normalize.py          第 1 步 规范化
    suppress.py           第 2 步 抑制规则
    group.py              第 3 步 指纹与归并
    reproduce.py          第 4 步 复现确认
    status.py             第 5 步 状态更新
    output.py             第 6 步 组装交接文档
    apply.py              在一个事务中把 ChangeSet 写入数据库，执行副作用
  commit_facts.py         经 vcs 只读查询 commit 的先后关系，交给纯函数使用
  manual.py               ignore、false-positive、merge、reopen
  rebuild.py              整体重放
  render/
    summary.py            运行摘要中的聚合片段
```

```
skills/aggregate/
  SKILL.md
```

aggregate 是确定性的，不调用执行器，没有角色说明；命令、交接文档的读法与约束都写在 `SKILL.md` 中，不另写参考文件(17b 按精简原则取舍)。

| 文件 | 内容要点 |
|---|---|
| `SKILL.md` | 何时运行聚合；命令与参数；如何读运行级与问题级交接文档；「没有新信号」的含义；问题的四种状态与待确认 |

### 3.3 命令行与前置条件

```
tightrein aggregate [--select <选择器>] [--input <collect 交接文档>] [--output <目录>] [--dry-run]
                     [--reproduce live|skip] [--rebuild] [--no-wait] [--target <地址>] [--now <时间>]
tightrein ignore <问题> --reason <原因> [--until <条件>]
tightrein false-positive <问题> --reason <原因> [--expires <日期>]
tightrein merge <问题A> <问题B>
tightrein reopen <问题>
```

| 参数 | 作用 |
|---|---|
| `--select` | `run:<运行编号>` 只处理该运行；`probe:<方法>` 只处理该方法的待聚合运行；缺省处理全部待聚合运行 |
| `--input` | 给一份 collect 交接文档，从其 `signalsFile` 读取信号、从 `outputs` 读取运行信息，不从数据库取运行；数据库中的既有问题照常只读使用 |
| `--output`、`--dry-run` | 1.3 |
| `--reproduce` | 复现确认是否实际重放请求：正常模式缺省 `live`，`--output` 模式缺省 `skip`(需要重放的问题保持「待确认」) |
| `--rebuild` | 整体重放(3.6) |
| `--no-wait` | 全局锁被占用时立即退出，而不是排队等待 |
| `--target`、`--now` | 重放请求的目标地址；判断到期条件所用的当前时间 |

`--runner`、`--model` 对聚合无效，传入时忽略并提示。

**前置条件**：存在 `aggregated_at` 为空的 collect 运行(没有信号的 `blocked` 运行只记为已聚合)，或存在需要重新判定的「待确认」问题。都不满足时输出「没有新信号」，交接文档不生成，退出码为 0。

### 3.4 处理步骤

`service` 获取 `data/aggregate.lock` 后，按运行的 `started_at` 升序逐个处理待聚合运行：先登记运行(`ChangeSet.register`，
计数从这里开始，运行记为已聚合)，再执行以下各步。各步只读数据库、只修改内存中的 `ChangeSet`，第 4 步可能发起网络请求；
完成后由 `apply.py` 在一个事务中写入，再写交接文档。健康检查不通过的采集已被跳过(2.4)，聚合不再做环境判定。

| 步骤 | 函数 | 所用的 domain 纯函数 | 做法 |
|---|---|---|---|
| 1 规范化 | `steps.normalize.apply(signals)` | `normalize.normalize(text, rules)`、`normalize.location(location, probe, rules)` | 规则为内置默认规则加 `normalize.yaml`，由 `config` 读取；结果写入信号的 `normalized_message` 与规范化后的定位 |
| 2 抑制 | `steps.suppress.apply(signals)` | `suppression.match(signal, rules, now)` | 规则来自 `suppressions.yaml`，到期规则不参与匹配；命中的信号 `suppressed=true`、`aggregate_state=done`，不再往下走 |
| 3 指纹与归并 | `steps.group.apply(signals)` | `fingerprint.fingerprint(signal, version)`、`problem.title_for(signal)`、`problem.apply_occurrence(problem, signal)` | 来自错误追踪、监控平台的信号直接用平台分组编号(`context.platformGroup`，如 `sentry:<组织>/<编号>`、`alertmanager:<编号>`)作指纹；其余按逻辑位置计算(不用行号)，项目探针为探针名与给出指纹的哈希。先查 `problems.fingerprint`，再查 `problem_aliases`；存在则追加出现，否则新建 `pending` 问题；回归信号(`check=regression`)不计算指纹，直接归到 `context.targetFingerprints` 对应的问题 |
| 4 复现确认 | `steps.reproduce.apply(problems)` | `reproduce.strategy(probe, check)`、`reproduce.judge_replays(results)` | 能确定性重放的重放一次确认：api-fuzz 的 `not_a_server_error` 经 `sources.api_fuzz.replay` 重放 `reproduceAttempts` 次(目标不可用时保持待确认、下次重试)；其余首次观察即有效。重放未复现的问题保持 `pending` 并标记 `intermittent`，之后不再重放；以后某次运行再次出现时 `promoted` 为 `new` |
| 5 状态更新 | `steps.status.apply(problems, run)` | `problem.transition(state, event, context)`、`problem.is_covered(problem, run)`、`problem.resolution_ready(problem, run, facts, thresholds)`、`problem.is_regression(problem, release, facts)`、`problem.ignore_expired(problem, now, facts)` | 本次出现的问题：再次出现、回归(只认比解决时更新的版本)、忽略的恢复条件；本次运行覆盖范围内而未出现的问题：累计 `clean_covered_runs`，满足条件时判为已解决；static 在新 commit 上覆盖而未命中即判为已解决；平台来源与项目探针的问题在其来源(`scope.source`)出现在运行的 `coverage.sources` 中时算被覆盖 |
| 6 输出 | `steps.output.build(changeset)` | — | 组装运行级与问题级交接文档(3.8)；`render.summary` 生成运行摘要片段 |

问题只有四种状态：新、持续、已解决、回归；另有复现确认前的「待确认」与用户的处置(已忽略、误报、已并入其他问题)。

**commit 的先后关系**：第 5 步需要判断「当前部署的 commit 晚于问题最后出现时的 commit」「出现在比解决时更新的版本」。纯函数不做 IO，由 `commit_facts.py` 在第 5 步之前经 `vcs` 只读查询本次涉及的 commit 对的祖先关系，以 `CommitFacts` 传入。查询不到的 commit(例如本地尚未同步)视为关系未知，相关问题本次不做解决或回归判定，在 `notes` 中列出。

**状态转换的副作用**：`transition` 返回的副作用由 `apply.py` 在同一事务中执行。

| 副作用 | 执行 |
|---|---|
| 写事件 | `problem_events` 插入一行，带 `run_id` 与 `detail` |
| Issue 重新打开(4.7：关联问题回归) | 经 `store/files/issue_files.py` 把 Issue 文件的 `status` 改为 `todo`，「历史」一节追加回归时的 commit 与信号编号，同步 `issues` 表 |
| 已有 Issue 的问题再次出现 | 只更新次数与最近出现时间，不进入交给分诊的清单 |

非法转换抛出 `InvalidTransition`，整个运行的处理中止(3.10)。

### 3.5 人工操作

`manual.py` 中的每个操作都获取全局锁，以 `ChangeSet` 表达变化，由 `apply.py` 写入，事件的 `operation` 为 `user_action`。

| 命令 | 变化 |
|---|---|
| `ignore` | 问题转为 `ignored`，`ignore_until` 写入恢复条件(日期、再出现 N 次、出现在新版本)；不给条件即永久 |
| `false-positive` | 问题转为 `ignored`；在 `suppressions.yaml` 追加一条以指纹匹配的规则，写明原因、添加日期、到期日期(缺省取 `thresholds.suppressionDays`) |
| `merge` | B 的信号关联改到 A，B 的指纹写入 `problem_aliases` 指向 A，B 标记为已合并并写事件；B 已有 Issue 时拒绝，提示先按 4.7 处理 Issue |
| `reopen` | `resolved` 或 `ignored` 的问题转为 `new`，清除 `ignore_until` 与 `clean_covered_runs` |

`suppressions.yaml` 的读写由 `store/files/suppressions.py` 完成，写入前备份上一版本，写入后重新校验。

### 3.6 整体重放

`tightrein aggregate --rebuild` 用于指纹规则版本变化或已聚合运行被重新解析之后：

1. 获取全局锁，把数据库文件复制到 `data/archive/tightrein-<时间>.db`。
2. 读出现有问题的编号、指纹、信号集合、`issue_id`，以及 `problem_events` 中的人工操作、分诊与 Issue 关联事件、复现确认的结果。
3. 在一个事务中清空 `problems`、`problem_signals`、`problem_aliases`、`problem_events`，把全部 collect 运行的 `aggregated_at` 与信号的聚合字段复位。
4. 按运行的 `started_at` 升序逐个执行各步；复现确认不发起网络请求，使用第 2 步读出的复现结果，没有记录的保持 `pending`；人工操作与分诊、Issue 关联事件按原时间点重新施加。
5. 编号继承：每个新问题继承与之信号重合最多的旧问题的编号与 `issue_id`；一个旧问题拆成多个新问题时，信号最多的一个继承，其余分配新编号并写 `rebuilt` 事件；未被继承的旧编号不再使用。
6. 提交事务，写运行级交接文档，其中列出编号的继承关系与拆分、合并情况。

重放与增量聚合遵守同一套纯函数，结果一致(3.11)。`--rebuild` 可以与 `--output`、`--dry-run` 组合，先查看重放结果再落库。

### 3.7 读写的表与文件

| 对象 | 读 | 写 |
|---|---|---|
| `runs` | 待聚合的 collect 运行及其覆盖范围 | collect 运行的 `aggregated_at`；本次 aggregate 运行 |
| `signals` | 待聚合运行的信号 | `normalized_message`、`fingerprint`、`suppressed`、`aggregate_state` |
| `problems`、`problem_aliases` | 按指纹与别名查找 | 新建与更新；`merge` 写别名 |
| `problem_signals`、`problem_events` | 历史出现与事件 | 追加与更新 |
| `triage_results` | 问题是否已分诊(决定「新发现 → 持续」) | — |
| `issues` 与 Issue 文件 | 问题关联的 Issue 状态 | 回归时重新打开 |
| `handoffs` | — | 本次交接文档索引 |
| 文件 | `project.yaml` 的 `thresholds`、`normalize.yaml`、`suppressions.yaml` | `suppressions.yaml`(仅 `false-positive`)；`data/runs/<运行编号>/handoff/`；`data/reports/run-<运行编号>.md`；`data/archive/`(仅重放) |
| 锁 | `data/aggregate.lock` | 同左 |

### 3.8 交接文档 outputs

**运行级**(`aggregate-<本次运行编号>`)

| 字段 | 说明 |
|---|---|
| `processedRuns` | 每个被处理的 collect 运行：`runId`、`probe`，以及 `grouped`、`suppressed` 的信号数 |
| `counts` | 本次转为 `new`、`regressed`、`resolved`、`ongoing` 的问题数，以及仍为 `pending` 的问题数 |
| `forTriage` | 本次变为「新发现」或「回归」的问题编号，每个附问题级交接文档的路径 |
| `statusChanges` | 每个状态变化：`problemId`、`from`、`to`、`event` |
| `reopenedIssues` | 本次因回归重新打开的 Issue |
| `rebuild` | 仅重放：编号继承、拆分与合并的清单 |
| `notes` | commit 关系未知、重放失败等需要说明的事项 |

**问题级**(`aggregate-<问题编号>`，分诊的输入)

| 字段 | 说明 |
|---|---|
| `problem` | `id`、`title`、`probe`、`status`、`fingerprint`、`fingerprintVersion`、`occurrences`、`firstSeenAt`、`lastSeenAt`、`firstSeenRelease`、`lastSeenRelease`、`resolvedRelease`、`intermittent` |
| `transition` | 本次的 `from`、`to`、`event` |
| `latestSignal` | 最近一条信号的完整内容 |
| `samples` | 同一问题下不同角色或参数的信号，最多 5 条 |
| `reproduction` | 复现确认的方式、次数与结果 |
| `issueId` | 回归时为已有 Issue 编号，否则为空 |

`nextAction`：运行级为「交给 triage」或「无需分诊」；问题级为「交给 triage」。

### 3.9 幂等与重跑

- 一个 collect 运行只会被聚合一次：`apply.py` 在写入 `ChangeSet` 的同一事务中设置其 `aggregated_at`，信号的 `aggregate_state` 同时改为终态，所以重跑时同一运行不会再被选中，同一信号不会被重复归并。
- 进程在事务提交前中断：数据库没有任何变化，重跑得到同样的结果。
- 进程在事务提交后、交接文档写完前中断：本次 aggregate 运行保持 `running`。下次启动时先检查这类运行，按 `problem_events.run_id` 从数据库重新生成其交接文档，再处理新的运行。
- 交接文档按「模块 + 对象」覆盖，旧版本保留为 `<文件名>.<序号>.json`。
- 需要改变既有结论(指纹规则调整、重新解析)时只能走 `--rebuild`，不对已聚合的运行做局部重算。

### 3.10 错误处理

| 情况 | 处理 |
|---|---|
| 全局锁被占用 | 缺省排队等待，超过 `stages.aggregate` 的时间上限后退出；`--no-wait` 立即退出 |
| `normalize.yaml` 或 `suppressions.yaml` 不合格 | 开始前校验，报出文件、位置与原因，不做任何处理 |
| 重放请求时目标不可用或登录失败 | 问题保持 `pending`，下次聚合重试；不作为运行失败 |
| `InvalidTransition` | 中止当前运行的处理，已提交的前序运行不受影响；本次 aggregate 运行记为 `failed`，事件带 `error_type` 与问题编号 |
| 事务失败 | 回滚，运行记为 `failed`，下次重跑 |
| Issue 文件写入失败 | 事务回滚(文件写入在提交前完成，失败时恢复原文件)，运行记为 `failed` |

每一步写一个 span，复现确认的结论写 `gate` 事件。

### 3.11 测试

| 对象 | 方式 |
|---|---|
| domain 纯函数 | `suppression`、`reproduce`、`fingerprint`、`problem` 的单元测试：每条规则的成立与不成立，边界值(3 次覆盖运行)，平台分组编号作指纹 |
| 各步骤 | 以内存中的运行、信号与问题构造输入，断言得到的 `ChangeSet`，不接触数据库 |
| `apply.py` | 临时数据库：`ChangeSet` 完整写入、失败时整体回滚、副作用与事件一致 |
| `service` | 多个运行按时间顺序处理；`blocked` 运行只记为已聚合；已有 Issue 的问题再次出现不交给分诊 |
| 回放 | `tests/replay/aggregate/<用例>/`：输入为 collect 交接文档、`signals.ndjson`、数据库种子与录制的重放结果，期望为 `changeset.json` 与交接文档；以 `--input` 与 `--output` 运行后逐字段比较 |
| 重放一致性 | 同一批运行分别做增量聚合与 `--rebuild`，问题与状态一致，编号全部继承 |
| `--output` | 运行后数据库文件的校验和不变，`suppressions.yaml` 与 Issue 文件不变 |
| 人工操作 | 四个命令的状态变化、事件与抑制规则；`merge` 后 B 指纹的新信号归到 A |
| 中断恢复 | 在事务提交后、交接文档写入前注入中断，断言下次启动补写交接文档 |

本篇用到的基础层定义(编号、枚举、表、路径、配置)统一见 01-foundation.md。
