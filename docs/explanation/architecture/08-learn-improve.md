# 流水线层：learn 与自我改进

本篇描述流水线层最后一个模块 `learn` 与它的自我改进部分(`pipeline/improve/`)的内部设计。业务规则见
`docs/explanation/design/08-learn.md`(学习)、`12-agent-scoring.md`(评分只作衡量)与 `14-improve.md`(自我改进只建议)，当前设计以
`docs/explanation/redesign/08-learn.md` 为准。

两者只读取数据库、交接文档与事件日志做统计，写入范围严格受限：指标快照、学习建议与决定文档、经验条目与缺陷模式条目(经写入去重)、
工作区规则库 `rules/`、分诊结论与环节效益的回填、周报。不修改任何 skill、配置或代码，没有合入补丁或改配置的执行路径。

编号、枚举、实体、表、路径与配置沿用 `01-foundation.md` 的定义。

## 1. 职责与边界

| 部分 | 负责 | 不负责 |
|---|---|---|
| `learn` | 指标计算与快照(按通道与模型的修复指标)；周报；出问题时写经验；经验类条目的自动清理与其他条目的复核建议；缺陷变规则；控制措施建议；链路健康检查；第三方 skill 核实；学习建议的存储、接受与拒绝 | 执行复现检查(随 `collect` 与 `verify` 运行)；判定问题回归与重开 Issue(`aggregate` 与 `issue`)；保存运行即评测的用例(`verify` 在部署后确认通过时保存) |
| `improve` | 从出问题的来源归纳一条改进建议(`improvement-writer`)；可改范围检查；经 `evaluation` 对比参与改进与未参与改进的用例；写决定文档与补丁 | 应用补丁、修改配置；修改评测集；被编排触发(只由用户运行 `learn improve`) |

两者遵守 00-overview 的依赖规则：不调用其他流水线模块，只通过数据库记录与交接文档衔接；调用 LLM 只经过 `runner`(各调用照常登记
环节效益)；对 git 只做只读查询。

**用到的能力**

| 部分 | 能力 | 用途 |
|---|---|---|
| `learn` | `retrieval` | 写入经验与缺陷模式条目(带写入去重)、`stale` 复核、归档与续期、预取同类已有条目 |
| `learn` | `runner` | `lesson-writer`(写经验、比对同类条目)、`rule-writer`(写 Semgrep 规则) |
| `learn` | `vcs`(只读)与 Semgrep | 取修复前后的文件与补丁，在临时目录与只读 worktree 上验证规则 |
| `improve` | `runner` | `improvement-writer` |
| `improve` | `evaluation` | 当前版本与候选的对比评测(候选为 HEAD 加补丁，或另一个能力档的模型) |

## 2. 文件划分

```
pipeline/learn/
  service.py              子命令入口：report、metrics、health、lessons、curate、suggestions、accept、reject
  steps/
    metric_base.py        MetricValue、MetricContext、维度写法、时间窗内以已修复关闭的 Issue
    metrics.py            各项指标与登记表 METRICS(指标名、环节、函数)；快照与趋势
    fix_metrics.py        一次通过率(按通道、模型)、每个修复的费用、被撤销的比例、用户纠正次数
    weeks.py              时间窗与工作日；定时任务的应执行时刻(编排复用)
    attention.py          周报「下一步」中需要处理的事项
    outcomes.py           分诊结论回填：判为误报的问题之后不再出现即判对
    yields.py             环节效益的有效产出回填与汇总
    troubles.py           出问题的来源(经验总结与自我改进共用)
    lessons.py            出问题时写经验
    curate.py             经验类条目的自动归档与续期；其他条目的复核建议；同类条目的矛盾与重复比对
    rules.py              缺陷变规则：取材料、运行 rule-writer、收录或写成缺陷模式条目
    rule_check.py         用原补丁验证 Semgrep 规则
    controls.py           分数驱动的控制措施建议
    suggestions.py        采集配置与覆盖缺口建议的生成；建议的存储、接受、拒绝与过期
    health.py             链路健康的六项检查
    third_party.py        第三方 skill 的每月核实
  prompts/
    common.py             LearnPrompt、LearnCalls、LearnEnv；角色名与 schema
    lesson.py             lesson-writer 的写经验与比对任务
    rule.py               rule-writer 的任务
  render/
    weekly.py             周报(result 类型交接文档)与通知文字
    decision.py           控制措施与改进建议的决定文档(decision 类型)
pipeline/improve/
  prompts.py              improvement-writer 的任务：出问题的来源与可改范围
  service.py              ImproveService.suggest：归纳、校验、评测、推荐、写决定文档与建议
```

