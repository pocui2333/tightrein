# 基础层：domain、contracts、store、config、observability

基础层被所有上层依赖，本篇定义全局共用的约定：编号、枚举、实体、状态机、数据库表、文件布局、配置、环境变量、事件日志与外部依赖。其他分篇引用这里的名称，不另行定义。

## 1. 全局约定

### 1.1 编号

| 对象 | 格式 | 例子 | 生成方式 |
|---|---|---|---|
| 运行 | `R-<日期>-<时分秒>-<模块>`；collect 为 `R-<日期>-<时分秒>-collect-<探针>`；编排运行为 `R-<日期>-<时分秒>-loop` | `R-20260929-021503-collect-api-fuzz` | 运行开始时生成；collect 带采集方法名，使同一时刻不同方法的运行不重名 |
| 信号 | `S-<ULID>` | `S-01J9Z3...` | 探针产出时生成，全局唯一且按时间有序 |
| 问题 | `P-<四位序号>` | `P-0042` | 聚合新建问题时递增 |
| Issue | 四位序号 | `0007` | 创建 Issue 时递增；文件名为 `0007-<简称>.md` |
| 待确认操作 | `OP-<四位序号>` | `OP-0015` | 生成待确认操作时递增 |
| 学习建议 | `LS-<四位序号>` | `LS-0021` | `learn` 生成建议时递增(含 `learn improve` 的改进建议) |
| 知识条目 | `<类型前缀>-<四位序号>` | `DP-0012` | 写入知识库时递增，前缀见 1.2；每种前缀一个序列，序列名为 `knowledge-<前缀>`，例如 `knowledge-DP` |
| 评测用例 | `E-<四位序号>` | `E-0004` | 在 `evals/<模块>/` 与 `evals/retrieval/` 内各自独立递增；用户新增用例时由 `admin eval add` 取该目录下的下一个编号 |
| 评测 | `EV-<日期>-<时分秒>` | `EV-20260929-031500` | 评测开始时生成 |
| 交接文档 | `<模块>-<对象编号>`；验证为 `verify-<阶段>-<Issue 编号>`；周报为 `learn-weekly-<日期>`；运行摘要为 `loop-<运行编号>` | `triage-P-0042` | 由模块与对象决定，重跑时覆盖 |
| 报告类人读文档的 frontmatter `id` | 与对应交接文档的编号相同 | 发现报告 `triage-P-0042`、修复报告 `fix-0007`、验证报告 `verify-local-0007` | 渲染时写入；Issue 的 frontmatter `id` 为其自身编号 |
| trace / span | 32 位与 16 位十六进制 | — | 与 OpenTelemetry 的格式一致 |

序号由数据库中的 `sequences` 表分配，保证在并发写入时不重复。

### 1.2 枚举

代码中使用英文取值，展示给用户时用中文名称。中英对照集中定义在 `domain/enums.py`，渲染人读文档时统一转换。枚举取值一律为小写字母加短横线；事件日志的 `operation` 取值(`invoke_agent`、`execute_tool`、`run_script`、`gate`、`user_action`)沿用 OpenTelemetry GenAI 语义约定的写法，信号的 `check` 与 Schemathesis 的检查名共用同一命名空间(`not_a_server_error`、`console_error` 等)，这两类不是本工具定义的枚举。

**信号、问题与分诊**

| 枚举 | 取值(中文) |
|---|---|
| `Source` | `error`(错误)、`performance`(性能)、`behavior`(行为)、`feedback`(反馈)、`synthetic`(合成) |
| `Probe` | 采集方法：`platform-errors`(内部错误)、`access-log`(访问日志)、`alerts`(业务告警)、`project-probe`(项目探针)、`api-fuzz`、`static`、`incidental` |
| `ProbeLevel` | `shallow`(浅跑)、`deep`(深跑)、`incremental`(增量)、`full`(全量)、`baseline`(基线)；各探针可用的档位见 04 篇 1.3 |
| `SignalAggregateState` | `pending`(待聚合)、`voided`(作废)、`done`(已处理) |
| `ProblemStatus` | `pending`(待确认，复现确认之前；重放未复现的标记间歇)、`new`(新发现)、`ongoing`(持续)、`resolved`(已解决)、`regressed`(回归)、`ignored`(已忽略) |
| `Verdict` | `confirmed`(确认成立)、`conditional`(条件成立)、`refuted`(不成立)、`insufficient`(证据不足) |
| `Severity` | `P0`、`P1`、`P2`、`P3` |
| `Complexity` | `low`(低)、`medium`(中)、`high`(高) |
| `ImpactKind` | `authorization`(权限)、`data-ownership`(数据归属)、`data-correctness`(数据正确性)、`credential-leak`(凭证泄露)、`core-flow-broken`(核心流程不可用)、`non-core-error`(非核心功能出错)、`contract-mismatch`(契约不一致)、`experience`(体验与规范)、`slow-response`(响应偏慢)、`dependency-vulnerability`(依赖漏洞)；取证角色的影响类别，严重度计算的输入 |
| `WorthRecommendation` | `fix`(该修)、`optional`(可修可不修)、`defer`(暂不修)、`wont`(不该修) |
| `TaskType` | `bug`(缺陷)、`security`(安全)、`data`(数据)、`frontend`(前端)、`feature`(功能)、`refactor`(重构)、`dependency`(依赖)、`docs-config`(文档配置)；取证给出，决定修复通道 |
| `SizeTier` | `micro`(微)、`small`(小)、`medium`(中)、`large`(大)、`oversize`(超限)；门槛在 `thresholds.tiers` |
| `Treatment` | `immediate`(立即修)、`scheduled`(排期修)、`observe`(观察)、`wont-fix`(不修)；由 `triage.treatment.rules` 的决策树给出 |
| `Disposition` | `problem false-positive`(判为误报)、`accepted-tradeoff`(已接受的取舍)、`awaiting-deploy`(等待部署)、`create-issue`(提 Issue)、`deferred`(暂不修)、`manual-queue`(人工队列) |
| `TriageOutcome` | `correct`(判对)、`false-confirm`(误判为成立)、`false-refute`(误判为不成立)、`overridden`(用户改判) |

**Issue、修复、验证与发布**

| 枚举 | 取值(中文) |
|---|---|
| `IssueStatus` | `needs-decision`(待决定)、`todo`(待修)、`in-progress`(进行中)、`pending-merge`(待合并)、`done`(完成)、`cancelled`(取消) |
| `IssuePhase` | `fix`(修复)、`verify`(合并前验证)、`submit`(提交)、`deploy-check`(部署后确认)；前三种只随进行中出现，`deploy-check` 只随完成出现；只供程序推进，不作为状态显示 |
| `IssueOrigin` | `triage`(分诊生成)、`manual`(用户需求：不关联问题，创建即为 `todo`) |
| `CloseReason` | `fixed`(已修复)、`fix-rejected`(修复未采纳)、`wont-fix`(不修)、`duplicate`(重复)、`not-a-bug`(不是缺陷) |
| `ReviewCategory` | `local`(局部问题)、`plan-gap`(计划没覆盖)、`needs-user`(规范要求用户确认)、`design`(设计问题) |
| `ReviewMode` | `light`(轻量评审)、`deep`(深度评审)、`screenshot`(截图评审) |
| `ReviewFindingKind` | `root-cause-unfixed`(根因没修掉)、`caller-broken`(破坏已有调用方或入口)、`hardcode`(针对复现输入写死)、`new-error-path`(新增错误路径)、`requirement-unmet`(验收标准未达成)、`requirement-reduced`(缩减需求)、`authz`(权限与归属，仅深度评审)、`data-structure`(数据结构与存量数据，仅深度评审)、`contract`(接口契约联动，仅深度评审)；不在此列的问题由核心丢弃 |
| `Lane` | `fast`(A 快速)、`standard`(B 标准)、`large`(C 大任务)；由 `fix.lanes` 的「类型 × 档」流程表决定(design 5.2) |
| `FixRiskLevel` | `normal`(常规)、`high`(高风险)；决定 B 通道是否另加深度评审(design 5.8) |
| `RiskCategory` | `schema`(数据库结构)、`authz`(权限与数据归属)、`contract`(公共接口或契约) |
| `VerifyPhase` | `local`(PR 阶段检查)、`staging`(部署后确认) |
| `CheckResult` | `pass`(验证通过)、`weak`(弱证据)、`unverified`(未验证)、`fail`(失败)；验证中每个检查项的结果 |
| `RegressionKind` | `api`、`page`、`static`、`test`；复现检查的类型 |
| `RegressionResult` | `passed`(通过)、`failed`(失败)、`not-run`(未执行)、`invalid`(无法执行)；复现检查的最近结果 |
| `DeploymentStatus` | `pending`(尚无覆盖该 commit 的部署)、`running`(部署中)、`succeeded`(成功)、`failed`(失败) |
| `OperationKind` | `create-fix-worktree`(建修复分支与 worktree)、`init-readonly-worktree`(初始化只读 worktree)、`commit`(提交)、`merge-main`(合并 `origin/main`)、`commit-merge`(冲突解决后的合并提交)、`abort-merge`(放弃合并)、`push`(推送)、`pull-request`(创建或更新 PR)、`pr-comment`(在 PR 上发评论)、`revert-pull-request`(提撤销合并的 PR)、`cleanup`(删除修复 worktree 与本地分支)、`fix-plan`(确认修复计划)、`local-migration`(把迁移应用到测试库) |
| `OperationExecutor` | `vcs`(确认后由核心执行) |
| `OperationStatus` | `pending`(待确认)、`confirmed`(已确认)、`rejected`(已拒绝)、`executed`(已执行)、`failed`(执行失败)、`expired`(已过期) |

**运行、执行器与边界**

| 枚举 | 取值(中文) |
|---|---|
| `Stage` | `collect`、`aggregate`、`triage`、`issue`、`fix`、`verify`、`release`、`learn`、`improve` |
| `RunStage` | `Stage` 的全部取值加 `loop`(编排运行)；只用于运行记录，不进入交接文档的 `stage` |
| `RunStatus` | `running`(进行中)、`ok`(成功)、`partial`(部分完成)、`failed`(失败)、`skipped`(无需运行)、`blocked`(前置条件不满足或等待用户)、`interrupted`(被中断)；`runs.status` 的取值 |
| `HandoffStatus` | `ok`、`blocked`、`failed` |
| `RunnerStatus` | `ok`(成功)、`failed`(失败)、`limit-reached`(达到上限)、`schema-invalid`(输出不合 schema)、`guard-violation`(边界违规)；执行器结果的状态 |
| `Access` | `read-only`(只读)、`workspace-write`(只能写工作目录) |
| `ViolationKind` | `credential-present`、`git-unreadable`、`readonly-modified`、`forbidden-path-modified`、`git-commit-created`、`git-branch-switched`、`git-ref-changed`、`git-remote-changed`、`git-stash-changed`、`git-worktree-changed`、`git-operation-started`、`protected-modified`、`test-modified`、`skip-marker-added`、`size-exceeded`、`suspected-hardcode`、`hidden-path-read`；含义见 02 篇 3.11 |
| `AgentSessionStatus` | `open`(进行中)、`closed`(已结束) |