`skills/learn/`：`SKILL.md`(命令、周报的读法、学习建议的处理)、`references/knowledge-curator.md`(写入去重)与
`references/roles/` 下 `lesson-writer.md`、`rule-writer.md`、`improvement-writer.md`。没有单独的 `skills/improve/`。

## 3. 命令

| 命令 | 作用 | 编排中的调用时机 |
|---|---|---|
| `learn report [--week <日期>]` | 回填 → 指标与快照 → 经验清理 → 建议(采集配置、覆盖缺口、知识复核、控制措施) → 过期 → 需要处理的事项 → 健康检查 → 第三方核实 → 交接文档 → 周报 → 通知 | 每周第一个工作日 |
| `learn metrics [--since] [--until] [--stage <环节>]` | 只计算并输出指标，不写快照 | 手动 |
| `learn health` | 链路健康，交接文档中标出需要立即通知的项 | 每次运行结束 |
| `learn lessons` | 回填，出问题时写经验，缺陷变规则 | 每次运行结束 |
| `learn curate` | 经验清理与知识库复核 | 包含在 `learn report` 中 |
| `learn improve [--days N]` | 自我改进的建议(第 8 节) | 不调用，只由用户运行 |
| `learn suggestions [--status] [--kind]` | 列出学习建议 | 手动 |
| `learn accept <编号> [--action renew\|merge\|archive]` | 记录批准；知识复核按 `--action` 处理条目 | 手动 |
| `learn reject <编号> --reason <原因>` | 记录拒绝 | 手动 |

`--output` 模式下不写数据库与知识，不运行缺陷变规则，交接文档与周报写到输出目录。

## 4. 指标

- **时间窗**：一周为本机时区的周一 00:00 到下周一 00:00，换算为 UTC 后查询。
- **结果形式**：`MetricValue(metric, dimension, value, numerator, denominator, sample_size)`；比率分母为 0 时 `value` 为空，周报显示
  「无样本」。维度写成 `probe=api-fuzz`、`lane=fast`、`model=<模型>`、`kind=<类型>` 这样的键值串，总计为 `all`。
- **快照**：`learn report` 把本周全部指标写入 `metric_snapshots`，重跑时按(周、指标、维度)覆盖；趋势与控制措施从快照读取。
- **登记表** `metrics.METRICS` 给出每项指标所属的环节，`learn metrics --stage` 按它筛选；单项出错时记入 `errors`，其余照常。

| 指标 | 计算方法 |
|---|---|
| 新发现问题数、回归的问题数、接口覆盖率、噪声占比与数量 | 问题事件、运行的覆盖范围、信号的归并状态 |
| 分诊准确率、误判为不成立 | `triage_results.outcome` 本周回填的记录；修复前复现不了(Issue 事件 `not-reproduced`)时回填 `false-confirm` |
| 人工队列积压与最长等待 | 每个问题最新一次分诊结论 |
| 各段耗时 | 本周完成的 Issue 逐段取时间点求中位数 |
| 验证一次通过率、改动量分布 | verify 与 fix 的交接文档 |
| 已修复数、回归率、PR 被拒率 | Issue 事件与 PR 记录 |
| `first-pass` | 每个 Issue 第一份带轮次的修复交接文档在本周的，`rounds[0].checksPassed` 为真即一次通过；维度 `lane=`(交接文档的 lane)与 `model=`(同一运行中 `fix-executor` 或 `fix-session` 在 `stage_yield` 中记下的模型，没有时为 `unknown`) |
| `fix-cost-usd` | 本周以已修复关闭的 Issue，其 fix、verify、release 环节全部调用的费用之和，分子为合计、分母为 Issue 数；维度 `lane=` |
| `revert-rate` | `learn.regressionWindowDays` 天内以已修复关闭的 Issue 中，撤销 PR 操作已确认或已执行的比例；维度 `lane=` |
| `user-corrections` | 本周驳回修复计划(`plan-rejected`)、用户关闭 Issue(`issue-closed`)、确认撤销 PR(`reverted`)、改判分诊(`triage-overridden`)的次数；维度 `kind=` |
| token、费用与耗时 | 事件日志与运行记录；工具未提供费用的调用按 token 估算并单列 |

`stage_yield` 登记时记下实际使用的工具与模型(`RunnerResult.tool`、`model`，迁移 012 之前的行为空)。