**评分与评测**

| 枚举 | 取值(中文) |
|---|---|
| `ScoreResult` | `pass`(通过)、`fail`(不通过)、`unknown`(无法判断)、`not-applicable`(不适用) |
| `ScoreMethod` | `code`(代码)、`judge`(评审：生产中为修复的那一次评审，评测中为评测组件的模型评审)、`user`(用户) |
| `EvalVerdict` | `pass`(通过)、`reject`(否决)、`needs-review`(需人工判断)、`incomplete`(未完成) |
| `EvalCaseCategory` | `representative`(代表性成功)、`corrected`(人工纠正过)、`edge`(边界情况) |
| `YieldOutcome` | `pending`(结果未定)、`useful`(有效产出)、`no-yield`(无有效产出)；环节效益的判定(design 14.2) |
| `SuggestionKind` | `probe-config`(采集配置)、`coverage-gap`(覆盖缺口)、`knowledge-review`(知识复核)、`improvement`(改进建议)、`control`(控制措施) |
| `SuggestionStatus` | `pending`(待处理)、`accepted`(已接受)、`rejected`(已拒绝)、`expired`(已过期) |

**知识**

| 枚举 | 取值(中文) |
|---|---|
| `KnowledgeType` | `defect-pattern`(DP)、`tradeoff`(TO)、`triage-lesson`(TL)、`fix-lesson`(FL)、`contract`(CT，字段契约等项目约定)、`reference`(RF，项目参考资料) |
| `KnowledgeStatus` | `active`、`superseded`、`archived` |
| `KnowledgeWriteDecision` | `add`(新增)、`update`(更新已有条目)、`merge`(合并)、`noop`(不写入) |

### 1.3 时间

- 所有存储与交接中的时间都是 UTC，ISO 8601 格式，精确到秒。
- 只有渲染人读文档时转换为本机时区，并写明时区。
- 需要「当前时间」的逻辑一律通过 `Clock` 接口获取，测试与 `--now` 参数可以替换它(第 15 章)。
- 工作日为周一到周五，排除 `schedule.nonWorkingDays` 中的日期；以工作日计的阈值(审阅提醒、PR 提醒)都按此计算。

## 2. domain

**原则**：不做任何 IO，不读时间(时间由调用方传入)，不依赖 `store` 与外部程序。所有函数都可以直接单元测试。

### 2.1 文件划分

```
domain/
  ids.py            编号格式的解析与校验
  enums.py          全部枚举及中文名称
  clock.py          Clock 接口与系统实现、固定时间实现
  signal.py         Signal 实体
  problem.py        Problem 实体、ProblemLink、问题状态机；标题生成、出现的累计、解决判定、回归判定、忽略到期判定
  environment.py    运行的环境判定(2.4)
  suppression.py    抑制规则的匹配(2.9)
  reproduce.py      复现确认的策略与判定、不稳定问题的升级(2.7)
  commit_facts.py   CommitFacts：调用方查询好的 commit 祖先关系，供解决与回归判定使用
  triage.py         TriageResult 实体；严重度、处理标签决策树与去向的判定规则(3.5、3.6、13.2)
  issue.py          Issue 实体、Issue 状态机、与问题状态的同步规则(4.7)
  run.py            Run 实体、覆盖范围(Coverage)
  knowledge.py      KnowledgeEntry 实体
  normalize.py      规范化规则的执行(规则本身由配置提供)，消息与位置两种规范化
  fingerprint.py    各采集方法的指纹组成与计算(平台来源直接用平台分组编号)，回归信号的归属
  sizing.py         规模档(13.1)与任务复杂度(13.3)；门槛由调用方从配置传入
  fix.py            修复的风险判定(design 5.8)；规则由调用方从配置传入
  next_step.py      对象状态到下一步模块的映射表(15.10)
```

### 2.2 实体

实体用不可变的数据类表示，字段与数据库列一一对应(列见 4.2)。下表只列关键字段，完整字段以 `contracts/schemas/` 中的 schema 为准。

| 实体 | 关键字段 |
|---|---|
| `Signal` | `id`、`run_id`、`source`、`probe`、`check`、`environment`、`occurred_at`、`release`、`location`、`message`、`normalized_message`、`context`(JSON)、`actor`(JSON)、`fingerprint`、`suppressed`、`aggregate_state` |
| `Problem` | `id`、`fingerprint`、`fingerprint_version`、`probe`、`title`、`status`、`first_seen_at`、`last_seen_at`、`first_seen_release`、`last_seen_release`、`resolved_release`、`occurrences`、`issue_id`、`ignore_until`(恢复条件，JSON)、`intermittent`(间歇出现)、`clean_covered_runs`(已解决判定所需的覆盖运行计数)、`merged_into`(被合并时指向的问题) |
| `TriageResult` | `problem_id`、`attempt`、`verdict`、`severity`、`complexity`、`treatment`、`task_type`、`size_tier`、`root_causes`(列表)、`introduced_by`(commit、作者、PR)、`disposition`、`reason`、`triage_commit`、`refuter_verdict`、`flags`(设计问题、数据结构、公共契约)、`labels`、`outcome` |
| `Issue` | 4.3 的全部字段；`status`、`phase`、`close_reason`、`branch`、`pr`、`hold`(待决定的原因：`reason`、`stage`、`since`、`details`)、`parent`(拆分出的子任务的父 Issue)、`depends_on`、`source` |
| `Run` | `id`、`stage`(`RunStage`)、`probe`、`level`、`parent_run_id`、`started_at`、`ended_at`、`target_commit`、`coverage`、`environment_detail`、`status`(`RunStatus`)、`aggregated_at`、`trace_id` |
| `Coverage` | `endpoints`(路由模板、方法与角色)、`endpointsTotal`、`pages`、`pagesTotal`、`cases`、`files`、`methods`(GET 或全部)、`serverLog`(是否读取了日志)；两个 `*Total` 由探针在运行时统计：接口总数取自 `spec-export` 扩展导出的接口描述并扣除排除范围，页面总数取自 `page-routes` 扩展；对应的扩展没有实现时为空，表示覆盖率未知(10 篇 4.1) |
| `CommitFacts` | 本次涉及的 commit 对的祖先关系；查询不到的记为未知 |
| `KnowledgeEntry` | 16.3 的 frontmatter 字段加 `path`、`hits`、`last_hit_at`；`hits` 与 `last_hit_at` 只存在于数据库，不写入 frontmatter |
| `FixRisk` | `level`(`FixRiskLevel`)、`categories`(`RiskCategory` 列表)、`hits`(每条命中的类别、文件、行、规则来源)、`phase`(`plan` 或 `apply`) |
| `StageYield` | `id`、`run_id`、`stage`、`role`、`subject_id`、`attempt`、`runner_status`、`input_tokens`、`output_tokens`、`cost_usd`、`outcome`(`YieldOutcome`)、`outcome_reason`、`decided_at`、`tool`、`model` |

待确认操作的数据类 `PendingOperation` 定义在 `vcs/operations.py`，字段与 `pending_operations` 表一一对应(4.2)。

### 2.3 状态机

问题状态机与 Issue 状态机都写成「转换表 + 纯函数」：

```
transition(current_state, event, context) -> (new_state, side_effects)
```

- `event` 是明确的事件类型，取值见下表。
- `side_effects` 是要执行的后续动作的描述(例如「Issue 重新打开」)，由上层执行，状态机自身不执行。
- 不在转换表中的组合一律抛出 `InvalidTransition`，不静默忽略。

转换表的内容来自 2.8、4.6、4.7，写成数据而不是分支语句，便于 `next_step.py` 与测试共用。

**问题的事件**

| 事件 | 含义 | 写入方 |
|---|---|---|
| `reproduced`、`not-reproduced` | 复现确认有效、未通过 | `aggregate` |
| `promoted` | 不稳定问题在最近的相关运行中出现次数达到阈值，升级为新发现 | `aggregate` |
| `seen-again` | 再次出现 | `aggregate` |
| `covered-run-without-occurrence` | 覆盖运行中没有出现，累计到已解决的判定 | `aggregate` |
| `resolved-on-new-commit` | static 在新 commit 上覆盖而未命中 | `aggregate` |
| `regression-check-failed` | 复现检查失败产生的回归信号 | `aggregate` |
| `triaged` | 分诊得出结论，按去向转换状态 | `triage` |
| `user-ignored`、`user-false-positive`、`user-reopened` | 用户的人工操作 | `aggregate` 的人工操作命令 |
| `problem ignore-expired` | 忽略的恢复条件满足 | `aggregate` |
| `merged` | 被并入另一个问题 | `aggregate` 的 `merge` 命令、`triage` 的查重 |
| `rebuilt` | 整体重放时一个旧问题拆出的新问题 | `aggregate --rebuild` |
| `overridden` | 用户改判分诊结论 | `triage` 的 `problem retriage --verdict` |
| `problem retriage-requested` | 需要重新分诊：聚合发现新类型的证据，或缺陷类的复现测试在基准版本上就通过 | `aggregate`、`fix`、`verify` |
| `issue-closed` | 关联 Issue 关闭，按关闭原因同步问题状态(4.7) | `issue`、`verify`、`release` |

**Issue 的转换表**(`domain/issue.TRANSITIONS`；规则按当前 `phase` 匹配，副作用 `set-phase` 写入新的 `phase`)

| 事件 | 从 | 到 | 触发 |
|---|---|---|---|
| `approve` | `needs-decision` | `todo`(清除 `hold`) | `approve` |
| `fix-started` | `todo`、`needs-decision`、`in-progress`、`pending-merge` | `in-progress`/`fix`(清除 `hold`) | `fix start`；待决定的须带 `hold` 并加 `--force`(未放行的先 `approve`)；从合并前验证、提交或待合并进入表示用户重新进入修复 |
| `not-reproduced` | `in-progress` | `needs-decision` | 缺陷类的复现测试在基准版本上就通过(修复第 5 步)；关联问题重新分诊 |
| `fix-held` | `todo`、`in-progress` | `needs-decision`(设置 `hold`) | 设计问题、超限、写不出复现测试、修改轮数或计划重出用尽、无人值守修复停下、`fix abandon` |
| `fix-done` | `in-progress`/`fix` | `in-progress`/`verify` | `fix done` |
| `verify-passed` | `in-progress`/`verify` | `in-progress`/`submit` | 合并前验证通过 |
| `verify-failed` | `in-progress`/`verify` | 带 `hold` 时 `needs-decision`，否则 `todo` | 合并前验证失败；连续失败达到上限时带 `hold` |
| `main-merged` | `in-progress`/`submit`、`pending-merge` | `in-progress`/`verify` | 修复分支合并了 `origin/main`，需要重新验证 |
| `pr-created` | `in-progress`/`submit` | `pending-merge` | PR 创建成功 |
| `pr-merged` | `pending-merge` | `done`/`deploy-check`(`fixed`) | PR 合并 |
| `pr-closed` | `pending-merge` | `cancelled`(`fix-rejected`) | PR 关闭且未合并；同步关联问题 |
| `staging-verified` | `done`/`deploy-check` | `done`(清空 `phase`) | 部署后确认全部满足；同步关联问题、回填分诊结果 |
| `staging-failed` | `done`/`deploy-check` | `todo`(清除关闭原因) | 部署后复现检查失败、补做项失败，或关联问题在修复部署后再次出现 |
| `problem-regressed` | `done`、`cancelled` | `todo` | 关联问题回归 |
| `user-closed` | 未关闭的四种 | `cancelled`(`wont-fix`、`duplicate`、`not-a-bug`) | `issue close`；在 GitHub 上关闭镜像 |
| `user-reopened` | `done`、`cancelled` | `todo` | `issue reopen`；在 GitHub 上重新打开镜像 |
| `restart` | `todo`、`in-progress`、`pending-merge`、带 `hold` 的 `needs-decision` | `todo` 或 `in-progress`/`verify` | 只能由 `continue --from fix` 或 `continue --from verify` 触发 |