## 5. 周报

`render/weekly.build` 由 learn 交接文档的 `outputs` 组装 result 类型的交接文档：编号 `weekly-<周一>`，`from: learn`、`to: user`、
`subject: <周一>`，写到 `data/reports/weekly-<周一>.md`(`store/files/documents.write`，按 `project.language`)。

| 小节 | 来源 |
|---|---|
| 结论 | 新发现、已修复、回归(与上周快照对比)，一次通过率，需要处理的事项、健康异常与待处理的建议数 |
| 做了什么 | 统计周期与生成时间，绝对日期并注明时区 |
| 产出 | 指标表(本周、上周、趋势、样本数)、环节效益表、`rules`(本周处理完的缺陷变规则结果)、`cleanup`、待处理的建议 |
| 证据 | 健康与第三方核实中未通过的项；逐项结果写数据块 `checks`(每项 passed 或 failed) |
| 偏离与原因 | `errors`：统计出错的项 |
| 需要决定 | 每条待处理的建议一项，带 `advice` 时用其中的推荐与理由 |
| 下一步 | 需要处理的事项与命令，owner 为 user |
| 引用 | learn 的 JSON 交接文档 |

`weekly.summary` 给出通知文字。周报可用 `tightrein doc check` 校验。

## 6. 经验、清理与缺陷变规则

### 6.1 出问题的来源

`steps/troubles.collect(conn, layout, since=None)` 返回 `Trouble(kind, keys, subject_type, subject_id, title, text, at, run_id)`：

| kind | 来源 | 幂等键 |
|---|---|---|
| `triage-misjudged` | `triage_results.outcome` 为误判为成立、误判为不成立或用户改判(改判的上一次结论已判为误判时由那一条负责) | `lesson:triage:<问题>:<次数>` |
| `review-rejected` | PR 上 `CHANGES_REQUESTED` 的评审(同一 Issue 合为一条)；修复交接文档中评审不通过的轮次(阻断项) | `lesson:pr:<Issue>:<评审>`、`lesson:review:<Issue>:<运行>-<轮次>` |
| `fix-failed` | Issue 事件 `fix-held` | `lesson:fix-held:<Issue>:<事件编号>` |
| `reverted` | 撤销 PR 操作(未被拒绝)，原因取发布交接文档或操作说明 | `lesson:revert:<Issue>:<操作编号>` |
| `plan-rejected` | 被拒绝的修复计划操作，取用户的说明 | `lesson:plan:<Issue>:<操作编号>` |
| `issue-closed` | Issue 事件 `user-closed`，取关闭原因与说明 | `lesson:closed:<Issue>:<事件编号>` |

普通评审意见、批准与正常完成的运行不是来源。

### 6.2 写经验

`learn lessons` 对幂等键未完成的来源运行 `lesson-writer`(预取同类型的已有条目摘要，分诊误判附发现报告，其余附修复报告)，草稿经
`KnowledgeService.write`(写入去重)写入：分诊误判为 `triage-lesson`，其余为 `fix-lesson`。程序在正文末尾追加「## 来源」(类型、对象、
运行)，`reviewBy` 为写入日加 `learn.lessonReviewDays`。成功或草稿为空时标记幂等键；失败时下次重试，同一对象累计失败
`learn.lessonRetries` 次后放弃，列入「需要手写的经验」。

### 6.3 清理与复核

- `curate.cleanup`：经验类条目(`triage-lesson`、`fix-lesson`)过了 `reviewBy` 且最近 `retrieval.unusedDays` 天没有被命中的归档，
  仍被命中的续期；结果写进交接文档的 `cleanup`。
- `curate.review_drafts`：其他类型过期与长期未命中的条目各一条 `knowledge-review` 建议；同类型、标签相近的候选组经 `lesson-writer`
  比对，矛盾组与重复组各一条建议。

### 6.4 缺陷变规则

`rules.generate` 在 `learn lessons` 末尾执行，处理 `learn.rules.lookbackDays` 天内以已修复关闭、不是用户需求、幂等键 `rule:<Issue>`
未完成的 Issue：

1. 取修复交接文档的 `baseCommit` 与 `changedFiles`(去掉 `testPaths` 匹配的文件)、PR 的合并提交，以及两者之间这些文件的补丁；
   缺任何一项时记为不生成规则(写入幂等键)。