Issue 的初始状态：分诊生成的为 `needs-decision`(等待放行；满足自动放行时立即放行为 `todo`)；用户需求(`origin: manual`)为 `todo`，`approve` 对它不转换状态(没有 `approve` 事件)，只申请建修复分支。用户需求的类型不是缺陷类，不会收到 `not-reproduced`。修复计划拆分出的后续子任务以用户需求的 Issue 建立，`parent` 为父 Issue、`dependsOn` 指向前一个子任务，它完成(`done`)之前不能建分支与开始修复(07 篇 4.6)。

`hold` 不是状态，只出现在待决定的 Issue 上，说明为什么待决定：`next_step.py` 对带 `hold` 的 Issue 返回「不能自动继续」；用户以 `fix start --force` 确认继续后清除。旧 Issue 文件中的八种状态读取时按 `domain/issue.legacy_status` 换算(与迁移 009 相同：带 `hold` 的未关闭 Issue 为待决定，已合并为完成并等待部署后确认)。

### 2.4 纯函数

| 函数 | 输入 | 输出 |
|---|---|---|
| `normalize.normalize(text, rules)` | 原文、规范化规则 | 规范化后的文本(2.5) |
| `normalize.location(location, probe, rules)` | 原始位置、探针、规则 | 规范化后的位置(路由模板、页面路由模板或 `文件:类名.方法名`) |
| `fingerprint.fingerprint(signal, version)` | 信号、指纹规则版本 | 16 位指纹(2.6)；回归信号(`check=regression`)不计算指纹，按 `context.targetFingerprints` 归属 |
| `environment.judge_run(run, signals, thresholds)` | 运行、信号、阈值 | 环境判定与各信号的 `aggregate_state` |
| `suppression.match(signal, rules, now)` | 信号、抑制规则、当前时间 | 命中的规则 |
| `reproduce.strategy(probe, check)`、`judge_replays(results)`、`judge_next_run(problem, run, occurred)`、`should_promote(history, window, minimum)` | 探针与检查、重放结果、运行历史 | 复现确认的方式与判定、是否升级 |
| `links.derive(occurrences, candidates, window_seconds)` | 本次出现、候选问题、时间窗 | 关联与置信度 |
| `problem.title_for(signal)`、`apply_occurrence(problem, signal)`、`resolution_ready(problem, run, facts, thresholds)`、`is_regression(problem, release, facts)`、`ignore_expired(problem, now, facts)`、`is_covered(problem, run)` | 问题、信号、运行、`CommitFacts` | 标题、累计后的问题、各项判定(2.8) |
| `triage.severity(impact_kind)` | `ImpactKind` | 严重度(3.5) |
| `triage.treatment(facts, rules)` | 判定、严重度、规模档、价值判断，配置中的 `triage.treatment.rules` | 处理标签：按顺序取第一条全部条件都满足的规则(13.2) |
| `triage.disposition(facts)` | 分诊结论与处理标签 | 去向(3.6、13.2) |
| `triage.labels(touches_protected)` | 预估文件是否命中受保护路径 | Issue 标签(只有「需要先与代码作者讨论」) |
| `sizing.size_tier(files, lines, limits)`、`sizing.larger(a, b)` | 文件数与行数(不含测试文件)，配置中的 `thresholds.tiers` | 规模档(13.1)；重评时取两档中较大的一档 |
| `triage.urgency_key(treatment, severity)` | 处理标签、严重度 | Issue 列表与运行摘要等待事项的排序键 |
| `fix.risk(files, changed_lines, flags, impact_kind, endpoint_files, rules)` | 改动文件或候选文件、新增与删除的行、分诊与计划标记、影响类别、端点处理方法所在文件、配置中的 `review.riskRules` 与 `review.deepTriggers` | `FixRisk`(design 5.8) |
| `sizing.complexity(problem, triage_hint)` | 问题与分诊前的线索 | 复杂度(13.3) |
| `next_step(obj, state)` | 对象与状态 | 下一步模块、能否自动继续(15.10) |

## 3. contracts

### 3.1 文件划分

```
contracts/
  schemas/
    common.schema.json                 各目录共用的定义
    handoff/                           交接文档
      envelope.schema.json             交接文档的统一外层(10.2)
      document.schema.json             Markdown 交接文档的头信息(redesign/00-handoff-documents.md)
      outputs/                         各环节的 outputs
        collect.schema.json
        aggregate.schema.json          运行级与问题级两种
        triage.schema.json             3.8 的结构化结论，含 incidentalFindings
        issue.schema.json
        fix-plan.schema.json           fix-planner 的输出
        fix-review.schema.json         fix-reviewer 的输出：mode(light、deep、screenshot)、items、blockers(含 ReviewFindingKind 类别、位置与触发条件)、unverified
        fix.schema.json                修复环节的最终输出，含 risk、incidentalFindings
        verify.schema.json
        release.schema.json
        learn.schema.json              metrics、attention、suggestions、health、rules、cleanup、improve、yields、thirdParty、lessons、outcomes、errors
        loop.schema.json               运行摘要
      frontmatter/                     Issue 与报告类人读文档的 frontmatter
        issue.schema.json
        report.schema.json             发现报告、修复报告、验证报告；summary 与 tags 必填
      types/                           Markdown 交接文档各类型的数据块(<类型>.schema.json)
    config/                            配置
      project-config.schema.json       配置的结构：核心默认值、技术栈默认值、project.yaml 共用，合成后整体再校验一次(5.1)
      user-config.schema.json          本机用户配置的结构，只含 5.3 所列的个人键
    extension/                         扩展点契约(10 篇)
      extension-request.schema.json    扩展调用请求的外层(10 篇 2.2)
      extension-response.schema.json   扩展调用响应的外层(10 篇 2.3)
      stack-manifest.schema.json       技术栈扩展的 stack.yaml(10 篇 5.2)
      method-manifest.schema.json
      points/                          各扩展点的输入与输出(10 篇第 3 章)
        spec-export.input.schema.json  outputFile
        spec-export.output.schema.json specFile、format、operationCount、tool、logFile
        authz-endpoints.input.schema.json 空对象
        authz-endpoints.output.schema.json endpoints(method、route、requires、anonymous、symbol、sourceFile)、unresolved
        authz-roles.input.schema.json  roles
        authz-roles.output.schema.json capabilities、roles、sourceFiles
        log-source.input.schema.json   cursor、initialLookbackMinutes、maxBytes、window
        log-source.output.schema.json  chunks、cursor、gaps、truncated
        log-parse.input.schema.json    chunks、state
        log-parse.output.schema.json   entries(含 level、exception、frames 与 isProject)、state、unparsed
        static-tools.input.schema.json level、baseCommit、changedFiles、rawDir
        static-tools.output.schema.json tools、findings
        page-routes.input.schema.json  空对象
        page-routes.output.schema.json routes(path、name、componentFile、meta)、sourceFiles
        local-run.input.schema.json    mode、ports
        local-run.output.schema.json   services、unavailable、migrationPaths
    runner/                            调用模型的任务与结果
      runner-task.schema.json          执行器任务(9.4)，含 web(是否允许联网检索)与 readPaths(工作目录之外允许读取的文件)
      runner-result.schema.json        执行器结果(9.4)
      transcript-event.schema.json     会话记录的统一事件
      guard-report.schema.json         边界检查报告
      replay-index.schema.json         回放录制集的 index.json
      change-request.schema.json       agent 提交的写操作请求(10.2)
      roles/                           各 agent 角色的输出 schema
        claim-verifier.schema.json     取证：先写 analysis，成立时另有 assessment(价值判断、任务类型、预估改动、修复方向、标记)
        refuter.schema.json
        static-review.schema.json      增量审查、全量扫描与基线审查共用
        fix-scout.schema.json
        fix-executor.schema.json
        repro-test.schema.json         复现测试：fix-executor 第一轮与 repro-writer 共用
        knowledge-curator.schema.json  知识写入去重的判断结果
        judge.schema.json              模型评审的逐项结果
        lesson-writer.schema.json
        rule-writer.schema.json        缺陷变规则：Semgrep 规则或表达不了的原因
        improvement-writer.schema.json 改进建议：prompt 类补丁或 model 类模型别名
      tasks/                           核心发起的非角色执行器任务的输出 schema
        triage-dedup.schema.json       查重
    data/                              信号、问题、复现检查等数据文件
      signal.schema.json
      problem.schema.json
      regression.schema.json           复现检查清单 check.yaml
      authz-model.schema.json          越权检查的数据：端点到所需能力、角色到能力，由 authz-endpoints 与 authz-roles 的输出合成
      eval-case.schema.json            评测用例(03 篇 2.3)，以 kind 区分 module 与 retrieval
      eval-report.schema.json          评测报告的机读格式
      pending-operation.schema.json    待确认操作
      knowledge.schema.json            16.3 的字段；status 为 superseded 时 supersededBy 必填
  validate.py                       校验，返回逐条错误
  versions.py                       各 schema 的当前版本号与升级函数
```

### 3.2 约定

- schema 使用 JSON Schema 2020-12。
- 每个 schema 带整数版本号；交接文档的 `schemaVersion` 记录写入时的版本。读取旧版本时由 `versions.py` 中的升级函数逐级升级，升级函数有单元测试。
- 字段名用 camelCase(与 JSON 交接文档一致)；Python 实体用 snake_case，由 `store` 层转换。
- 校验失败返回每条错误的 JSON 路径与原因，交回执行器重试时原样提供给 agent。
- 任务外发现在交接文档中统一写在 `outputs.incidentalFindings`，每项为 `file`、`line`、`symbol`、`text`；`collect` 的 incidental 探针只从这里读取。

## 4. store

### 4.1 文件划分

```
store/
  db.py                 连接(WAL、busy timeout、外键开启)、事务
  migrations/           本工具数据库的结构变更，按序号执行，记录在 schema_migrations 表
    001_initial.sql
  sequences.py          编号分配
  repos/                每个实体一个仓储：读、写、按条件查询
    runs.py  signals.py  problems.py  triage.py  issues.py  issue_events.py
    handoffs.py  scores.py  suggestions.py  metric_snapshots.py
    knowledge.py  deployments.py  pulls.py  regressions.py
    schedule_state.py  pending_operations.py  agent_sessions.py
    source_cursors.py  probe_states.py  pending_claims.py  incidental_sources.py  budget_usage.py  stage_yield.py
  files/
    layout.py           工作区内与工作区之外全部路径的唯一来源(4.3)
    handoff_files.py    交接文档的写入、覆盖与历史保留
    markdown.py         带 frontmatter 的 markdown 读写
    issue_files.py      Issue 文件与 issues 表索引的同步
    suppressions.py     suppressions.yaml 的校验、读取与带文件锁、带备份的写入
  locks.py              对象锁与全局锁
  idempotency.py        幂等键
  retention.py          按保留期清理(2.14、10.4、10.5)
```

- `repos/problems.py` 提供 `merge(target, source)`，聚合的 `merge` 命令与分诊的查重共用，合并逻辑只有一份。
- `files/suppressions.py` 的写入函数由聚合的 `problem false-positive`、分诊判为误报、`issue close --reason not-a-bug` 共用。

### 4.2 数据库表

数据库文件为 `data/tightrein.db`。表结构的唯一来源是 `migrations/` 中的 SQL；仓储代码不隐式建表。删除与归档只由 `retention.py` 执行，按保留期批量处理。

| 表 | 用途 | 列 | 主键与关键索引 |
|---|---|---|---|
| `schema_migrations` | 已执行的结构变更 | `version`、`name`、`applied_at` | `version` |
| `sequences` | 各类编号的当前值 | `name`、`value` | `name` |
| `runs` | 运行记录与覆盖范围 | `id`、`stage`(`RunStage`)、`probe`、`level`、`parent_run_id`(编排运行的子运行指向编排运行)、`started_at`、`ended_at`、`target_commit`、`coverage`(JSON：接口与角色、方法范围、文件、读到数据的来源)、`environment_detail`(JSON：健康检查结果、登录失败的角色、报告是否完整)、`status`(`RunStatus`)、`aggregated_at`(collect 运行被聚合的时间)、`trace_id` | `id`；按 `stage`、`started_at`；按 `aggregated_at` |
| `signals` | 信号 | `id`、`run_id`、`source`、`probe`、`check`、`environment`、`occurred_at`、`release`、`location`、`message`、`normalized_message`、`context`(JSON)、`actor`(JSON)、`fingerprint`、`suppressed`、`aggregate_state`(`SignalAggregateState`) | `id`；按 `fingerprint`、`run_id`、`occurred_at`、`aggregate_state` |
| `problems` | 问题 | `id`、`fingerprint`、`fingerprint_version`、`probe`、`title`、`status`、`first_seen_at`、`last_seen_at`、`first_seen_release`、`last_seen_release`、`resolved_release`、`occurrences`、`issue_id`、`ignore_until`(JSON)、`intermittent`、`clean_covered_runs`、`merged_into` | `id`；`fingerprint` 唯一；按 `status` |
| `problem_signals` | 问题与信号的对应 | `problem_id`、`signal_id` | (`problem_id`、`signal_id`) |
| `problem_aliases` | 合并产生的别名指纹 | `fingerprint`、`problem_id`、`created_at` | `fingerprint` 唯一 |
| `problem_events` | 问题的状态变化与人工操作 | `id`、`problem_id`、`at`、`event`(2.3 的问题事件)、`from_status`、`to_status`、`run_id`、`operation`(`auto` 或 `user_action`)、`reason`、`detail`(JSON)、`handled_at`(`problem retriage-requested` 被分诊处理的时间) | `id`；按 `problem_id`、`at`；按 `event`、`handled_at` |
| `triage_results` | 分诊结论 | `problem_id`、`attempt`、`run_id`、`verdict`、`severity`、`complexity`、`root_causes`(JSON)、`introduced_by`(JSON)、`disposition`、`reason`、`triage_commit`、`refuter_verdict`、`flags`(JSON)、`labels`(JSON)、`outcome`、`outcome_at`、`created_at`、`treatment`、`task_type`、`size_tier`(处理标签、任务类型与规模档，迁移 008) | (`problem_id`、`attempt`)；按 `disposition`、`outcome_at` |
| `issues` | Issue 索引，可从文件重建 | `id`、`slug`、`path`、`title`、`status`、`close_reason`、`severity`、`origin`(`triage` 或 `manual`，迁移 005)、`treatment`、`task_type`、`size_tier`(迁移 008)、`problems`(JSON)、`root_cause`(JSON)、`branch`、`pr`、`hold`(JSON)、`github_number`、`github_url`(GitHub 镜像，迁移 006)、`depends_on`(拆分出的后续子任务排在其后的 Issue，与头信息的 `dependsOn` 一致，迁移 007)、`phase`、`parent`、`source`(迁移 009，同时按旧状态与 `hold` 改写 `status`、`phase`、`close_reason` 与 `issue_events` 的状态)、`file_sha256`、`created_at`、`updated_at` | `id`；按 `status` |
| `github_mirror` | GitHub 镜像的簿记(06 篇 10.8)，不随 reindex 重建 | `issue_id`、`remote_state`(GitHub 上已知的 `open`、`closed`)、`labelled_status`(已打的标签：`<状态>|<类型>`)、`relations`(已建立的子 Issue 与阻塞关系，迁移 009)、`error`、`failed_at`(最近一次失败)、`synced_at` | `issue_id` |
| `github_mirror_comments` | GitHub 镜像的待发评论 | `id`、`issue_id`、`body`、`created_at`、`posted_at` | `id`；按 `issue_id`、`posted_at` |
| `issue_events` | Issue「历史」一节的结构化副本 | `issue_id`、`at`、`event`(2.3 的 Issue 事件或 `user-edited`)、`from_status`、`to_status`、`close_reason`、`actor`、`note` | 按 `issue_id`、`at` |
| `handoffs` | 交接文档索引 | `stage`、`phase`(仅 `verify` 为 `VerifyPhase`，其余为空串)、`subject_id`、`attempt`、`run_id`、`path`、`status`、`schema_version`、`created_at`、`stale_at`(`continue --from` 重来时标记下游结果过期) | (`stage`、`phase`、`subject_id`、`attempt`) |
| `scores` | 评分记录(12.3) | `id`、`stage`、`run_id`、`subject_id`、`attempt`、`item`(评分项编号)、`result`(`ScoreResult`)、`method`(`ScoreMethod`)、`detail`、`created_at` | `id`；按 `stage`、`run_id`；按 `item` |
| `suggestions` | 学习建议：采集配置、覆盖缺口、知识复核、改进建议与控制措施 | `id`、`kind`(`SuggestionKind`)、`subject`、`evidence`(JSON)、`target_path`、`diff`、`base_hash`、`status`(`SuggestionStatus`)、`reason`、`created_at`、`decided_at` | `id`；按 `status`、`kind` |
| `metric_snapshots` | 每周指标快照，供周报趋势与控制措施 | `week`、`metric`、`dimension`、`value`、`numerator`、`denominator`、`sample_size`、`computed_at` | (`week`、`metric`、`dimension`) |
| `deployments` | 检测到的 staging 部署 | `commit`、`workflow_run_id`、`status`(`DeploymentStatus`)、`url`、`deployed_at`、`detected_at` | `commit`；按 `detected_at` |
| `pulls` | PR 跟踪状态 | `issue_id`、`number`、`url`、`branch`、`title`、`state`、`mergeable`、`merge_commit`、`merged_at`、`closed_at`、`close_note`、`reviews`(JSON：编号、作者、正文摘要、时间)、`created_at`、`last_checked_at`、`last_reminded_at`、`master_at` | `issue_id`；按 `state` |
| `regressions` | 复现检查清单与最近结果 | `issue_id`、`check_id`、`kind`(`RegressionKind`)、`requires`(JSON)、`path`、`hash`、`base_commit`、`base_result`(修复前复现的结果)、`last_result`(`RegressionResult`)、`last_run_id`、`last_run_at`、`last_release` | (`issue_id`、`check_id`) |
| `schedule_state` | 各定时任务最近一次执行的时间与结果 | `task`、`last_started_at`、`last_ended_at`、`last_status`、`last_run_id`、`missed_count` | `task` |
| `workspace_meta` | 工作区级的少量状态：阶段 `phase`(`onboarding`、`running`)、暂停标记 `paused`、接入历史 | `key`、`value`、`updated_at` | `key` |
| `breaker_counts` | 熔断计数 | `subject_id`、`step`、`failures`、`repeats`、`last_reason`、`tripped_at`、`updated_at` | `subject_id` |
| `onboarding_items` | 接入清单各项 | `item`、`position`、`title`、`state`、`owner`、`detail`、`recommendation`、`answer`、`updated_at` | `item` |
| `locks` | 对象锁 | `subject_id`、`holder_pid`、`holder_host`、`run_id`、`acquired_at`、`expires_at` | `subject_id` |
| `idempotency_keys` | 对外操作的幂等键 | `key`、`status`(`in-progress`、`done`)、`result`(JSON)、`created_at`、`completed_at` | `key` |
| `pending_operations` | 待确认操作 | `id`、`stage`、`subject_id`、`kind`(`OperationKind`)、`executor`(`OperationExecutor`)、`commands`(JSON：逐条 argv、工作目录与说明)、`description`(JSON：作用的仓库与分支、按文件名排序的文件清单、是否影响远程、撤销方法、渲染给用户的说明全文)、`impact`、`reversible`、`preconditions`(JSON：生成时的状态，按 `kind` 取 HEAD、分支、改动内容的哈希、`origin/main` 的 commit、PR 是否存在、计划文件的哈希、迁移文件的哈希等)、`idempotency_key`、`confirmations_required`、`confirmations_given`、`status`(`OperationStatus`)、`created_at`、`decided_at`、`executed_at`、`result`(JSON) | `id`；按 `subject_id`、`status` |
| `agent_sessions` | 交互会话 | `stage`、`role`、`subject_id`、`started_at`、`tool`、`session_id`、`workdir`、`status`(`AgentSessionStatus`)、`ended_at` | (`stage`、`role`、`subject_id`、`started_at`) |
| `source_cursors` | 平台来源的读取位置(04 篇 4.1) | `source`(来源标识，如 `platform-errors:error-tracking`)、`cursor`(JSON：窗口终点 `until`，访问日志另有 `baseline`)、`parse_state`(JSON，`log-parse` 返回，核心不解读)、`updated_at` | `source` |
| `probe_states` | 项目探针的上次运行时间与状态(04 篇 4.4) | `name`、`last_run_at`、`state`(JSON，探针上次输出的 state) | `name` |
| `pending_claims` | 静态巡检未取证的疑点(04 篇 5.2) | `id`、`claim`(JSON)、`severity`、`reason`(`low`、`over-limit`)、`run_id`、`created_at`、`state`(`pending`、`verified`) | `id`；按 `state`、`reason` |
| `incidental_sources` | 任务外发现的已读记录 | `source_path`、`content_hash`、`read_at`、`signal_count` | `source_path` |
| `knowledge_meta` | 知识条目与可检索文档的元数据、命中次数 | `id`、`type`(`KnowledgeType` 加 `issue`、`finding`、`fix-report`)、`status`、`title`、`summary`、`tags`(JSON)、`related`(JSON)、`superseded_by`、`updated`、`review_by`、`path`、`content_sha256`、`file_mtime`、`file_size`、`hits`、`last_hit_at`、`indexed_at` | `id`；`path` 唯一；按 `type`、`status` |
| `knowledge_fts` | FTS5 全文索引(16.4) | `id`(不参与匹配)、`title`、`summary`、`tags`、`body`；分词器 `unicode61 remove_diacritics 2`，列权重见 03 篇 1.5 | — |
| `stage_yield` | 环节效益：每次 LLM 调用的 token 与有效产出(design 14.2) | `id`(自增)、`run_id`、`stage`、`role`、`subject_id`、`attempt`、`runner_status`、`input_tokens`、`output_tokens`、`cost_usd`、`outcome`(`YieldOutcome`)、`outcome_reason`、`created_at`、`decided_at`、`tool`、`model`(迁移 012，实际使用的工具与模型) | `id`；按(`stage`、`role`、`created_at`)；按 `outcome` |
| `budget_usage` | 各环节每天的费用累计(11.3) | `stage`、`date`、`cost_usd`、`input_tokens`、`output_tokens`、`estimated`(是否含估算费用)、`updated_at` | (`stage`、`date`) |