2. 运行 `rule-writer`(工作目录为只读 worktree；`semgrep-rule-variant-creator` 已安装时加载)；没有结果时记入 `errors`，下次重试。
3. `rule_check.check`：规则 YAML 只含一条带 `id`、`message`、`languages` 与模式键的规则；改动文件的修复前版本(`git show <base>:<路径>`)
   与修复后版本(`<合并提交>`)分别写到 `data/runs/<运行>/rules/<Issue>/` 下，Semgrep 在修复前至少命中一处、修复后不命中；再在只读
   worktree 全仓库运行，命中数不超过 `learn.rules.maxRepoHits`。Semgrep 命令与超时同静态巡检。
4. 通过：写 `rules/<Issue>-<简称>.yaml`，头部注释写来源 Issue、运行与三项验证数字。不通过或表达不了：不收录并写明原因，缺陷模式描述
   (现象、根因写法、检出方法、反例、适用目录、来源)作为 `defect-pattern` 条目写入知识库。
5. 结果存进幂等键；周报读取本周完成的结果。

静态巡检(`sources/static/probe.py`)在工作区 `rules/` 有规则时单独运行一次 Semgrep，命中经 `mapping.rule_signals` 直接映射为信号
(`check` 为 `rule:<规则编号>`，`location` 为 `文件:行`，`context.rule` 为规则编号与消息)，不经审查与取证；运行失败时巡检为 partial。
`variant-scan` 继续以知识库中 active 的缺陷模式为种子。

## 7. 学习建议与控制措施

| 类型 | 生成 | 接受 |
|---|---|---|
| `probe-config` | 同一检查本周被判误报、作废或间歇未复现的信号占比超过 `learn.noisyCheckRatio`，样本不少于 `learn.minSamples` | 只记录决定 |
| `coverage-gap` | 接口描述中连续 `learn.coverageGapWeeks` 周没有被覆盖的接口 | 只记录决定 |
| `knowledge-review` | 6.3 | 按 `--action` 续期、归档或合并 |
| `control` | `steps/controls.py`(周报中执行) | 只记录决定 |
| `improvement` | `learn improve`(第 8 节) | 只记录决定 |

- **存储**：同一类型、同一对象已有待处理的不再生成；被拒绝过的，只有出现拒绝时没有的证据对象才再生成。带决定文档的建议
  (`control`、`improvement`)写 `data/improve/<建议编号>.md`(decision 类型：背景、选项 accept 与 reject、推荐与理由、下一步的应用方法)，
  建议记录的 `target_path` 指向它；接受或拒绝时在文档历史中记下决定并把状态改为完成。
- **控制措施**：
  - 模型一次通过率：`first-pass` 的 `model=` 维度连续 `learn.controls.weeks` 周(含本周，取快照)样本都不少于 `learn.minSamples` 且都低于
    `learn.controls.firstPassFloor` 时，建议写代码的角色升档或换模型，给出配置键 `stages.fix.roles.fix-executor.capability` 与对比评测的命令；
  - 用户频繁纠正：本周 `plan-rejected`、`issue-closed`、`reverted` 分别达到 `learn.controls.corrections` 次，且对应关卡 `plan-confirm`、
    `issue-approve`、`merge` 为 `auto` 时，建议改为 `user`；
  - 连续失败停下转待决定由编排的熔断完成，不生成建议。
- **收件箱**：`orchestrator/inbox.py` 为每条待处理的建议列一项(kind `learn-suggestion`，命令 `tightrein learn accept <编号>`，推荐取建议中的
  `advice`)，每日汇总随之列出。
- 待处理超过 `learn.suggestionExpiryWeeks` 周的标为过期。

## 8. 自我改进

`ImproveService.suggest(days=None)`：

1. `troubles.collect` 取 `learn.improve.lookbackDays`(或 `--days`)天内出问题的来源，少于 `learn.improve.minTroubles` 条时不调用模型。
2. 运行 `improvement-writer`(工作目录为本工具的 `skills/`，只读)，输出 `suggestion` 为空(写明原因)或
   `{stage, target: prompt|model, patch, capability, rationale, addresses[], expected}`。
3. 校验：prompt 类补丁只改 `skills/` 下的文件，且不触及 `evaluation.versions.FORBIDDEN_PATTERNS`；model 类的能力档对应的模型与当前不同。
4. 用例：注入的 `cases(stage)` 取该环节校验通过的用例(`evaluation.cases.verify_cases`：`evals/` 中封存的用例，fix 另加运行即评测的用例
   `data/eval/cases/`)。来源对象在 `addresses` 或出问题的来源中的为「参与改进」，其余为「未参与改进」；后者少于
   `learn.improve.minHeldOutCases` 时不出建议并写明原因。