**待确认操作的约定**

- `executor` 为 `vcs` 的操作确认后由核心执行：`commands` 经 `vcs` 逐条执行；`commands` 为空的确认类操作(`fix-plan`、`local-migration`)确认后不执行命令，只调用发起模块登记的后续处理。
- 状态流转：`pending` → `confirmed` → `executed` 或 `failed`；`pending` → `rejected`；执行前复核 `preconditions` 不一致，或超过 7 天未确认，改为 `expired`。`confirmations_required` 为 2 的操作(`cleanup`)第一次确认后仍为 `pending`，`confirmations_given` 记为 1。执行中由幂等键标记进行中；幂等键已完成时操作直接改为 `executed`，`result` 取上次的结果。
- 确认与拒绝的命令为 `tightrein approve <编号>` 与 `tightrein reject <编号> [--note <说明>]`；确认只对这一个操作有效。

### 4.3 文件布局

`files/layout.py` 是以下路径的唯一来源，其他代码不拼接路径。

**工作区**

```
workspaces/<项目>/
  project.yaml
  onboarding.md                         接入清单(progress 类型交接文档，09 篇 3.12)
  normalize.yaml
  suppressions.yaml
  extensions/                           项目扩展：只属于本项目的扩展点实现及其夹具测试(10 篇第 6 章)
  knowledge/<类型>/<编号>-<简称>.md     知识条目，类型为 KnowledgeType 的取值
  knowledge/<类型>/INDEX.md             各类型的索引，自动生成
  knowledge/<类型>/INDEX-<起始编号>-<结束编号>.md  某类型超过 200 行时的分页
  knowledge/INDEX.md                    总索引，自动生成
  e2e/tsconfig.json                     把模块 tightrein/e2e 映射到核心的 fixtures
  e2e/<角色>/                           各角色的巡检用例
  e2e/common/                           各角色共用的巡检用例
  regressions/<Issue 编号>/check.yaml   复现检查清单，以及清单引用的检查文件
  evals/manifest.json                   全部评测用例的哈希
  evals/<模块>/<用例编号>/              评测用例：case.json、input/、gates.json、replay/
  evals/retrieval/cases.jsonl           检索评测用例
  rules/<Issue 编号>-<简称>.yaml        规则库：缺陷变规则验证通过的 Semgrep 规则，静态巡检单独运行
  issues/<编号>-<简称>.md               Issue
  worktrees/readonly/                   只读 worktree(不纳入本工具仓库的版本管理)
  worktrees/fix-<Issue 编号>/           修复 worktree(不纳入本工具仓库的版本管理)
  data/                                 不纳入本工具仓库的版本管理
    tightrein.db
    aggregate.lock                      聚合的全局锁
    run.lock                            编排运行的全局锁
    guards/readonly-<worktree 名>.json  只读锁定标记
    specs/<commit>/openapi.json         按 commit 缓存的接口描述(spec-export 写出)
    specs/<commit>/<扩展点>.json        按 commit 缓存的扩展输出：spec-export、authz-endpoints、authz-roles、page-routes
    specs/<commit>/<扩展点>.meta.json   上述缓存的缓存键(10 篇 2.7)
    specs/<commit>/authz-model.json     由两份权限数据合成的越权检查数据
    runs/<运行编号>/
      handoff/<交接文档编号>.json         交接文档
      handoff/<交接文档编号>.<序号>.json  重跑前的旧版本
      transcripts/<角色>-<对象编号>.jsonl 会话记录
      signals.ndjson                    collect 的信号原件，aggregate --input 的输入
      raw/<探针>/                       探针的原始输出
      raw/extensions/<扩展点>[-<序号>].stderr.log  扩展的标准错误输出(10 篇 2.1)
      raw/runner/<角色>-<对象编号>/     提示、schema、策略文件、stdout.jsonl、result.json
      raw/guards/<角色>-<对象编号>.json 边界检查的快照与报告
      raw/vcs/<操作编号>/               提交信息、PR 描述、命令输出
    logs/events-<日期>.jsonl            事件日志
    logs/launchd.out.log                launchd 的标准输出
    logs/launchd.err.log                launchd 的标准错误
    findings/<问题编号>.md              发现报告
    fixes/<Issue 编号>/                 交接文档 progress.md、task.md、scout.md、plan.md、result.md、review-<轮>-<模式>.md、decision.md；route.json、repro.json、report.md、prepare.log、plan.json、decisions.json、rounds/<轮次>/、conflicts.md、pr-body.md、pr-comment.md、merge-decision.md、summary.md
    verify/<Issue 编号>/<日期>-<阶段>/   report.md、services.json、services/、screenshots/、raw/
    onboarding/checks/                  接入时在主分支上自检检查命令的日志
    eval/cases/<Issue 编号>.json、<Issue 编号>.input.json  运行即评测的用例：修复部署后确认通过时保存，评测 fix 时与 evals/ 中封存的用例一起加载
    evals/<评测编号>/                   评测产物：plan.json、versions/、snapshots/、outputs/、scores.jsonl、report.json、report.md
    reports/run-<运行编号>.md           各模块运行的人读摘要(collect、aggregate、triage、issue)
    reports/daily-<日期>.md、.json      每日汇总(progress 类型交接文档，编排的运行摘要与收件箱合为一份)及其数据
    reports/weekly-<周一日期>.md        周报(result 类型交接文档)
    improve/<建议编号>.md、.patch       改进建议的 decision 文档与补丁(`learn improve`)
    archive/                            迁移归档；整体重放前的数据库备份 tightrein-<时间>.db
```

- 会话记录的「对象编号」取任务的对象；以运行为对象的任务(静态巡检)取运行编号，同一运行内的多次调用在其后加模式编号或序号，例如 `static-review-<运行编号>.jsonl`、`claim-verifier-<运行编号>-<序号>.jsonl`。
- `--output <目录>` 模式下，交接文档、会话记录、原始输出与事件都写入该目录，事件写入 `<目录>/events.jsonl`，不写 `data/logs/`。

**工作区之外**

| 路径 | 内容 |
|---|---|
| 本工具仓库的 `skills/` | 由 `layout.py` 以核心所在仓库的根目录定位；执行器任务以绝对路径引用 skill 文件，使版本快照中的核心使用同一快照中的 skill |
| 本工具仓库的 `extensions/stacks/<技术栈>/` | 技术栈扩展，同样以核心所在仓库的根目录定位(10 篇第 5 章) |
| `~/.cache/tightrein/extensions/<技术栈或工作区名>/` | 扩展的长期缓存，例如技术栈扩展自带工具的构建产物；经 `TIGHTREIN_CACHE_DIR` 传给扩展 |
| `third_party/skills.lock.yaml`、`third_party/installed.json` | 第三方 skill 的锁定清单与本机安装记录(后者加入 `.gitignore`) |
| `local/third_party-cache/<名称>/<commit>/` | 第三方 skill 按 commit 下载的缓存，在本工具仓库的 `local/`(本机依赖，被 `.gitignore` 忽略)内 |
| `~/.config/tightrein/config.yaml` | 本机用户配置(5.3) |
| `~/.cache/tightrein/venv/` | Python 虚拟环境 |
| `~/.cache/tightrein/packaging/claude/` | Claude Code 插件的构建目录 |
| `~/Library/LaunchAgents/local.tightrein.<项目名>.plist` | launchd 定时配置 |
| `~/.local/state/tightrein/paused` | 全局暂停的标记文件(`tightrein pause`，09 篇 3.11) |

### 4.4 锁与幂等

- **对象锁**：`locks` 表中一行一个对象，记录持有者进程号、主机名、所属运行、开始时间与超时时间。获取失败时等待或退出，由调用方决定；超时的锁、或持有者进程已不存在的锁视为失效，可以被接管，接管时记录事件。除业务对象外，另有两个具名对象锁：`knowledge`(知识的同步与写入)、`local-run`(同一时间只有一个本机服务)。
- **全局锁**：聚合使用 `data/aggregate.lock` 文件锁(2.2)；编排运行使用 `data/run.lock`，同一工作区同一时间只有一个 `run`。
- **幂等键**：对外操作执行前写入 `idempotency_keys`，状态为 `in-progress`，执行成功后改为 `done` 并记录结果；再次执行时发现已完成则跳过并返回上次的结果(15.7)。

## 5. config

### 5.1 文件划分与配置分层

```
config/
  defaults.yaml     核心默认值：全部可调键及其默认值，每个键带一行注释说明含义与取值范围
  layers.py         四层配置的读取、按键合并与来源记录
  project.py        读取与校验 project.yaml，含 stack 与 extensions 段；扩展点的解析由能力层的 extensions 完成(10 篇 1.4)
  user.py           读取本机用户配置，只接受 5.3 所列的个人键与 models、routes、network 段
  network.py        本机用户配置的网络代理到子进程环境与 HTTP 代理表的翻译(5.3)
  routes.py         模型别名与路由表：调用点清单(CALL_POINTS)与条件、按条件与 default 解析工具、模型与推理强度，模型价格，旧键的提示
  show.py           `tightrein project config` 的输出
  secrets.py        从 macOS 钥匙串按条目名读取测试账号密码
```

**配置分层**：所有可调的值(阈值、权重、系数、超时、轮询间隔、轮数与预算上限、保留期、输出截断长度、风险判定规则、评审触发条件、各调用点的模型与推理强度)都写在配置文件中，由人直接编辑，代码中不写死。配置分四层，后一层按键覆盖前一层：

| 层 | 文件 | 放什么 | 校验 |
|---|---|---|---|
| 1. 核心默认值 | `core/tightrein/config/defaults.yaml` | 全部键与默认值；每个键一行注释写明含义与取值范围；与项目、技术栈都无关的值(例如风险判定规则的路径与模式为空) | `config/project-config.schema.json`；启动时校验，出错即退出 |
| 2. 技术栈默认值 | `extensions/stacks/<技术栈>/defaults.yaml` | 同一技术栈通用的取值，例如 `aspnetcore` 的风险判定规则、项目检查命令的缺省形式 | 同上，且只能出现第 1 层已有的键 |
| 3. 项目配置 | 工作区 `project.yaml` | 项目特有的取值与必填项(5.2) | 同上 |
| 4. 本机用户配置 | `~/.config/tightrein/config.yaml` | 只允许 5.3 所列的个人键，以及选模型的个人缺省(`models`、`routes`)、网络代理(`network` 段) | `config/user-config.schema.json`；出现其他键时报出键名并退出 |

- **用户路由层的位置**：`models` 与 `routes`(选模型的两张表，5.2)是个人缺省，写一次所有工作区生效，项目需要时可以按项覆盖，所以它排在第 2 层与第 3 层之间：core → stack → user(models、routes) → project → user(其余个人键)。两类用户键不重名，各键仍按「后一层覆盖前一层」取值；`notify.method` 等个人键仍覆盖项目。

- **合并规则**：映射按键递归合并；列表与标量整体替换，不拼接。`models` 按别名、`routes` 按路由键逐项合并，上层的同一项整体替换下层的。需要在上一层的列表上追加的键，在键名后加 `+`(例如 `paths+`)，表示追加到下层的列表后面：按完整键名读取列表时(`ProjectConfig.get`)，取到某层的值后依次拼上该层与更上层 `<键>+` 的列表；技术栈层的键检查按去掉 `+` 的键名，两个技术栈各自追加不算冲突。schema 目前只为 `stages.fix.roles.frontend-designer.paths+` 开放这一写法，`review.riskRules` 整段读取，暂不支持。`credentialFiles` 只允许追加：各层的值依次拼接，上层不能去掉核心的缺省模式。
- **技术栈层的顺序**：按 `project.yaml` 中 `stacks` 的顺序依次合并；两个技术栈给同一个标量键赋不同的值时报错，由项目配置显式给出。
- **来源记录**：`layers.py` 为每个生效的键记录来源层(`core`、`stack:<名称>`、`project`、`user`)与文件路径，供 `project config` 与报错信息使用。
- **命令**：`tightrein project config [--key <键>]` 列出每个键的生效值与来源层；`--key` 只显示该键及其下级键，并列出各层中该键的值，便于看清是哪一层覆盖了哪一层。`models`、`routes` 以生效键名显示(例如 `models.opus.model`、`routes.fix.planner`)，来源为 `user`，项目写了同一键时来源为 `project`；`--routes` 列出每个调用点(及写了路由的条件变体)解析出的别名、工具、模型、推理强度与生效的路由行；`network.proxy` 中的密码显示为 `[已脱敏]`；`tools.semgrep.path` 同时作为 `runtime.tools.semgrep` 的 `user` 层值显示。命令只读，不写任何文件。
- **不是可调项的值**：单位换算、协议与外部工具规定的值(退出码、HTTP 状态码、编号的长度、schema 版本号)与状态机的规则，留在代码中，不进入配置。

### 5.2 project.yaml 的结构

下表列出 `project.yaml` 可以出现的段。顶层只有 `project` 必填；`target`、`accounts`、`stages`、`evaluation`、`thresholds` 都可以省略，通用的取值在 `defaults.yaml` 中有默认值，项目专属的取值(地址、账号、端口等)没有默认值。给出了某一段时，段内标明必填的键须写全(`target.baseUrl`、`accounts.roles`、`accounts.login` 按 `kind` 要求的键)。合并后的配置按 `config/project-config.schema.json` 整体校验，缺少必填项时报出完整键名并退出。