5. 评测：注入的 `evaluate(plan)`；prompt 类以 HEAD 加补丁为候选(`version_plan`)，model 类以候选能力档的模型对比当前模型
   (`tool_model_plan`)，每个用例每个变体 `learn.improve.repeats` 次。
6. 汇总两组的确定性项(code 评分项)通过率与平均分。评测完整、未参与改进的用例确定性通过率没有下降、参与改进的有提升时推荐批准，
   否则推荐拒绝并写明原因。
7. 写 `data/improve/<建议编号>.md` 与 `.patch`，建议记录 kind `improvement`(`diff` 为补丁)；结果写进 learn 交接文档的 `improve`。

接受只记录决定，应用方法写在决定文档的「下一步」：prompt 类在本工具仓库 `git apply` 补丁并提交，model 类在工作区配置中修改能力档。
`cli/assemble.App.improve` 组装依赖：评测与 `eval run` 使用同一套依赖。

## 9. 读写的表与文件

| 对象 | learn | improve |
|---|---|---|
| `runs`、`signals`、`problems`、`problem_events`、`triage_results`、`issues`、`issue_events`、`pulls`、`deployments`、`pending_operations` | 读；回填 `triage_results.outcome` | 读 |
| `handoffs` | 读 fix、verify、release 的记录；写本模块记录 | 写本模块记录 |
| `stage_yield` | 读；回填有效产出 | 经 runner 登记 |
| `suggestions` | 读写 | 写 `improvement` |
| `metric_snapshots` | 读写 | — |
| `knowledge_meta`、`knowledge/**` | 经 `retrieval` 写经验与缺陷模式条目、归档与续期 | — |
| `idempotency_keys` | 写(`lesson:`、`rule:`) | — |
| 工作区 `rules/` | 写 | — |
| `data/reports/weekly-<周一>.md` | 写 | — |
| `data/improve/` | 写控制措施的决定文档 | 写改进建议的决定文档与补丁 |
| `data/runs/<运行>/rules/` | 规则验证的临时文件 | — |
| `data/eval/cases/`、`evals/**` | — | 经 `evaluation` 只读 |
| `data/evals/<评测编号>/` | — | 由 `evaluation` 写 |

## 10. 错误处理

| 情况 | 处理 |
|---|---|
| 某项指标的来源缺失或查询出错 | 该项记入 `errors`，周报「偏离与原因」列出，其余指标照常 |
| 分母为 0 | 显示「无样本」，不计入趋势 |
| 执行器没有结果(经验、规则、比对、改进) | 经验与规则不标记幂等键，下次重试；经验连续失败达到上限后放弃并列入需要处理的事项；比对没有结果的组记入 `errors`；改进写明原因不出建议 |
| 写入知识失败 | 经验记入 `errors`；规则的结果中写明缺陷模式写入失败的原因 |
| Semgrep 失败或输出无法解析 | 规则判为验证不通过，写明原因 |
| 评测不完整或有用例明显变差 | 改进建议推荐拒绝并写明原因 |
| 通知发送失败 | 周报与交接文档照常写入 |

## 11. 测试

| 对象 | 测试文件 |
|---|---|
| 指标(含按通道与模型的修复指标) | `tests/unit/pipeline/test_learn_metrics.py`、`test_learn_fix_metrics.py` |
| 出问题的来源、经验、清理与复核 | `test_learn_troubles.py`、`test_learn_lessons.py`、`test_learn_curate.py` |
| 缺陷变规则与规则验证 | `test_learn_rules.py`(假执行器、假 git 与假 Semgrep)；静态巡检的规则库 `tests/unit/sources/test_static_probe.py` |
| 控制措施、建议的存储与决定文档 | `test_learn_controls.py`、`test_learn_suggestions.py` |
| 周报与 `--output` 模式 | `test_learn_service.py`(周报经 `documents.check` 校验) |
| 自我改进 | `test_improve_service.py`(注入用例与评测替身) |
| 运行即评测的保存与加载 | `test_verify_staging.py`、`tests/unit/evaluation/test_eval_cases.py` |
| 收件箱中的学习建议 | `tests/unit/orchestrator/test_inbox.py` |

本篇用到的基础层定义(编号、枚举、表、路径、配置)统一见 01-foundation.md。