| 段 | 键 | 含义 |
|---|---|---|
| `project` | `name`、`repo`、`mainBranch`、`language` | 名称、项目主仓库路径、主分支；`language` 为给人读的文字(结论、标题、Issue 正文、计划与 PR 的叙述、评论)的语言，缺省 `en`，Issue 的固定文字有 zh、en、ja 三套(02 篇 2.5 的「输出语言」，06 篇 10.3) |
| `stacks` | — | 用到的技术栈列表，每项对应本工具仓库的 `extensions/stacks/<名称>/`；可省略。示例项目中的取值为 `[aspnetcore, vue, node]` |
| `extensions.<扩展点>.use` | — | 从方法目录中选用的方法编号，与 `command` 二选一(10-extensions 1.4) |
| `extensions.<扩展点>` | `command`、`mode`、`options`、`timeoutSeconds`、`enabled` | 按扩展点声明项目扩展的命令，或给技术栈扩展传参数；`mode` 为 `replace` 或 `extend`；扩展点只能取 00 篇 3.6 清单中的八个；完整说明与示例项目中的取值见 10 篇 1.5 |
| `target` | `baseUrl` | 被测环境的地址(给出 `target` 时必填)；没有 `target` 也没有 `--target` 时，需要地址的方法(api-fuzz)返回 `skipped` |
| `target` | `environment` | 被测地址所在的环境，`staging`(缺省)或 `production`；写入信号的 `environment` 与 collect 交接文档的 `target.environment` |
| `target` | `healthcheck`、`healthTimeoutSeconds` | 健康检查接口与超时；没有 `healthcheck` 时不做健康检查，运行说明中写明 |
| `target` | `manualDeployPaths` | 不随部署自动更新、需要提示用户联系负责人的路径；部署来源见 `extensions.deploy-source`(10 篇 3.9) |
| `accounts` | `roles.<角色>.keychain` | 各角色测试账号在钥匙串中的条目名；角色键同时作为信号中的账号别名。`anonymous` 是保留的匿名身份，不能作为角色键。没有 `accounts` 时 api-fuzz 与验证的页面检查以匿名身份运行(请求不带凭证，04 篇 1.6)；也可以用 `--select role:anonymous` 显式选择匿名身份 |
| `accounts` | `login.kind` | 取凭证的方式：`token-endpoint`(缺省)或 `static-header`，各探针与 `verify` 的接口请求共用 |
| `accounts` | `login.endpoint`、`login.bodyTemplate`、`login.tokenPath` | `token-endpoint` 时必填：调用登录接口换取 token 的方式，token 放进 `Authorization: Bearer` |
| `accounts` | `login.header`、`login.verify.endpoint`、`login.verify.bodyTemplate` | `static-header` 时 `header` 必填：钥匙串条目中的密码原样放进该请求头，不请求登录接口；`verify` 可选，启动取凭证时 POST 该接口(`bodyTemplate` 中只替换 `{password}`)，2xx 即有效 |
| `sources.api-fuzz` | `exclude`、`levels.<档位>`、`workers`、`sanitizeKeys`、`timeoutMinutes`、`checks`、`production.allow` | 排除接口的正则、各档位的参数、并发数、报告中额外脱敏的键、超时、检查项的开关(04 篇 2.4)、生产环境允许测试的路由 |
| `sources.platform-errors` | `every`、`initialLookbackHours`、`logQuery`、`logLimit`、`levels` | 内部错误：编排运行的间隔、首次读取向前的小时数、日志平台上的查询(为空时不读日志平台)、每次读取的条数上限、产出信号的归一化级别(默认 `error`、`critical`)；平台由 `error-tracking`、`log-platform` 扩展读取 |
| `sources.access-log` | `every`、`initialLookbackHours`、`query`、`limit`、`fields`、`pattern`、`minRequests`、`latencyRatio`、`errorRateDelta`、`baselineWeight` | 访问日志(可选，`query` 为空时不启用)：字段映射与退化判断的阈值(04 篇 4.2) |
| `sources.alerts` | `every`、`exclude.names`、`exclude.labels` | 业务告警：排除基础设施类告警的名称正则与标签 |
| `sources.project-probes[]` | `name`、`command`、`every`、`keychain`、`timeoutSeconds` | 项目探针的登记(04 篇 4.4) |
| `checks.pages` | `patrolGrep`、`ignoreRequests`、`locale`、`retries`、`timeoutMinutes` | 验证环节的页面巡检与页面类复现检查：选择巡检用例的正则、不记为失败请求的「方法 + 路径正则 + 状态码」、浏览器语言、失败用例的重试次数、超时 |
| `sources.static` | `semgrep.configs`、`maxClaims`、`baseline.maxClaims`、`baseline.batchFiles`、`baseline.batchLines`、`baseline.exclude` | Semgrep 的规则集(示例项目中的取值为 `p/csharp`、`p/javascript`)、每次取证的疑点上限(超出的进入待处理清单)；基线审查的取证上限(缺省 40)、每批的文件数与总行数上限(缺省 20、3000)、不审查的路径模式(缺省为测试夹具、生成文件、锁文件、二进制与文档，可用 `exclude+` 追加)；构建告警与依赖漏洞等技术栈工具由 `static-tools` 扩展负责 |
| `checks` | `commands[]`(`name`、`cwd`、`command`、`when`、`mustNotModify`、`affected`) | 项目检查命令；`when` 为改动文件匹配的路径模式，`mustNotModify` 表示执行后工作区不得出现新改动；`affected` 为受影响测试的选择：`map`(源文件路径模式到测试文件路径模板的列表)与 `command`(带测试参数的命令形式)，映射为空时运行 `command` 本身覆盖的整组测试(07 篇 4.9) |
| `checks` | `prepare` | 修复 worktree 的准备命令，例如安装前端依赖 |
| `checks` | `residuePatterns` | 交付规则中调试代码与残留的匹配模式(07 篇 4.9) |
| `localRun` | `ports.api`、`ports.backendForPages`、`ports.frontend` | 两种启动模式的端口(07 篇 11.2)；示例项目中的取值为 5100、5000、8080 |
| `localRun` | `readyTimeoutSeconds`、`stopTimeoutSeconds` | 就绪等待与收尾的超时；启动命令、环境变量、就绪与失败信号、迁移文件、不在本机启动的服务由 `local-run` 扩展给出(10 篇 3.8) |
| `protectedPaths` | — | 受保护文件(11.2) |
| `protectedPatterns` | — | 受保护内容模式，例如 `[Authorize]`、`[AllowAnonymous]` 的增删 |
| `testPaths` | — | 测试文件与复现检查的路径模式 |
| `skipMarkers` | — | 跳过测试、关闭告警、抑制类型检查的标记模式 |
| `credentialFiles` | — | 凭证文件模式，例如 `.env`、`*.pem`、`*.pfx`、`secrets*.json` |
| `git` | `conventions.branch`、`conventions.commit`、`conventions.prTemplate` | 工作区显式配置的分支与提交格式串、PR 模板路径；为空时按项目文档、历史推断与通用格式的优先级取(07 篇 19.4) |
| `git` | `personalPrefix`、`forbiddenPrefixes` | 分支名是否加本机用户配置的个人前缀(缺省 `false`)、禁用的个人前缀(AI 与工具名称) |
| `git` | `branchTypes`、`commitTypes` | 任务类型到分支类型、提交类型的映射 |
| `git` | `inference.sampleSize`、`inference.minSamples`、`inference.minRatio` | 从历史推断约定的样本数与统一比例(只给结果，接入时确认) |
| `git` | `splitThreshold` | 拆分阈值 |
| `git` | `worktreeLinks` | 建修复 worktree 后从项目主工作区建立符号链接的路径 |
| `models`(也可写在本机用户配置) | `<别名>.tool`(必填)、`<别名>.model`、`<别名>.effort`、`<别名>.inputUsdPerMTok`、`<别名>.outputUsdPerMTok` | 模型别名：工具、模型(省略时用工具自己的缺省模型)、推理强度与每百万输入、输出 token 的价格(两项同时写或都不写)，工具不返回费用时据此估算；同一工具的同一模型在不同别名中价格不同时报错；工具不支持推理强度时忽略 `effort` |
| `routes`(也可写在本机用户配置) | `default`、`<调用点>`、`<调用点>.<条件>` | 调用点 → 别名。调用点为 `config/routes.py` 的 `CALL_POINTS` 中的固定清单，条件为 `high-risk`、`frontend`、`large`(只用在声明了该条件的调用点上)；带条件的调用依次取 `<调用点>.<条件>`、`<调用点>`、`default`。键不在清单中、别名不存在时报出完整键名；`triage.refuter` 须与 `triage.claim-verifier`、`fix.review.deep` 须与 `fix.executor` 及其条件变体解析为不同的工具或模型。核心不给别名与路由，都没有时运行到该调用点报出调用点名。命令行的 `--runner` 改写工具(工具不同时不沿用别名的模型与推理强度)，`--model` 改写模型。旧的 `defaultTool`、`capabilities`、`roleCapabilities`、`agents`、`evaluation.judge` 与 `stages` 中的 `tool`、`model`、`capability`、`refuter`、`session` 已删去，出现时报出该键与新写法 |
| `stages.<环节>` | `limits.maxTurns`、`limits.maxDurationMs`、`limits.maxCostUsd` | 执行器任务的缺省上限 |
| `stages.<环节>` | `budgetPerDay` | 每天的费用上限 |
| `stages.triage` | `roles.<角色>.limits.<复杂度>`、`tasks.<任务>.limits` | 各角色按复杂度的上限、查重等任务的上限 |
| `stages.fix` | `roles.<角色>.limits.<复杂度>` | 各修复角色按复杂度的上限 |
| `stages.fix` | `roles.frontend-designer.paths` | 前端文件的路径模式(写法同 `protectedPaths`)；修复计划预估改动的文件中有匹配的文件时运行 `frontend-designer`(07 篇 4.6)。核心缺省为常见前端扩展名与目录，技术栈与项目写 `paths` 整体覆盖、写 `paths+` 追加 |
| `stages.fix` | `review.light.limits`、`review.deep.limits` | 轻量评审与深度评审的上限(模型按调用点 `fix.review.light`、`fix.review.deep` 的路由) |
| `stages.verify` | `screenshotReview.limits` | 截图评审的上限(模型按调用点 `verify.screenshot-review` 的路由) |
| `stages.fix` | `budget.low`、`budget.high` | 低复杂度与中高复杂度修复的预算(13.3) |
| `review` | `riskRules.<类别>.paths`、`riskRules.<类别>.patterns` | 风险判定规则，类别为 `schema`、`authz`、`contract`；核心默认为空，技术栈与项目逐层补充(design 5.8) |
| `review` | `deepTriggers.categories`、`deepTriggers.impactKinds`、`deepTriggers.flags` | 参与判定的风险类别(默认三类)、触发深度评审的分诊影响类别(默认权限、数据归属)、触发深度评审的分诊与计划标记(默认数据结构、公共契约) |
| `runtime` | `runner.*`、`extensions.*`、`vcs.*`、`network.*`(换路重试判断路线的主机与网络类错误的模式，02 篇 4.8)、`store.*`、`observability.*`、`keychain.*`、`guards.*`、`aggregate.*`、`probes.*`、`retrieval.*`、`evaluation.*` | 工程参数：各组件的超时、轮询间隔、宽限时间、输出截断长度、重试间隔、日志轮转大小与份数；每个键的含义见 `defaults.yaml` 的注释 |
| `notify` | `method` | 本机通知方式的缺省值(`macos`)；本机用户配置的 `notify.method` 覆盖 |
| `triage` | `severityGuide` | 项目语境下的严重度说明，拼在分诊规则 P0 到 P3 的定义之后(06 篇 4.5、4.10)；缺省没有 |
| `triage` | `refute.severities`(`P0`、`P1`)、`refute.taskTypes`(`security`)、`refute.impactKinds`(`authorization`、`data-ownership`) | 证伪复核的触发条件：取证判为成立或条件成立且命中任一项时运行(design 3.4) |
| `triage` | `treatment.rules[]`(`verdicts`、`severities`、`tiers`、`worth`、`treatment`) | 处理标签的决策树：按顺序取第一条全部条件都满足的规则，条件省略表示不限；项目写 `rules` 整体覆盖(design 13.2) |
| `fix` | `lanes.<类型或 default>.<档>`、`scout.taskTypes`、`repro.skipTypes`、`repro.independentTypes`、`repro.passOnBaseTypes`、`review.skipMicro`、`manualTaskType` | 修复的流程表与各步的类型规则(design 5.2、5.6；07 篇 4)：「类型 × 档」到通道 `fast`、`standard`、`large`；B 通道总是勘察的类型；不写复现测试、由 `repro-writer` 独立写、在基准版本上须通过的类型；A 通道微档跳过轻量评审；用户需求缺省的任务类型 |
| `issues` | `tracker`(`local` 或 `github`，缺省 `local`)、`github.repo`、`github.labelPrefix`、`github.privateOnly`、`github.labelColor` | Issue 去向与 GitHub 镜像(06 篇 10.8)：`github` 时本地仍是数据源，GitHub Issue 是镜像；仓库缺省取 origin；`privateOnly` 缺省为真；镜像写入是否逐次确认由关卡 `gates.mirror-writes` 决定 |
| `release` | `mergeMethod`(`squash` 或 `merge`，缺省 `squash`)、`autoMergeBlockPaths`、`reviewComment`(缺省 `true`)、`deploy.observationHours`(缺省 24) | 自动合并的方式；改动命中 `autoMergeBlockPaths` 时不自动合并、写决策简报(必须交用户的关卡 `high-risk-merge`)；`reviewComment`：AI 评审结论以评论写入 PR；没有配置部署来源时合并后经过 `deploy.observationHours` 视为已部署。写操作是否逐次确认、是否自动合并由关卡 `gates.release-writes`、`gates.merge` 决定(07 篇 19.6) |
| `gates` | `issue-approve`、`plan-confirm`、`fix-session`、`release-writes`、`mirror-writes`、`merge`(取 `user` 或 `auto`，缺省 `user`)；`high-risk-merge`、`delete`、`permissions-secrets`、`over-task-limit`、`needs-decision`(只能为 `user`) | 审批关卡表(`config/gates.py`，09 篇 3.8)：放行新建 Issue、确认 B 通道计划、无人值守修复、发布的写操作、镜像写入、合并 PR 是否按规则自动处理；必须交用户的五项由 schema 限定 |
| `autonomy` | `approve.maxComplexity`(`low`)、`approve.impactKinds`(`authorization`、`data-ownership`)、`approve.severities`(空) | 自主决定的规则：`gates.issue-approve` 为 `auto` 时新建 Issue 满足规则即自动放行(06 篇 9.3)，`gates.plan-confirm` 为 `auto` 时修复计划满足规则即自动确认(07 篇 4.7)，不满足的交用户；计划的门槛在 `thresholds.autonomy` |
| `budget` | `perRunUsd`、`perDayUsd`、`perWeekUsd`、`perWeekPercent`、`subscriptionWeekUsd`(都缺省为空，不限) | 全部环节合计的费用上限(09 篇 3.9)；`perWeekUsd` 为空时按订阅额度的百分比换算 |
| `onboarding` | `stackMarkers`、`checkCommands` | 接入时识别技术栈的标记文件与没有检查命令时推荐的命令(09 篇 3.12) |
| `schedule` | `tick.weekdays`、`tick.hours`、`tick.minutes`、`runAt` | launchd 唤醒 `tick` 的时刻(缺省工作日每 15 分钟)；工作日完整运行的时刻(缺省 09:00)，其余 tick 只检查事件 |
| `schedule` | `onDeploy` | 检测到新部署后浅跑的探针与档位 |
| `schedule` | `tasks[]`(`name`、`days`、`at`、`command`) | 定时任务；`days` 取 `workdays`、`daily`、`firstWorkdayOfWeek` |
| `schedule` | `weekly`、`nonWorkingDays` | 周任务的时刻、非工作日 |
| `evaluation` | `repeats`(3，不小于 3)、`parallelism`(1)、`budgetUsd`(10) | 评测的运行次数、并发数、单次评测的费用上限；模型评审按调用点 `eval.judge` 的路由 |
| `thresholds` | 见下表 | 各项阈值 |

**thresholds**

`thresholds` 中的每个数值项写成 `{ value, min, max }`，`min`、`max` 为允许的取值范围，加载配置时校验 `min ≤ value ≤ max`；映射类的项(例如 `retrieval.contextLimits`)其中每个值同样如此。括号中为默认的 `value`。

| 键 | 含义 |
|---|---|
| `tiers.<档>.maxFiles`、`tiers.<档>.maxLines` | 规模档的门槛(design 13.1)：`micro`(1、30)、`small`(3、100)、`medium`(10、500)、`large`(30、3000)，不含测试文件；超出 `large` 为超限 |
| `slowResponseSeconds` | api-fuzz 的响应过慢阈值 |
| `reproduceAttempts`(2) | api-fuzz 复现确认的重放次数 |
| `resolveCoveredRuns`(3) | 判为已解决所需的覆盖运行次数 |
| `suppressionDays`(30) | 抑制规则的缺省有效天数，聚合的 `problem false-positive` 与分诊判为误报共用 |
| `retention.signalsDays`(90)、`retention.rawDays`(30)、`retention.logsDays`(90)、`retention.fixesDays`(90) | 保留期 |
| `triage.perRun`(5)、`triage.deferredReopenOccurrences`(3)、`triage.evidenceRetries`(2)、`triage.complexityFiles`(1) | 每次分诊的问题数上限、暂不修的问题再出现多少次后重新分诊、证据检查不通过时的重做次数、任务复杂度判为中的预估文件数门槛(13.3) |
| `issue.reviewReminderWorkdays`(3) | 待决定(等待放行)提醒 |
| `change.maxFiles`(10，1 到 30)、`change.maxLines`(500，20 到 3000) | 单个 PR 的改动量上限，不含 `testPaths` 匹配的测试文件；计划预估超出时拆分(07 篇 4.6)，实施后的实际 diff 按同一上限检查(07 篇 4.9) |
| `fix.reviewRounds`(3)、`fix.planRounds`(2) | 写代码之后修改的轮数上限(检查或评审不通过时交回修改)、计划重出次数 |
| `verify.maxConsecutiveFailures`(2)、`verify.stagingWaitDays`(7)、`verify.observationHours`(48)、`verify.readonlyLockMinutes`(120) | PR 阶段检查连续失败的上限、部署后确认等待的提醒天数、按观察期确认的小时数、在只读 worktree 上重跑检查的对象锁时限 |
| `release.prReminderWorkdays`(3) | 待合并的 PR 提醒 |
| `autonomy.planMaxFiles`(3)、`autonomy.planMaxLines`(100) | 自动确认修复计划的预估改动量上限；加载配置时校验各层合并后不大于 `change.maxFiles`、`change.maxLines` |
| `loop.breakerFailures`(3)、`loop.breakerRepeats`(3) | 熔断：无人值守推进中同一对象连续失败、同一步没有进展的次数上限(09 篇 3.10) |
| `retrieval.unusedDays`(90)、`retrieval.inlineFullTokens`(3000)、`retrieval.contextLimits`(`static-review` 20、`triage` 15、`fix` 15) | 未命中判定天数、直接放全文的 token 上限、各任务类型预取的条目上限 |
| `learn.noisyCheckRatio`、`learn.minSamples` | 采集配置建议的阈值 |
| `learn.rules.lookbackDays`(14)、`learn.rules.maxRepoHits`(5) | 缺陷变规则：处理多少天内以已修复关闭的 Issue、规则在全仓库命中数的上限(超过不收入规则库) |
| `learn.improve.lookbackDays`(30)、`learn.improve.minTroubles`(3)、`learn.improve.minHeldOutCases`(2)、`learn.improve.repeats`(3) | `learn improve`：归纳出问题的来源的天数、调用模型所需的最少来源数、未参与改进的评测用例的最少个数、评测中每个用例每个变体的运行次数 |
| `learn.controls.weeks`(3)、`learn.controls.firstPassFloor`(0.5)、`learn.controls.corrections`(3) | 控制措施：一次通过率连续偏低的周数与下限、一周内用户纠正同一类自动决定的次数 |
| `health.missedWindowMinutes`、`health.deployToShallowMinutes`、`health.dataDirMaxBytes` | 链路健康检查的阈值 |

分诊证据检查使用的含糊措辞词表不在 `thresholds` 中，写在条目表 `evaluation/rubrics/triage.json` 对应条目的 `params` 里，生产与评测共用。

### 5.3 本机用户配置

`~/.config/tightrein/config.yaml`，配置的第 4 层，只放因人而异、不应写进工作区的值。只允许下表的键，出现其他键(例如阈值、上限、预算)时报出键名并退出，避免个人配置悄悄改变项目的行为：

| 键 | 含义 |
|---|---|
| `branchPrefix` | 个人分支前缀 |
| `defaultWorkspace` | 省略 `--workspace` 时使用的工作区 |
| `notify.method` | 本机通知方式：`macos`、`none` |
| `tools.<工具>.path` | 各 agent 工具的可执行文件路径；`tools.semgrep.path` 为 Semgrep 的命令，覆盖 `runtime.tools.semgrep`，含 `/` 的相对路径相对本工具仓库根目录解析，例如 `local/semgrep/bin/semgrep` |
| `install.targets.<工具>.path`、`install.targets.<工具>.enabled` | skill 的安装位置与是否安装到该工具 |
| `models.<别名>` | 模型别名，结构同 5.2；同一模型在各别名中的价格须一致 |
| `routes.<调用点>` | 调用点 → 别名，键同 5.2(`default`、调用点与条件变体)；上限、预算、路径模式属于项目，不在这里 |
| `network.proxy` | 代理地址，例如 `http://127.0.0.1:8118`；带用户名或密码时按凭证处理(登记到脱敏器，agent 子进程的环境不含它) |
| `network.noProxy` | 直连的主机或域名后缀列表，例如 GitHub 的各域名 |

`models`、`routes` 在合并时位于技术栈层与 `project.yaml` 之间(5.1)，旧的 `agents` 段出现时报出新写法；各工具都由核心的适配器(02 篇)以子进程调用。

**网络代理**：配置了 `network.proxy` 时，组装根生成一次子进程环境：`http_proxy`、`https_proxy`、`HTTP_PROXY`、`HTTPS_PROXY` 为代理地址，`no_proxy`、`NO_PROXY` 为 `network.noProxy` 加本机回环地址(`localhost`、`127.0.0.1`、`::1`，本机服务总是直连)；git、gh、agent 工具、扩展、Schemathesis、Playwright、Semgrep、本机服务都使用这份环境，核心自己的 HTTP 请求(登录、重放、健康检查、local-run 就绪检查、第三方 skill 下载)按同一份环境的代理表发送并遵守 `noProxy`。没有配置时沿用当前进程环境中的代理变量。

### 5.4 凭证

- 测试账号密码只存在 macOS 钥匙串中，配置里只写条目名。
- 读取凭证只在需要登录的探针与验证步骤中进行，读到的值只在内存中使用，不写入任何文件、日志或交接文档。
- agent 执行器启动 agent 进程时，环境变量中不包含任何凭证(9.5)。

### 5.5 环境变量

**执行器传给 agent 进程**(环境由 `guards.credentials.build_env` 生成，都不含凭证)

| 变量 | 含义 |
|---|---|
| `TIGHTREIN_WORKSPACE` | 工作区绝对路径，供 `kb` 命令与 MCP 服务定位工作区 |
| `TIGHTREIN_RUN_ID`、`TIGHTREIN_TRACE_ID`、`TIGHTREIN_PARENT_SPAN_ID` | 使 agent 调用 `kb` 时产生的事件挂到当前运行的 trace 下；`admin eval seal` 与 `admin eval add` 检测到 `TIGHTREIN_RUN_ID` 时拒绝执行 |
| `TIGHTREIN_SANDBOX` | `--output` 模式下为 `1` |
| `GIT_TERMINAL_PROMPT` | 固定为 `0` |
| `GIT_OPTIONAL_LOCKS` | 只读任务为 `0`，使只读 git 命令不刷新索引 |

**探针与验证的子进程**

| 变量 | 进程 | 含义 |
|---|---|---|
| `TIGHTREIN_TOKEN` | Schemathesis | 当前角色的 JWT，由 `schemathesis.toml` 的 `headers` 以 `${TIGHTREIN_TOKEN}` 引用；不出现在命令行参数中 |
| `TIGHTREIN_AUTHZ_MODEL`、`TIGHTREIN_ROLE` | Schemathesis | 越权检查的数据文件路径与当前角色 |
| `TIGHTREIN_E2E_PLAN` | Playwright | 本次运行的计划文件路径 |
| `TIGHTREIN_PASSWORD_<角色>` | Playwright | 各角色的密码，供 setup 走登录页生成登录态 |
| `TIGHTREIN_EXTENSION_POINT`、`TIGHTREIN_EXTENSION_PROTOCOL`、`TIGHTREIN_CACHE_DIR` | 扩展 | 扩展点名、协议版本、长期缓存目录；其余环境变量经过凭证清除，另加 `stack.yaml` 中声明的非敏感变量(10 篇 2.5) |
| `local-run` 输出的 `services[].env` | 本机启动的服务 | 扩展给出的非敏感变量，示例项目中的取值为 `ASPNETCORE_ENVIRONMENT=Development`；其余环境变量经过凭证清除 |

**vcs 执行 git 与 gh**：固定设置 `GIT_TERMINAL_PROMPT=0`、`LC_ALL=C`，避免交互提示与本地化输出影响解析。

## 6. observability

### 6.1 文件划分

```
observability/
  events.py         事件结构与写入(一行一个 JSON)
  tracing.py        trace 与 span 的上下文管理：start_span、end_span，自动计时
  redact.py         脱敏规则
  notify.py         本机通知与去重
```

### 6.2 事件

字段见 10.5，另有 `attributes`：JSON 对象，放各操作特有的补充信息，例如检索的查询串与返回编号，写入前同样经过脱敏。写入规则：

- 每个 span 在结束时写一行，包含开始时间与耗时；关卡判定(`gate`)与用户操作(`user_action`)是瞬时事件，写一行。
- 事件写入前经过 `redact.py`：匹配到 token、密码、连接串、身份证号与手机号格式、钥匙串读出的值，一律替换为 `[已脱敏]`。
- `--output` 模式下事件写入 `<输出目录>/events.jsonl`，不写 `data/logs/`，避免评测运行混入生产统计。
- 写入失败不影响主流程，但会在运行摘要中报告「日志写入失败」及原因。

### 6.3 通知

`notify(event_type, subject_id, text)` 供流水线模块与编排层共用：

- 方式取本机用户配置的 `notify.method`：`macos` 时调用 `osascript` 显示通知，`none` 时不发。
- 以「事件类型 + 对象 + 日期」为幂等键写入 `idempotency_keys` 去重，同一天同一事件只通知一次。
- 通知失败不影响主流程，失败原因写入运行摘要的「异常」一节。

### 6.4 与 store 的分工

事件日志只追加、不修改，用于统计与追查；业务状态只在数据库中维护。`learn`(含 `learn improve`)同时读取两者：状态与结果读数据库，过程与耗时读事件日志。

## 7. 外部依赖

| 依赖 | 版本与安装 | 使用者 |
|---|---|---|
| Python | 3.10 及以上；需要加载 `sqlite-vec` 时使用支持 `enable_load_extension` 的 Python(例如 Homebrew 安装的版本) | 核心 |
| `mcp`(PyPI) | 2.x | `admin kb mcp` 服务 |
| `schemathesis`(PyPI) | 锁定版本，作为核心 Python 环境的依赖安装 | api-fuzz 探针 |
| `sqlite-vec` | 只在满足 16.9 的条件后引入 | 向量检索 |
| `@playwright/test` | 锁定版本，安装在 `pipeline/checks/pages/runtime/` | 验证环节的页面巡检、截图与页面类复现检查 |
| 技术栈扩展的外部工具 | 由各技术栈的 `stack.yaml` 的 `requires` 声明，缺失时扩展以 `tool-missing` 报告并给出安装命令；aspnetcore 所需工具见 10 篇第 5 章 | 技术栈扩展 |
| Semgrep | 社区版 | 静态巡检、静态类复现检查 |
| git、gh | gh 需已登录 | `vcs` |
| Claude Code、Codex CLI、Antigravity CLI(agy) | 各自的本机登录状态 | `runner` 的适配器 |

## 8. 测试

| 组件 | 测试方式 |
|---|---|
| `domain` | 单元测试覆盖全部纯函数与两个状态机的转换表：每个合法转换一条用例，每类非法转换一条用例 |
| `contracts` | 每个 schema 至少一个合法样例与一个非法样例；每个升级函数一条用例 |
| `store` | 使用临时目录与临时数据库；测试迁移可以从空库完整执行、仓储的读写与查询、锁的获取与超时接管、幂等键的跳过 |
| `config` | 合法与缺字段的 `project.yaml` 样例；`thresholds` 中的值越出 `min`、`max` 时报错；四层合并：每层覆盖上一层、列表整体替换与 `+` 追加、两个技术栈给同一标量不同值时报错、技术栈层出现核心没有的键时报错、用户配置出现非个人键时报错；`project config` 显示的来源层与实际覆盖一致；`defaults.yaml` 中每个键都有注释，且核心代码中不存在与这些键对应的写死默认值(以键名检索) |
| `observability` | 脱敏规则的正反样例；span 嵌套时 `parent_span_id` 正确；通知的幂等去重，`notify.method=none` 时不调用外部命令 |

测试目录分为 `core/tests/unit/`(纯函数与单个组件)、`core/tests/replay/`(回放夹具)与 `core/tests/integration/`(需要真实外部工具的测试，工具缺失时跳过)。
