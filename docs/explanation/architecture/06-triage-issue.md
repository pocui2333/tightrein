# 流水线层：triage、issue

本篇描述分诊取证(`pipeline/triage`)与提 Issue(`pipeline/issue`)两个模块的内部结构、命令、处理步骤、读写的数据与测试方式。业务规则见 `docs/explanation/design/03-triage.md`、`04-issue.md`，评分见 `12-agent-scoring.md`(评分只作衡量)，规模档与处理标签见 `13-task-scoring.md`；编号、枚举、实体、表、文件布局与配置沿用 `01-foundation.md` 的定义。

## 1. 共同约定

### 1.1 模块内部结构

两个模块都按 `00-overview.md` 3.3 的结构组织：

| 部分 | 职责 | 约束 |
|---|---|---|
| `service.py` | 入口：解析选择器、检查前置条件、加对象锁、逐个对象调用各步骤、写交接文档 | 不含业务判断；判断都在 `steps` 或 `domain` 中 |
| `steps/` | 模块内的各个步骤，每步一个文件；纯函数与有 IO 的步骤分开 | 有 IO 的步骤只通过 `store`、`runner`、`vcs`、`retrieval` 访问外部 |
| `prompts/` | 为需要 LLM 的步骤组装执行器任务(`runner-task`) | 只拼装字段与上下文，提示正文一律取自 `skills/<模块>/references/` |
| `render/` | 由交接文档生成人读文档 | 只读交接文档，不查数据库 |

### 1.2 命令形态

命令遵循 `15-standalone-run.md` 15.2 的统一形态，公共参数 `--select`、`--input`、`--output`、`--dry-run`、`--runner`、`--model` 的含义不再重复；下文只列各命令特有的参数。

### 1.3 评分记录

凡是对 LLM 结果打分的地方，每个评分项写一行 `scores`：`stage`、`run_id`、`subject_id`、`attempt`、`item`(评分项编号)、`result`(`ScoreResult`：`pass`、`fail`、`unknown`、`not-applicable`)、`method`(`ScoreMethod`：`code`、`judge`、`user`)、`detail`。同时在事件日志中写一条 `gate` 事件，`score` 字段为该步骤的汇总结果。

## 2. triage：职责与文件划分

### 2.1 职责

对聚合交来的每个问题给出四个结论：是否成立、根因位置、引入提交、值不值得修，由决策树给出处理标签(`Treatment`)，并据此定出去向(`Disposition`)。整体设计见 [redesign/03-triage.md](../redesign/03-triage.md)。只读代码、只出结论，不改代码、不对外发布。无人值守运行，不向用户提问。

### 2.2 代码目录

```
core/tightrein/pipeline/triage/
  service.py              入口：选择问题、前置条件、每次最多 5 个、逐个处理、落库
  steps/
    case.py               一个问题在各步骤之间传递的状态
    select.py             选取待分诊问题并按预估严重度排序(纯函数 + 查询)
    claims.py             问题转主张：3.3 的模板、附带事实、短标题(纯函数)
    main_diff.py          staging 部署 commit 到 main 最新 commit 之间根因候选文件的提交记录(vcs 只读)
    dedup.py              查重：检索候选、调用查重任务、执行合并
    evidence.py           取证：调用 claim-verifier(static 先复用采集时的取证)、重做循环
    evidence_checks.py    分诊条目表的全部五项与评估检查，均由代码判定(纯函数 + 文件存在性检查)
    refute.py             高风险时的证伪复核(triage.refute)与两次结论的合并规则
    attribution.py        归因：git blame、git log、PR 编号(vcs 只读)
    tradeoff.py           已接受取舍的核对
    rating.py             严重度、规模档、复杂度(调用 domain 纯函数)
    disposition.py        处理标签、去向、Issue 标签、问题状态变化、抑制规则(调用 domain 纯函数)
    persist.py            写 triage_results、problem_events、suppressions.yaml
  prompts/
    common.py             两个取证角色共用的输入组装
    claim_verifier.py     组装 claim-verifier 与 refuter 任务
    dedup.py              组装查重任务
  render/
    findings.py           发现报告 data/findings/<问题编号>.md
    summary.py            运行摘要中分诊一节的条目(交给编排层汇总)
```

### 2.3 skill 目录

```
skills/triage/
  SKILL.md
  references/
    evidence-standard.md
    findings-template.md
    severity.md
    roles/
      claim-verifier.md
      refuter.md
    tasks/
      dedup.md
```

| 文件 | 内容要点 |
|---|---|
| `SKILL.md` | 何时使用(分诊新发现与回归问题、查看发现报告、处理人工队列、改判)；可用命令与参数；如何解读运行结果：四档判定、去向、人工队列的含义；人工队列的处理方式(补充信息后 `problem retriage --note`，或直接 `problem retriage --verdict`)；明确 skill 本身不做判断，判断都由 `tightrein triage` 完成 |
| `references/evidence-standard.md` | 取证底线，写给所有分诊角色：每条事实带 `文件路径:行号`；只写读到的；打开文件读，不凭匹配行下判断；同一概念试多种命名；数字要有来源，读不到标「未确认」；「证据不足」写清缺什么即合格；缺陷类四步与「追到入口」的反证要求；前端拦截不算反证；判不成立必须指出挡住它的代码位置或现象来源(引用主张中的某条事实)；反证检查每条写明入口位置与上游有无校验；预估改动的文件要数出来、不含测试文件；反模式清单。与 12.2 分诊的验收标准逐项对应，五项都由代码判定；不要求交付前再验证一遍 |
| `references/findings-template.md` | 发现报告的结构说明：frontmatter 字段、各节标题与每节取自交接文档的哪个字段、写作要求(第一句是结论、带位置、绝对日期)。`render/findings.py` 的输出须与本文件的节标题一致，由单元测试比对 |
| `references/severity.md` | 严重度 P0 到 P3 的标准与 Issue 报告的写法(4.5) |
| `references/roles/*.md` | 两个角色的说明，见 2.4 |
| `references/tasks/dedup.md` | 查重任务说明：给定本问题的主张与若干候选(未关闭 Issue、已分诊问题的根因与标题)，判断是否同一根因；必须给出能说明同一根因的代码位置；拿不准时判为不同 |

### 2.4 角色说明的迁移

两个角色说明都由原先放在被测项目 `.claude/` 下的文件迁移而来；原 `auto-probe` 中的容量分析(响应过慢)与价值评估(值不值得修)不单独成为角色，并入 `claim-verifier` 的一次取证。迁移时统一做以下处理：

| 统一处理 | 说明 |
|---|---|
| 删去 YAML frontmatter 中的 `tools` 字段 | 可用工具由执行器任务的 `access` 与 `allowedCommands` 决定，适配器负责翻译成各工具的机制 |
| 「主会话」改为「本工具」 | 调用方是核心程序，不是某个对话会话 |
| 「先读 `ARCHITECTURE.md`」改为「先读任务说明中给出的项目参考条目」 | 项目结构说明作为工作区 `knowledge/` 中的参考条目(RF)由 `retrieval` 预取，角色说明中不写死任何项目文件名 |
| 引用项目规范条款编号的地方，改为直接陈述规则本身 | 例如「任务外发现只列出，不在本次处理」，不再写条款号 |
| Markdown 输出格式改为 JSON 输出 | 输出由 `contracts/schemas/runner/roles/<角色>.schema.json` 约束；角色说明中保留每个字段的填写要求，不再给 Markdown 模板 |
| 项目专属的技术细节移入工作区知识库 | 例如调用链层次、连接池配置位置、权限矩阵文件名，作为参考条目(RF)与字段契约(CT)按需预取 |

| 角色 | 来源 | 保留的规则 | 删去或改写的内容 | 新增的内容 |
|---|---|---|---|---|
| `claim-verifier` | `.claude/agents/auto-probe/claim-verifier.md` | 不是来论证主张成立的，「不成立」同样有价值；主张一律当待验证的假设；判定走完四步(找代码、读懂实际逻辑、找触发条件、找反证)，反证必须追到入口；四档判定及每档的必填要求；不确定时选「证据不足」；判不成立须说明现象的真实来源；任务外发现单列，不混进主判定；只读，不运行构建与测试；找不到相关代码如实说明 | `tools` 字段；`ARCHITECTURE.md`；「主张可能来自用户的观察」一段改为「主张由本工具根据探针观察到的事实生成」；Markdown 输出格式 | `impact.kind`(影响类别，从固定枚举中选择，供严重度计算)；`fixedOnMain`(给定的 main 差异事实显示根因已被修改时，指出修改它的提交与依据)；`tradeoffHit`(命中上下文中某条已接受的取舍时给出编号)；`missingInfo`(判为证据不足时缺少的信息，区分「代码中可查」与「只有用户知道」)；`counterEvidence` 每条增加 `entry`(入口位置)与 `upstreamValidation`(`present` 或 `absent`，`present` 时带位置)；`sourceOfPhenomenon` 改为对象：`location`(挡住问题的代码位置)或 `factRef`(主张中某条事实的序号)二选一，另有 `explanation`；最前面的 `analysis`(自由分析，结构化字段在它之后)；`assessment`(价值判断与理由、任务类型、预估改动、修复方向、三类标记、重估条件，见 4.9)；响应过慢类同样由它取证 |
| `refuter` | `.claude/skills/auto-dev/workflows/auto-dev-exec.js` 中「对抗复核」一段的内联提示，以及 `claim-verifier.md` 的取证规则 | 自己打开代码核实，不凭描述判断；与第一次取证互相独立；`claim-verifier` 的四步、四档判定与输出字段 | 多视角(`refuterLenses`)与多数投票；「尝试驳倒这条发现」的任务方向；「不确定时默认判为已驳倒」；内联的 JSON schema | 任务方向为「专门寻找理由反驳这条主张，同时如实承认反驳不了的地方」；不确定时判「证据不足」；看不到第一次的判定；输出 schema 与 `claim-verifier` 相同 |

## 3. triage：命令、前置条件与选择

### 3.1 命令

| 命令 | 特有参数 | 作用 |
|---|---|---|
| `tightrein triage` | `--limit <n>`(默认取 `thresholds.triage.perRun`，即 5)、`--commit <commit>`(在指定 commit 上取证，默认 `origin/main` 最新) | 分诊选中的问题 |
| `tightrein problem retriage <问题>` | 无额外参数 | 对已分诊的问题重新分诊，新结论的 `attempt` 加一 |
| `tightrein problem retriage <问题> --note <补充信息>` | `--note` | 把用户补充的信息作为「用户提供的事实」加入主张后重新分诊，用于人工队列 |
| `tightrein problem retriage <问题> --verdict <判定> --reason <原因>` | `--verdict`、`--reason`、可选 `--severity`、`--disposition` | 用户改判：不调用任何角色，直接写入新的分诊结论，`outcome` 记为 `overridden` |
| `tightrein triage queue` | 无 | 列出人工队列中的问题：标题、判定、缺少的信息、发现报告路径 |

### 3.2 前置条件

| 条件 | 不满足时 |
|---|---|
| 问题状态为 `new` 或 `regressed`；或 `problem_events` 中有未处理(`handled_at` 为空)的 `problem retriage-requested` 事件；或由 `problem retriage` 指定 | 「P-0042 已分诊，重新分诊请用 `problem retriage`」 |
| 问题没有被合并为别名 | 「P-0042 已并入 P-0031」 |
| 只读 worktree 可以切换到取证 commit | 调用与 `collect` 共用的只读 worktree 同步函数切换；切换失败时停止并提示「先执行 `tightrein project worktree sync`」 |
| 当天 `triage` 的费用累计未超过 `stages.triage.budgetPerDay` | 停止启动新的问题，运行摘要中说明 |
| 能获取问题的对象锁 | 跳过该问题，交接文档不写，运行摘要中列出「正被其他运行处理」 |

`--output` 模式下可以用 `--ignore-state` 跳过第一条。

### 3.3 选择与排序

`steps/select.py` 按以下顺序取前 `--limit` 个问题，其余留到下一次：

1. 预估严重度为 P0 的问题：api-fuzz 越权类、数据归属类检查失败的问题。
2. 运行报错类：api-fuzz 的 `not_a_server_error` 失败、内部错误(平台上的运行报错与前端错误)。
3. 回归问题。
4. 其他运行时问题(e2e 用例失败、契约不符、响应过慢)。
5. static 与 incidental 问题，以及预估为 P3 的问题。

同一档内按最近出现时间倒序。预估严重度只用于排序，最终严重度由 4.10 计算。

## 4. triage：处理步骤

### 4.1 总览

一次 `triage` 运行生成一个运行编号，对选中的每个问题依次执行 4.2 到 4.10，再执行 4.12 的定去向与落库。

| 步骤 | 实现 | 调用 | 输出 schema |
|---|---|---|---|
| 4.2 组装主张 | `steps/claims.py` | 纯函数 | 内部结构 `Claim` |
| 4.3 main 差异事实 | `steps/main_diff.py` | `vcs` 只读 | 内部结构 `MainDiff` |
| 4.4 查重 | `steps/dedup.py` | 执行器：查重任务 | `runner/tasks/triage-dedup.schema.json` |
| 4.5 取证 | `steps/evidence.py` | 执行器：`claim-verifier` | `runner/roles/claim-verifier.schema.json` |
| 4.6 证据检查 | `steps/evidence_checks.py` | 代码评分器 | — |
| 4.7 证伪复核 | `steps/refute.py` | 执行器：`refuter` | `runner/roles/refuter.schema.json` |
| 4.8 归因 | `steps/attribution.py` | `vcs` 只读 | 内部结构 `Attribution` |
| 4.9 值不值得修 | `steps/tradeoff.py` | 取自取证的 `assessment`；已接受取舍查 `store` | — |
| 4.10 评级 | `steps/rating.py` | `domain` 纯函数 | — |
| 4.12 定去向与落库 | `steps/disposition.py`、`steps/persist.py` | `domain` 纯函数；`store` | `handoff/outputs/triage.schema.json` |

### 4.2 组装主张

`claims.build(problem, signals, links, user_notes) -> Claim` 是纯函数，按 `03-triage.md` 3.3 的模板生成：

| 字段 | 内容 |
|---|---|
| `statement` | 按探针与检查类型套用模板得到的一句话主张 |
| `title` | 短标题，写现象不写原因，例如「`POST /api/Material/Query` 在分页参数为负数时返回 500」；后续作为 Issue 标题与提交信息的描述 |
| `facts` | 附带的事实：请求与响应摘要、复现命令、堆栈中本项目的帧、出现次数与时间分布 |
| `entryPoints` | 从事实中直接得到的入口：路由模板、页面路由、堆栈中本项目的首帧 |
| `userNotes` | `problem retriage --note` 提供的信息，标明「用户提供」 |
| `priorVerdicts` | 不填。给角色的输入中不包含任何已有判定与聚合阶段的推测 |

只放观察到的事实，不放推测。组装结果写入交接文档的 `outputs.claim`，便于评测时核对输入。

### 4.3 main 差异事实

问题发现于 staging 部署的 commit(`problem.last_seen_release`)，取证在 `main` 最新 commit 上进行。`main_diff.py` 在取证前取出两者之间的差异，作为事实交给取证角色：

1. 从 `entryPoints` 与堆栈帧得到候选文件。
2. `vcs.log(range=<部署 commit>..<取证 commit>, paths=<候选文件>)` 列出期间修改过这些文件的提交。
3. 结果作为 `facts.mainDiff` 交给角色：提交编号、标题、改动的文件。没有提交时写明「期间未修改」。

取证角色据此判断根因代码在 `main` 上是否已被修改；已被修复时填写 `fixedOnMain`，由 4.12 判为「等待部署」。

### 4.4 查重

1. **候选**：`retrieval.search` 按问题的路由、页面、文件、异常类型过滤出未关闭的 Issue 与已分诊问题(最近 90 天)，最多 10 个，只带标题、根因位置与状态。没有候选时跳过本步。
2. **执行器任务**：

| 字段 | 取值 |
|---|---|
| `instructions` | `skills/triage/references/tasks/dedup.md` + 本问题的主张 + 候选列表 |
| `workdir` | 只读 worktree |
| `access` | `read-only` |
| `allowedCommands` | 只读 git 命令(`log`、`show`、`blame`、`grep`)与文本搜索 |
| `limits` | `stages.triage.tasks.dedup`：轮数、时间、费用上限 |
| 模型 | 调用点 `triage.dedup` 的路由 |
| `outputSchema` | `runner/tasks/triage-dedup.schema.json`：`sameRootCause`、`target`(问题编号或 Issue 编号)、`evidence`(`文件路径:行号` 列表)、`reason` |

3. **代码检查**：`sameRootCause` 为真时 `target` 必须在候选中，`evidence` 必须非空且位置真实存在。不通过按 4.6 的重做规则处理，仍不通过视为「不同根因」继续分诊。
4. **合并**：判为同一根因时，调用 `store.repos.problems.merge(target_problem, this_problem)`(与聚合的 `merge` 使用同一个仓储函数)：本问题的指纹写入 `problem_aliases`，本问题记 `merged` 事件。`target` 是 Issue 时取该 Issue 的主问题作为合并目标。交接文档 `status` 为 `ok`，`nextAction` 为「已并入 P-xxxx」，本问题的流程结束。

### 4.5 取证

每个问题只取证一次，全部由 `claim-verifier` 完成：

| 问题类型 | 角色 | 说明 |
|---|---|---|
| api-fuzz、内部错误、业务告警、访问日志、项目探针的缺陷类 | `claim-verifier` | — |
| api-fuzz 响应过慢 | `claim-verifier` | 输入是多次耗时记录与链路入口 |
| incidental | `claim-verifier` | 发现原文作为主张，来源报告的上下文作为事实 |
| static | 不调用角色 | 采集时 `claim-verifier` 已给出证据，从信号的 `context` 取出后直接进入 4.6 的代码检查；检查不通过，或缺少 `analysis`、`report`、`assessment` 时视为不可复用，按缺陷类重新调用 `claim-verifier` |

**执行器任务**

| 字段 | 取值 |
|---|---|
| `instructions` | 角色说明 + `evidence-standard.md` + `severity.md`(严重度标准与 Issue 报告的写法，后附项目的 `triage.severityGuide` 与标题上限 `thresholds.issue.titleMaxLength`) + 验收标准(分诊条目表的条目文字，不出现评分的说法，12.2) + 主张与事实 + `retrieval.context_for(task)` 预取的条目摘要(缺陷模式、已接受的取舍、分诊经验、同路由与同文件的历史问题与 Issue) |
| `workdir` | 只读 worktree，已切换到取证 commit |
| `access` | `read-only` |
| `allowedCommands` | 只读 git 命令与文本搜索；不允许构建、测试与启动服务 |
| `limits` | `stages.triage.roles.<角色>.limits[<复杂度>]`；复杂度由 `domain.sizing.complexity(problem, hint)` 计算(13.3) |
| 模型 | 调用点 `triage.claim-verifier`(证伪复核为 `triage.refuter`)的路由；复杂度只决定 `limits` |
| `interactive` | `false` |
| `outputSchema` | `runner/roles/claim-verifier.schema.json` |
| 第三方 skill | `claim-verifier` 与 `refuter` 加载 `fp-check`(已安装且哈希核对通过时，architecture/09 7.2) |

`claim-verifier` 输出的主要字段：最前面的 `analysis`(自由分析，不限格式，结构化字段写在它之后)、`verdict`(`Verdict`)、`facts`(每条含 `location` 与 `observation`)、`trigger`、`counterEvidence`(查过的入口与校验及结果)、`impact`(`kind` 取 `ImpactKind`、`roles`、`data`、`callSites`、`consequence`)、`sourceOfPhenomenon`(判不成立时必填)、`rootCauses`(`file`、`line`、`symbol`)、`fixedOnMain`、`tradeoffHit`、`missingInfo`、`incidental`(任务外发现，由核心整理为交接文档的 `incidentalFindings`)、`report`(判成立时给出，见下)、`assessment`(判成立或条件成立时必填，不成立与证据不足时为 null，见 4.9)。

`report`(`common.schema.json#/$defs/issueReport`，`claim-verifier`、`refuter` 共用，不成立与证据不足时为 null)：`title`(「[模块] 现象与后果」)、`summary`(问题，一两句)、`steps`(触发条件或复现步骤)、`expected`、`actual`、`acceptance`(可验证的验收条件)、`severity`(按 `severity.md` 的 P0 到 P3 与项目说明)、`severityReason`。全部给人读的文字按 `project.language` 书写：语言要求由 `runner/prompt.build_prompt` 统一加在每个执行器任务的「输出语言」一节，不写进角色说明。

### 4.6 证据检查与重做

检查之前先由 `pipeline/common/locations.py` 按代码快照补全代码位置：位置字段(`文件:行号`、`{file, line}`)与文字中引用的 `文件:行号` 只写了文件名或缺了前几级目录时，快照中恰好一个文件以它结尾即补全；补不全的位置字段由下面的位置检查判不通过，文字中的引用(扩展名在快照中出现过的)作为一条不通过的原因，一并交回重做。

分诊条目表(12.2)的五项全部由代码判定，取证之后不另设模型评审：

| 条目 | 实现 |
|---|---|
| 每条证据都带 `文件路径:行号`，且文件与行号真实存在 | `evidence_checks.locations_exist`：在取证 commit 上检查文件存在、行号不超过文件行数；`facts`、`counterEvidence`、`sourceOfPhenomenon`、`rootCauses` 中的位置都检查 |
| 输出 JSON 字段齐全，判定属于四档之一 | 执行器已按 schema 校验；此处再检查条件必填字段：判「证据不足」时 `missingInfo` 非空，判「条件成立」时 `trigger` 写明条件 |
| 结论不含含糊措辞 | `evidence_checks.vague_terms`：对 `facts`、`trigger`、`reason` 等文本字段匹配含糊措辞词表(「可能」「大概」「建议进一步排查」「应该」等)；词表写在条目表 `evaluation/rubrics/triage.json` 该条目的 `params` 中，由 `evaluation` 的代码评分器执行，生产与评测共用 |
| 判定为「不成立」时，现象来源指向真实存在的代码位置或主张中的某条事实 | `evidence_checks.refuted_source`：`sourceOfPhenomenon` 非空；`location` 真实存在，或 `factRef` 是主张 `facts` 中存在的序号 |
| 做过反证检查：至少追到一个入口，每条写明入口位置与上游有无校验 | `evidence_checks.counter_check`：`counterEvidence` 至少一条；每条的 `entry` 真实存在，或属于主张的 `entryPoints`；`upstreamValidation` 为 `present` 时带真实存在的位置 |

评估检查(`evidence_checks`)：判为成立或条件成立时 `assessment` 不能为空；`assessment.estimate.files` 中标为已有的文件在取证 commit 上存在；`flags` 中每项都有理由与位置；价值判断为暂不修时必须有 `reevaluateWhen`。

**重做**：任一项 `fail` 时，把逐项未通过的原因作为附加输入交回同一角色重新取证，最多重做 `thresholds.triage.evidenceRetries`(2)次(12.3)。重做后仍不通过，判定改为 `insufficient`，去向为 `manual-queue`，`reason` 中写明未通过的评分项。

执行器返回 `schema-invalid`、`limit-reached` 或 `failed` 时，计为一次未通过的尝试，按同样的次数上限处理。

判定本身对不对，由 4.7 的证伪复核(只在高风险时)、修复第 5 步的复现测试(design 5.6)、用户改判与 `03-triage.md` 3.10 的效果反馈校准。

### 4.7 证伪复核

触发条件(`refute.needed`)：取证判为 `confirmed` 或 `conditional`，且严重度、任务类型或影响类别命中 `triage.refute` 的任一项(缺省严重度 P0、P1，类型 `security`，影响类别 `authorization`、`data-ownership`)；或 `claims` 阶段预估严重度为 P0(越权检查失败)而取证判定为 `refuted`。其余问题不复核，由修复前的复现测试兜底。

| 字段 | 取值 |
|---|---|
| `instructions` | `roles/refuter.md` + `evidence-standard.md` + 与第一次完全相同的主张与事实；不包含第一次的判定与输出 |
| 任务表述 | 「专门寻找理由反驳这条主张，同时如实承认反驳不了的地方」 |
| 工具与模型 | 调用点 `triage.refuter` 的路由，须与 `triage.claim-verifier` 解析出的工具或模型不同(配置校验时检查) |
| 其余字段 | 与 4.5 相同 |
| `outputSchema` | `runner/roles/refuter.schema.json`(字段与 `claim-verifier` 相同) |

复核结果同样经过 4.6 的代码检查。合并规则(`refute.combine`，纯函数)：

| 第一次 | 复核 | 最终判定 | 去向 |
|---|---|---|---|
| 确认成立或条件成立 | 确认成立或条件成立 | 采用第一次的判定 | 按 4.12 |
| 确认成立或条件成立 | 不成立、证据不足或未通过证据检查 | 保留第一次的判定 | `manual-queue` |
| 不成立 | 不成立 | 不成立 | 按 4.12 |
| 不成立 | 确认成立或条件成立 | 采用复核的判定 | `manual-queue` |
| 不成立 | 证据不足 | 证据不足 | `manual-queue` |

复核的判定写入 `refuter_verdict`。

### 4.8 归因

判定为 `confirmed` 或 `conditional` 时执行，全部是只读的 `vcs` 调用：

1. `vcs.blame(file, lines, commit=<取证 commit>)`：取每处根因行的最后修改提交。
2. `vcs.log(commit)`：作者与提交标题。
3. `vcs.pr_for_commit(commit)`：用 `gh` 查询包含该提交的已合并 PR 编号；查不到时为空。
4. 多处根因指向不同提交时全部列出，第一处根因的提交作为主引入提交。

归因失败(例如文件在取证 commit 上被移动)不影响判定，`introducedBy` 留空并在 `reason` 中说明。

### 4.9 值不值得修

1. **已接受的取舍**：取证角色给出 `tradeoffHit` 时，`steps/tradeoff.py` 核对该编号在知识库中存在、类型为 `tradeoff`、状态为 `active`；核对通过即判为 `accepted-tradeoff`。
2. **取证的评估**：不另调用角色，取自 `claim-verifier`(或采用复核结论时的 `refuter`)输出的 `assessment`：

| 字段 | 内容 |
|---|---|
| `worth` | 价值判断：`fix`(该修)、`optional`(可修可不修)、`defer`(暂不修)、`wont`(不该修)，即 `WorthRecommendation` |
| `worthReason` | 价值判断的理由：不修的代价、修复的代价与修复引入回归的风险 |
| `taskType` | 任务类型(`TaskType`)：`bug`、`security`、`data`、`frontend`、`feature`、`refactor`、`dependency`、`docs-config` |
| `estimate` | 预估改动：`files`(`path`、`isNew`)与 `lines`，不含测试文件 |
| `direction` | 修复方向 |
| `flags` | 设计问题、数据结构或存量数据、公共实现或接口契约，每项附理由与位置 |
| `reevaluateWhen` | 价值判断为暂不修时的重估条件 |

评估的代码检查见 4.6，与证据检查一起重做。

### 4.10 评级

除严重度外全部由 `domain` 中的纯函数计算，不由模型直接给出：

| 结果 | 函数 | 输入 |
|---|---|---|
| 严重度 | 取证输出的 `report.severity`；没有时 `domain.triage.severity(impact_kind)` | `report.severity` 由取证角色按 `severity.md`(P0：线上不可用、数据丢失或泄露、安全漏洞；P1：核心功能错误或数据长期错误但有替代；P2：非核心功能错误或偶发；P3：轻微)与项目的 `triage.severityGuide` 给出并写明理由。没有报告(旧结论)时按 `impact.kind` 映射：权限、数据归属、数据正确性、凭证泄露为 P0；核心流程不可用为 P1；非核心功能出错、契约不一致为 P2；体验与规范、响应偏慢、低危依赖漏洞为 P3 |
| 规模档 | `domain.sizing.size_tier(files, lines, limits)` | 预估改动取 `assessment.estimate`(不含测试文件)，没有时取根因文件、行数为 0；门槛为 `thresholds.tiers`(design 13.1) |
| 复杂度 | `domain.sizing.complexity(problem, hint)` | 已在 4.5 计算，此处按取证结果重新计算一次并写入结论，供修复环节使用；分段点为 `thresholds.triage.complexityFiles` |
| 任务类型 | 取 `assessment.taskType` | — |
| 三类标记 | 取 `assessment.flags` | 设计问题、数据结构或存量数据、公共实现或接口契约 |

### 4.11 跨条对齐

不保留：同一根因的问题由 4.4 的查重并入，矛盾的结论由高风险时的证伪复核与修复前的复现测试兜底([redesign/03-triage.md](../redesign/03-triage.md))。

### 4.12 定去向与落库

1. **处理标签**：`domain.triage.treatment(facts, rules)`，按 `triage.treatment.rules` 的决策树由判定、严重度、规模档与价值判断给出 `immediate`、`scheduled`、`observe`、`wont-fix`(design 13.2)；判为不成立、证据不足与转人工的不计算。
2. **去向**：`domain.triage.disposition(facts)`，规则来自 3.6 与 13.2：

| 条件 | 去向 | 问题状态变化 | 附带动作 |
|---|---|---|---|
| 不成立(P0 须经过 4.7) | `problem false-positive` | 转为 `ignored` | 生成抑制规则：匹配条件为指纹，到期日期为当天加 `thresholds.suppressionDays` |
| 命中已接受的取舍 | `accepted-tradeoff` | 转为 `ignored` | 记录取舍编号 |
| 取证给出 `fixedOnMain` | `awaiting-deploy` | 转为 `ongoing` | 由聚合在部署后按 2.8 判定已解决 |
| 成立或条件成立，处理标签为立即修或排期修 | `create-issue` | 转为 `ongoing` | 交接文档 `nextAction` 为「交给 issue 创建」 |
| 成立或条件成立，处理标签为观察 | `deferred` | 转为 `ignored` | `ignore_until` 为「再出现 `thresholds.triage.deferredReopenOccurrences` 次或严重度升级」；上一次分诊已判为观察、恢复后再次判为观察的(`observed_before`)升为排期修，去向为 `create-issue`，理由写进分诊结论 |
| 成立或条件成立，处理标签为不修 | `accepted-tradeoff` | 转为 `ignored` | 理由写明处理标签，不自动恢复 |
| 证据不足，或 4.6、4.7 转入人工 | `manual-queue` | 保持 `new` | 发现报告中列出缺少的信息 |
| P0 且成立或条件成立 | 一律 `create-issue`(先于已接受的取舍判断) | 转为 `ongoing` | 运行摘要的等待事项按处理标签置顶 |

3. **标签**：`domain.triage.labels(touches_protected)`，写入交接文档 `outputs.labels`：`assessment.estimate.files` 与 `protectedPaths` 有交集时为「需要先与代码作者讨论」，没有其他标签。
4. **落库**：在一个事务内写入 `triage_results`(新的 `attempt`)、`problem_events`(状态变化与理由)、问题状态；抑制规则写入工作区 `suppressions.yaml`(由 `store` 提供的带文件锁的写入函数)。
5. **交接文档与发现报告**：写 `triage-<问题编号>.json`，再由 `render/findings.py` 生成发现报告。

## 5. triage：读写的数据

### 5.1 读写清单

| 读取 | 用途 |
|---|---|
| `problems`、`problem_signals`、`signals`、`problem_aliases` | 组装主张 |
| `problem_events` | `problem retriage-requested` 事件、历史改判 |
| `issues`、`triage_results` | 查重候选 |
| `knowledge_meta`、`knowledge_fts` 与知识条目文件 | 经 `retrieval` 预取上下文、核对已接受的取舍 |
| 上游交接文档 `aggregate-<问题编号>.json`(或 `--input`) | 输入 |
| 只读 worktree | 取证 |

| 写入 | 内容 |
|---|---|
| `triage_results` | 分诊结论，主键为问题编号加 `attempt` |
| `problems`、`problem_aliases`、`problem_events` | 状态变化、合并、改判；处理 `problem retriage-requested` 事件时写入 `handled_at` |
| `suppressions.yaml` | 判为误报时的抑制规则 |
| `scores` | 分诊条目表各项的结果 |
| `runs` | 本次运行记录 |
| `handoffs` 与 `data/runs/<运行编号>/handoff/triage-<问题编号>.json` | 交接文档 |
| `data/runs/<运行编号>/transcripts/<角色>-<问题编号>.jsonl` | 各角色的会话记录 |
| `data/findings/<问题编号>.md` | 发现报告 |

### 5.2 交接文档 outputs

`outputs` 由 `contracts/schemas/handoff/outputs/triage.schema.json` 约束，字段与 3.8 的结构化结论一一对应，并补充下游需要的内容：

| 字段 | 说明 |
|---|---|
| `problemId` | 问题编号 |
| `claim` | 4.2 的主张：`statement`、`title`、`facts`、`entryPoints`、`userNotes` |
| `verdict` | `Verdict` |
| `severity`、`complexity`、`taskType`、`sizeTier` | 4.10 的结果 |
| `treatment` | 4.12 的处理标签 |
| `rootCauses` | `file`、`line`、`symbol` 列表 |
| `introducedBy` | `commit`、`author`、`pr`；多处根因时为列表 |
| `disposition` | `Disposition` |
| `reason` | 判定与去向的依据 |
| `triageCommit` | 取证 commit |
| `refuterVerdict` | 4.7 的判定，未触发时为空 |
| `flags` | `design`、`dataStructure`、`publicContract`，每项含理由与位置 |
| `labels` | 4.12 的 Issue 标签 |
| `evidence` | 代码事实、触发条件、反证检查、影响面、现象来源(判不成立时) |
| `worth` | 取证评估的摘要：`recommendation`、`reason`、`direction`、`reevaluateWhen` |
| `estimate` | 预估改动：`files`(`path`、`isNew`)与 `lines`，不含测试文件 |
| `fixedOnMain`、`tradeoffHit`、`mergedInto` | 对应情况下填写 |
| `report` | 取证输出的 Issue 报告(4.5)；没有时为 null |
| `missingInfo` | 证据不足时缺少的信息 |
| `incidentalFindings` | 任务外发现：`file`、`line`、`symbol`、`text`；由 `collect` 的 incidental 探针读取 |
| `scores` | 各评分项的结果与方式 |
| `attempts` | 各角色的调用次数与每次的状态 |

`status`：正常得出结论为 `ok`；去向为 `manual-queue` 时为 `blocked`，`blockedReason` 写明缺少的信息或未通过的评分项；程序错误为 `failed`。

## 6. triage：发现报告

`render/findings.py` 只读交接文档，按 `findings-template.md` 的结构生成 `data/findings/<问题编号>.md`：

| 部分 | 内容来源 |
|---|---|
| frontmatter | `type: finding`、`id`(`triage-<问题编号>`，与交接文档编号相同)、`problemId`、`status`(与去向一致)、`summary`、`tags`、`runId`、`createdAt`、`updatedAt`、`verdict`、`severity`、`disposition`、`treatment`、`triageCommit` |
| 结论 | 第一句写判定与去向，第二句写根因位置与影响；由 `verdict`、`disposition`、`rootCauses`、`impact` 拼成 |
| 主张与事实 | `claim` |
| 证据 | `evidence.facts`，逐条 `文件路径:行号` |
| 触发条件 | `evidence.trigger` |
| 反证检查 | `evidence.counterEvidence` |
| 影响面 | `evidence.impact` |
| 根因与引入 | `rootCauses`、`introducedBy` |
| 值不值得修 | `worth`，以及处理标签、任务类型、规模档与预估改动量 |
| 需用户定夺 | `flags` 中为真的项，每项含理由与位置；没有时写「无」 |
| 查了但不成立 | 判不成立时的 `sourceOfPhenomenon` 与挡住它的代码位置 |
| 没查清的 | `missingInfo` |
| 任务外发现 | `incidentalFindings` |
| 评分 | 各评分项、方式与结果 |

时间按本机时区渲染并写明时区，日期为绝对日期。

## 7. triage：用户介入、幂等与错误处理

### 7.1 用户介入

分诊中不向用户提问，也没有 git 写操作。用户介入只发生在分诊之后：

| 场景 | 用户操作 | 模块的处理 |
|---|---|---|
| 人工队列 | `problem retriage <问题> --note <补充信息>` | 以补充信息重新分诊 |
| 人工队列或抽查 | `problem retriage <问题> --verdict <判定> --reason <原因>` | 写入新结论，`outcome` 为 `overridden`；原结论保留；按新判定重新计算去向并执行 4.12 |
| 每周误报抽查 | 在运行摘要中查看自动判为误报的问题，改判用 `problem retriage` | 改判为成立的，原结论的 `outcome` 回填为 `false-refute` |

### 7.2 效果反馈

`triage_results.outcome` 由后续事件回填，本模块只负责 `overridden` 与 `false-refute`，其余由对应模块写入：

| 结果 | 写入方 |
|---|---|
| `correct` | Issue 完成并通过部署后确认(`staging-verified`)时写入，`issue` 的同步步骤补写遗漏的；误报问题在之后覆盖运行中不再出现由 `learn` 写入 |
| `false-confirm` | Issue 以「不是缺陷」关闭时由 `issue` 写入；PR 以「不是缺陷」「无法复现」理由被关闭时由 `release` 写入 |
| `false-refute`、`overridden` | `problem retriage` |

### 7.3 幂等与重跑

- 同一问题每次分诊产生一个新的 `attempt`，交接文档按「模块 + 对象」覆盖，旧版本改名保留(15.7)。
- 未满足 3.2 的条件时不会重复分诊；结论按指纹缓存，只有回归、新类型的证据(聚合写入 `problem retriage-requested`)、用户要求三种情况重新分诊。
- 运行中途中断时，已落库的问题不受影响；未落库的问题没有 `triage_results` 新行，下次运行时仍满足前置条件，从头重新处理。每个问题的落库在一个事务内完成，不会留下半个结论。
- 合并操作按目标问题与本问题的编号幂等：已并入的再次执行不做任何改动。

### 7.4 错误处理

| 错误 | 处理 |
|---|---|
| 执行器返回 `schema-invalid`、`limit-reached`、`failed` | 计为一次未通过的尝试；达到 2 次重做上限后去向为 `manual-queue`，`reason` 写明执行器状态 |
| 证据检查不通过 | 4.6 的重做，2 次后转人工 |
| 只读 worktree 同步失败 | 整次运行停止，交接文档不写，运行摘要中说明 |
| `vcs` 只读查询失败(归因、PR 查询) | 对应字段留空，`reason` 中说明，不影响判定 |
| 预算超出 | 已处理的问题照常落库，其余问题留到下一次 |
| 单个问题出现程序异常 | 该问题的交接文档 `status` 为 `failed`，记录异常类型与堆栈摘要；其他问题继续 |
| 落库事务失败 | 回滚，该问题视为未处理 |

## 8. issue：职责与文件划分

### 8.1 职责

把去向为「提 Issue」(处理标签为立即修、排期修)的分诊结论写成本地 Issue 文件，维护 Issue 的状态与索引，并执行 Issue 与问题状态之间的同步(4.7)。纯脚本，不调用 LLM。Issue 文件是唯一的数据来源，`issues` 表只是索引。

### 8.2 代码目录

```
core/tightrein/pipeline/issue/
  service.py              入口：create、sync、list、show、edit、approve、close、reopen、rerender、reindex
  steps/
    select.py             选取去向为 create-issue 且尚无 Issue 的问题
    locate.py             按根因位置查找未关闭的 Issue(幂等键)
    slug.py               由问题的探针数据生成英文简称(纯函数)
    acceptance.py         按探针类型与指纹生成验收标准(纯函数)
    body.py               由分诊交接文档生成结论、「内容」各小节与引用(纯函数)
    frontmatter.py        生成与校验 frontmatter(纯函数 + schema 校验)
    create.py             分配编号、写文件、写索引、回写问题
    append.py             把问题追加到已有 Issue
    transitions.py        approve、close、reopen 的状态转换与历史记录
    sync.py               Issue 与问题状态的同步(4.7)、待决定超时提醒
    edit.py               调用编辑器、编辑后校验与记录历史
    github_comments.py    GitHub 镜像的关键节点评论入队(10.8)
    github.py             GitHub 镜像的对齐：读回开关状态，推送标签、子 Issue 与阻塞关系、评论与开关状态，未同步项(10.8)
  render/
    issue.py              Issue 文件的正文(交接文档 issue 类型的版式)与历史行格式
    summary.py            运行摘要中 Issue 一节的条目
```

Issue 文件本身就是人读文档，`render/issue.py` 组装交接文档并交 `domain/handoff/document` 渲染；修改已有文件时只改动头信息与「历史」「引用」两节，其余正文保持用户编辑后的内容。

### 8.3 skill 目录

```
skills/issue/
  SKILL.md
  references/
    issue-template.md
    commands.md
```

| 文件 | 内容要点 |
|---|---|
| `SKILL.md` | 何时使用(查看、审阅、放行、关闭、重开 Issue)；Issue 状态的含义与每个状态的下一步；放行前应当检查的内容(复现步骤是否可信、修复方向是否合理、三类需用户定夺的标记)；放行时会连带确认建分支与 worktree；说明创建 Issue 由分诊后自动进行，skill 不手写 Issue 文件 |
| `references/issue-template.md` | 4.3 的 frontmatter 字段与 4.4 的正文模板，逐节写明内容来源与用户可以修改的部分；验收标准的写法(按探针类型给出的可检查条件) |
| `references/commands.md` | 各命令的参数、效果、对问题状态的连带影响(4.7) |

## 9. issue：命令与前置条件

### 9.1 命令

| 命令 | 特有参数 | 作用 | 前置条件 |
|---|---|---|---|
| `issue create` | — | 为选中的问题创建或追加 Issue | 最新分诊结论的去向为 `create-issue`；问题尚无 `issue_id` |
| `new` | `--title`、`--body` 或 `--body-file`(二选一)、`--severity`(缺省 P2)、`--type`(任务类型，缺省 `fix.manualTaskType`) | 新建用户需求的 Issue(10.7) | 不与 `--select`、`--input`、`--dry-run`、`--output` 同用 |
| `issue sync` | — | 执行 4.7 的同步与待决定超时提醒；`issues.tracker` 为 `github` 时再对齐 GitHub 镜像(10.8)并列出未同步项 | 无 |
| `issue list` | `--status`(六种状态之一)、`--severity` | 列出 Issue：按处理标签(立即修在前)、严重度、创建时间排序(`domain.triage.urgency_key`，design 13.2) | 无 |
| `issue show <编号>` | — | 显示 Issue 全文与关联问题的最新状态、出现次数 | Issue 存在 |
| `issue edit <编号>` | — | 用 `$EDITOR` 打开 Issue 文件，保存后校验 | Issue 存在 |
| `approve <编号>` | — | 放行：状态改为「待修」，随后确认建分支与 worktree；用户需求已是「待修」，不改状态只申请建分支 | 状态为 `needs-decision`，或用户需求处于 `todo` |
| `issue close <编号>` | `--reason <关闭原因>`、`--note`、`--duplicate-of <编号>` | 关闭为取消 | Issue 未关闭(不是 `done`、`cancelled`)；`--reason duplicate` 时必须给 `--duplicate-of` |
| `issue reopen <编号>` | `--note` | 重新打开，状态回到「待修」 | 状态为 `done` 或 `cancelled` |
| `issue rerender [<编号>...]` | — | 按当前版式重写正文(10.3)，之后刷新镜像 | 不带编号时处理全部未关闭的 Issue |
| `issue reindex` | — | 从 Issue 文件重建 `issues` 表 | 无 |

`--reason` 取 `CloseReason` 中的 `wont-fix`、`duplicate`、`not-a-bug`；`fixed` 与 `fix-rejected` 只由 `verify` 与 `release` 根据验证与 PR 结果写入，用户不能直接指定。

### 9.2 放行与建分支的衔接

`approve` 是编排层提供的组合命令：先调用 `issue` 服务完成状态转换，再调用 `fix` 服务的 `prepare` 步骤，由它向 `vcs` 申请建分支与 worktree 的待确认操作，当场交用户确认(见 `07-fix-verify-release.md` 3.3)。`issue` 模块自身不调用任何 `vcs` 写操作。用户拒绝建分支时，Issue 仍为「待修」，之后执行 `fix prepare` 再次申请。

### 9.3 自动放行

关卡 `gates.issue-approve: auto`(缺省 `user`，09 篇 3.8)时，`issue create` 新建的 Issue(状态为待决定；追加到已有 Issue 的不判断；用户需求的 Issue 创建即为待修，照常视为已放行)在写交接文档前按 `orchestrator/policy/autonomy.py` 判断，规则针对每个关联问题的分诊交接文档：有分诊结论；标签不含「需要先与代码作者讨论」；三类需用户定夺的标记(设计、数据结构、公共契约)都未命中；影响类别不在 `autonomy.approve.impactKinds`(缺省权限、数据归属)；复杂度不高于 `autonomy.approve.maxComplexity`(缺省 `low`)；严重度不在 `autonomy.approve.severities`(缺省空)。

- 全部满足：经 `transitions.apply_event` 放行为待修(事件 `approve`，actor `autonomy`，备注「自动放行：满足 …」，GitHub 镜像的放行评论带上这句)，本次 create 结束后调用组装层注入的 `IssueDeps.prepare`(即 `fix prepare`)申请建分支；`gates.release-writes: auto` 时直接建成。
- 任一条不满足：留在待决定，「历史」与镜像评论写「需要用户决定：…」。
- 每次决定写 `gate` 事件(decision `auto-approve` 或 `needs-decision`)，交接文档写 `autonomy: {approved, reasons}`，运行摘要「自动决定」列出。
- 用户需求(`new`)创建即视为已放行：「历史」写「自动放行(gates.issue-approve 为 auto)」，镜像评论放行，随后调用 `IssueDeps.prepare` 申请建分支。修复计划拆分出的后续子任务等前一个完成后由 `run` 的无人值守推进建分支(07 篇 4.6)。
- 无人值守修复停下的 Issue 转为待决定(带 `hold`)，镜像的状态标签随之为 `<前缀>needs-decision`(10.8)。

## 10. issue：处理步骤

### 10.1 create

| 步骤 | 实现 | 说明 |
|---|---|---|
| 1. 选取 | `steps/select.py` | 读取问题的最新 `triage_results` 与交接文档 `triage-<问题编号>.json`(或 `--input`) |
| 2. 查找已有 Issue | `steps/locate.py` | 幂等键为根因位置：把 `rootCauses` 规范化为「文件 + 所在方法」的集合，与未关闭 Issue 的 `rootCause` 求交集；有交集即视为同一根因 |
| 3a. 追加 | `steps/append.py` | 把问题编号加入该 Issue 的 `problems`，「引用」一节(旧版式为「关联」)补充问题，「历史」一节追加一行；严重度更高时提升 Issue 的 `severity` 并记录；问题的 `issue_id` 回写 |
| 2b. 补全与核对 | `service.py` | 按代码快照补全分诊结论中的代码位置(`pipeline/common/locations.py`，同 4.6)；新建前渲染正文并核对「引用」中完整证据的位置，补不全或不存在时不建 Issue：交接文档 `failed`，`nextAction` 提示 `problem retriage` |
| 3b. 新建 | `steps/create.py` | 从 `sequences` 分配编号；`slug.py` 生成简称；`body.py` 与 `render/issue.py` 按 `project.language` 生成正文；`frontmatter.py` 生成头信息并按 `handoff/frontmatter/issue.schema.json` 校验；写文件 `issues/<编号>-<简称>.md`，写 `issues` 表，回写问题的 `issue_id`，写 `problem_events` |
| 4. 代码检查 | `steps/frontmatter.py` | 12.2「提 Issue 与报告」一项：必需小节齐全(按小节键)、「引用」中的完整证据每条带位置、日期为绝对日期；结果写 `scores` |
| 5. 通知 | `observability/notify.py` | 新建或追加的问题为 P0 时立即发本机通知，幂等键为「事件类型 + Issue 编号 + 日期」 |
| 6. 交接文档 | `service.py` | 写 `issue-<问题编号>.json` |

新建 Issue 的状态为 `needs-decision`(等待放行；满足自动放行时立即放行为 `todo`，9.3)。第 4 步不通过说明生成逻辑有缺陷，交接文档 `status` 为 `failed`，已写入的文件保留供排查，运行摘要中列出。

### 10.2 简称与标题

| 字段 | 生成方式 |
|---|---|
| `title` | 分诊交接文档的 `report.title`(「[模块] 现象与后果」)，超过 `thresholds.issue.titleMaxLength`(80)截断；没有时为 `claim.title` |
| 简称 | `slug.py` 按探针取英文词：api-fuzz 取路由模板的末两段加状态码或检查名(例如 `material-query-500`)；内部错误取异常类型与首个本项目帧的方法名；static 与 incidental 取缺陷模式名与方法名。统一转为小写、以 `-` 连接、截断到 40 个字符 |

简称同时用于修复分支名的描述部分(`07-fix-verify-release.md` 3.3)。

### 10.3 正文生成

Issue 正文是交接文档的 issue 类型(版式与依据见 [redesign/04-issue.md](../redesign/04-issue.md))：基础小节为结论、内容、需要决定、下一步、引用、历史，「内容」下八个小节。`body.py` 是纯函数，输入是分诊交接文档，输出结论、「内容」各小节(键见 `domain/issue_sections.py`)与引用；`render/issue.py` 组装 `HandoffDocument` 交 `domain/handoff/document` 按 `project.language` 渲染(zh、en、ja 三套标题，其他语言用英文)，固定文字取 `render/labels.py`。字段缺失时显示「—」，不丢节：

| 节(键) | 内容来源 |
|---|---|
| 结论 | `report.title` 加后果，一句话 |
| 问题(`problem`) | `report.summary`(没有时为 `claim.statement`)，附「期望：…；实际：…」 |
| 影响(`impact`) | 严重度及理由、`evidence.impact`：后果、受影响的角色、数据、调用点 |
| 复现(`reproduce`) | `report.steps` 编号列表(没有时以 `evidence.trigger` 作一条)；另附 `claim.facts` 中的复现命令、失败步骤、截图与 trace |
| 原因(`cause`) | 根因位置、`evidence.trigger`、`evidence.counterEvidence` 追到的入口、`introducedBy` |
| 范围(`scope`) | 预估改动的文件(`assessment.estimate`)；明确不做的部分(`assessment.outOfScope`) |
| 注意事项(`notes`) | 「需要先与代码作者讨论」(预估文件命中 `protectedPaths`)；必须保持不变的文件、接口与行为(`assessment.mustKeep`)；三类需用户定夺的标记 |
| 验收标准(`acceptance`) | 复选框：固定三条在前(复现测试修复前失败、修复后通过，`fix.repro.skipTypes` 中的类型写不适用；现有测试全部通过；必须保持不变的行为，逐条列 `mustKeep`，没有时写「除本 Issue 描述的问题外，现有行为不变」)，再接 `report.acceptance` 与 `acceptance.py` 生成的探针条件 |
| 修复方向(`direction`) | `assessment.direction`(建议，不强制) |
| 下一步 | 按状态生成一条(`domain/next_step`) |
| 引用 | 发现报告路径、完整证据(`evidence.facts`，逐条 `` `文件路径:行号` ``，折叠显示)、关联的问题 |
| 历史(`history`) | 第一行为创建记录：日期、运行编号、分诊结论摘要 |

读取一律按小节键(`issue_sections.find`)：`issue_sections.split` 同时识别新版式(把「内容」下的 `###` 小节摊平)与旧版式，任一语言的标题与旧标题(触发条件或复现步骤、期望与实际、完整证据、元信息、关联等)都识别为对应的键，修复上下文、评审、PR 描述、工作摘要、镜像、edit 的校验与代码检查(`keyed`)都不依赖具体标题与版式。

**验收标准**(`acceptance.build(problem, signals, links, language)`，纯函数，文字按语言)：

| 探针 | 生成的条件 |
|---|---|
| api-fuzz | 「api-fuzz 对 `<方法> <路由>` 的 `<检查名>` 检查以 `<角色>` 身份通过」 |
| e2e | 「巡检用例 `<用例编号>` 的第 `<n>` 步通过，且页面加载时不再出现 `<报错或失败请求>`」 |
| 内部错误、业务告警、访问日志、项目探针 | 「部署后的观察期与之后的覆盖运行中不再出现指纹为 `<指纹>` 的问题」 |
| static | 「静态巡检的 `<规则名>` 规则在 `<文件:类名.方法名>` 不再命中」 |
| incidental | 「对原发现重新取证，判定为不成立」，并追加修复环节建立的复现检查通过 |

探针条件接在固定三条之后。

### 10.4 状态转换

所有转换都通过 `domain.issue.transition(current, event, context)` 计算(转换表 `TRANSITIONS` 见 01 篇 2.3)，不在转换表中的组合抛出 `InvalidTransition`，命令报错退出。每次转换在「历史」一节追加一行(日期、事件、操作者、说明)，并写一条 `user_action` 或 `run_script` 事件。

| 命令或事件 | 转换 | 连带处理 |
|---|---|---|
| `approve` | `needs-decision` → `todo`(清除 `hold`) | 9.2 的建分支确认 |
| `close --reason wont-fix` | 未关闭 → `cancelled` | 关联问题转为 `ignored`，恢复条件为严重度升级或出现在新版本中 |
| `close --reason not-a-bug` | 未关闭 → `cancelled` | 关联问题判为误报并生成抑制规则(与 4.12 相同的写入函数)；对应分诊结论 `outcome` 回填 `false-confirm` |
| `close --reason duplicate --duplicate-of <n>` | 未关闭 → `cancelled` | 关联问题追加到 Issue n 的 `problems`，问题的 `issue_id` 改为 n |
| `reopen` | `done`、`cancelled` → `todo` | 「历史」记录重开原因 |

`pr-merged`(完成，关闭原因 `fixed`，`phase` 为 `deploy-check`)、`pr-closed`(取消，关闭原因 `fix-rejected`)、`staging-verified`、`staging-failed` 由 `release` 与 `verify` 通过同一个转换函数写入；部署后确认通过(`staging-verified`)时同步关联问题，并把对应分诊结论的 `outcome` 回填为 `correct`。

`approve` 用于处于 `todo` 的用户需求(10.7)时不做转换、不写事件，只接着申请建分支。

### 10.5 sync

每次定时运行执行一次：

| 检查 | 处理 |
|---|---|
| 完成或取消的 Issue 的关联问题状态为 `regressed` | Issue 重新打开为 `todo`(事件 `problem-regressed`)，「历史」记录回归时的 commit 与信号编号 |
| Issue 完成且部署后确认已通过(`phase` 为空)，分诊结论 `outcome` 为空 | 回填 `correct` |
| 没有 `hold` 的 `needs-decision`(等待放行)超过 `thresholds.issue.reviewReminderWorkdays`(3 个工作日) | 运行摘要中列出 |
| Issue 文件与 `issues` 表不一致(文件被手动修改) | 以文件为准更新索引；头信息校验不通过时不更新索引，运行摘要中列出文件与错误 |

### 10.6 edit

1. 记录文件修改前的哈希，调用 `$EDITOR`。
2. 保存后按 `handoff/frontmatter/issue.schema.json` 校验，并检查必需小节；不通过时显示逐条错误，询问重新编辑或放弃修改(放弃时恢复原文件)。
3. 通过后更新 `issues` 表，「历史」一节追加「用户编辑」一行与改动的节名。
4. 用户修改了 `status` 字段时，按 10.4 的转换表校验，不允许绕过命令直接跳转状态。

### 10.7 用户需求

`new` 由 `IssueService.create_manual` 与 `steps/create.create_manual` 实现：分配编号，简称取标题中的英文词(取不到时为 `manual-<序号>`)，头信息的 `origin` 为 `manual`、`from` 为 `user`、`problems` 为空、`taskType` 取 `--type`、状态为 `todo`；正文同为交接文档版式(`render/issue.manual_document`)：「问题」为用户原文(原文中的验收标准提到「验收标准」一节，其余标题降为加粗文字)，验收标准为原文中的验收标准(没有时写说明)加固定三条，其余小节写「—」。修复计划拆分出的后续子任务同样以这种 Issue 建立，`parent` 为父 Issue、`dependsOn` 为前一个子任务、`from` 为 `fix`。不写运行记录、交接文档与评分(「提 Issue 与报告」的代码检查针对分诊生成的正文)；P0 照常通知；`--output` 模式不支持。`issue rerender` 把旧版式的「需求」转为「问题」。

没有关联问题时各环节的处理：关闭与重开的问题同步、回填分诊结果、`sync` 的回归检查都按空列表跳过；`next` 与运行摘要对还没有修复分支的用户需求给出 `approve`；`--from triage` 报错；fix 按任务类型(`--type`，缺省 `fix.manualTaskType`)与中档查流程表，第 5 步按验收标准写测试(见 `07-fix-verify-release.md`)；learn 不为它写缺陷规则，交付时长从创建算起。

### 10.8 GitHub 镜像

按项目选择 Issue 去向：`project.yaml` 的 `issues.tracker` 为 `local`(缺省)时只在本地；为 `github` 时每个未关闭的本地 Issue 在 GitHub 上有一个镜像 Issue，给用户阅读与讨论。状态机、证据、修复计划都在本地，镜像不新增状态与事件。字段按归属划分：开关状态以 GitHub 为准，标题、正文、标签与子 Issue、阻塞关系以本地为准，每次对齐按本地覆盖。

| 配置 | 含义 |
|---|---|
| `issues.github.repo` | `owner/name`；为空时取 `project.repo` 的 origin 远程解析出的仓库 |
| `issues.github.labelPrefix` | 标签前缀，缺省 `tightrein:`；状态标签为 `<前缀><状态取值>`，类型标签为 `<前缀>type:<任务类型>` |
| `issues.github.privateOnly` | 缺省 `true`：仓库公开时整次跳过并写明原因(缺陷与安全发现不应公开) |
| `gates.mirror-writes` | 关卡表中的一项(09 篇 3.8)，缺省 `user`：镜像写入按对外写操作的规则逐次确认；`auto` 时直接执行。只覆盖镜像的 gh 写操作，不影响建分支、提交、推送、提 PR 的确认 |
| `issues.github.labelColor` | 自动创建的标签颜色 |
| `runtime.vcs.issueListLimit` | 读回开关状态、列出标签时一次查询的条数 |

**数据。** GitHub 编号与链接属于 Issue：头信息 `github: {number, url}` 与 `issues.github_number`、`github_url`(迁移 006)。镜像的簿记不属于 Issue，放在 `github_mirror`(GitHub 上已知的开关状态 `remote_state`、已打的标签 `labelled_status`(`<状态>|<类型>`)、已建立的关系 `relations`、最近一次失败)与 `github_mirror_comments`(待发评论)，`reindex` 不动它们。

**一次对齐**(`steps/github.GithubMirror.sync`，gh 一律经 `VcsProcess.gh`，读经只读重试)：

1. `gh repo view --json isPrivate,hasIssuesEnabled`：Issue 未启用，或 `privateOnly` 而仓库公开，整次跳过；
2. 读回：`gh issue list --state all --json number,state,stateReason` 一次列出，与 `remote_state` 不同说明用户在 GitHub 上改过。关闭 → 本地 `user-closed` 取消(原因 `wont-fix`，备注写 GitHub 编号与关闭原因；以 duplicate 关闭也按不修处理，因为对应不到本地 Issue)；重新打开 → `user-reopened` 为待修。本地处于 `pending-merge` 或 `done` 且 GitHub 以 completed 关闭的，是 PR 的 `Closes` 自动关闭，只记下状态。读回引起的转换不写评论。只同步开关状态，不同步正文编辑与评论；
3. 推送(先读后写)：没有镜像且本地未关闭时 `gh issue create`(带状态标签与类型标签；父 Issue 已有镜像时带 `--parent <编号>`，`dependsOn` 已有镜像时带 `--blocked-by <编号>`)；标签与 `labelled_status` 不符时 `gh issue edit` 去掉上次打的标签、加上应有的(不去掉没打过的标签)；关系缺失时以 `gh issue edit --parent`、`--add-blocked-by` 补上，记进 `relations`；逐条发出待发评论；开关状态与本地要求不符时 `gh issue close --reason completed|not planned`(完成为 completed)或 `gh issue reopen`。完成与取消要求关闭，待合并不要求，其余要求打开。缺失的标签以 `gh label create --force` 建立。`needs-decision` 是状态标签，不另加。

**正文**(`github.mirror_body`，创建时写，`issue rerender` 时更新)：本地正文去掉「历史」(旧版式另去掉标题行)；`` `路径:行号` `` 与 `` `路径:起-止` ``(文件在项目仓库中存在时)换成取证 commit 的永久链接 `https://github.com/<仓库>/blob/<triageCommit>/<路径>#L<起>-L<止>`(用户需求没有取证 commit，不链接；代码块内不处理)；「引用」中的完整证据(旧版式为「完整证据」一节)放进 `<details>` 折叠；末尾是按语言的说明，整段经 `Redactor` 脱敏；最后是本地标记 `<!-- tightrein:<项目名>:<编号> -->`。状态标签的描述与关键节点评论的固定部分同样按 `project.language`。

**重新渲染**(`issue rerender [<编号>...]`，`IssueService.rerender`)：分诊 Issue 用现有分诊交接文档按当前版式重写正文与标题，用户需求的 Issue 把原「需求」转为「问题」，都保留原历史并追加一行；旧版式的文件由此转为新版式。之后 `GithubMirror.refresh` 为已有镜像加一步 `gh issue edit --title --body-file`(遵守 `gates.mirror-writes`)，再照常对齐。不调用模型：交接文档中没有的字段显示「—」，命令输出列出缺少的字段，完整格式需先 `problem retriage`。

**关键节点评论**：放行(含用户需求的放行)、修复计划确认、PR 创建、PR 合并、关闭(带原因)。由 `transitions.apply_event` 在转换的同一事务中写进 `github_mirror_comments`(`github_comments.queue`)，修复计划确认由 fix 的 follow_up 写入。

**何时对齐**：`IssueService` 在 create、new、approve、close、reopen 之后对齐涉及的 Issue；`issue sync` 之后与 run 的 `issue-mirror` 步骤对齐全部。

**确认与幂等。** `gates.mirror-writes` 为 `user` 时，每个 Issue 每次对齐最多生成一个 `github-issue` 待确认操作(发起环节 `issue`，内含这次的全部 gh 命令；还没有镜像时只含建 Issue)，`tightrein approve` 执行后由 issue 的 FollowUp 按每步输出记下编号、标签、已发评论与开关状态；同一 Issue 已有待确认或已确认未执行的操作时不再生成，内容与被拒绝的操作相同时不再生成。为 `auto` 时直接执行，每步成功即记下结果。建 Issue 以幂等键 `github-issue:<仓库>:<编号>` 记下编号与链接后再写 Issue 文件；键处于进行中(上次在 gh 返回后被中断)时按本地标记在仓库 Issue 中找回。

**失败。** gh 失败、Issue 文件冲突等只记进 `github_mirror.error`，不影响本地流程；下次对齐重算差异重试。`issue sync` 的输出与运行摘要的「未同步到 GitHub」列出没有镜像、标签或开关状态未对齐、待发评论、等待确认的操作与最近一次失败。确认模式下一个操作中途失败时，已执行的步骤(例如已发出的评论)会在重新生成的操作中再执行一次。

## 11. issue：读写的数据

| 读取 | 用途 |
|---|---|
| `triage_results` 与 `triage-<问题编号>.json` | 创建的输入 |
| `problems`、`signals` | 验收标准 |
| Issue 文件 | 唯一数据来源 |

| 写入 | 内容 |
|---|---|
| `issues/<编号>-<简称>.md` | Issue 文件 |
| `issues` 表 | 索引 |
| `problems.issue_id`、`problem_events` | 回写与留痕 |
| `triage_results.outcome` | 效果反馈 |
| `suppressions.yaml` | 以「不是缺陷」关闭时 |
| `scores` | 12.2「提 Issue 与报告」 |
| `data/runs/<运行编号>/handoff/issue-<问题编号>.json` | 交接文档 |
| `github_mirror`、`github_mirror_comments`、frontmatter `github` | GitHub 镜像(10.8) |

**交接文档 outputs**(`handoff/outputs/issue.schema.json`)：

| 字段 | 说明 |
|---|---|
| `issueId` | Issue 编号 |
| `action` | `created` 或 `appended` |
| `path` | Issue 文件路径 |
| `problems` | 该 Issue 当前关联的全部问题 |
| `severity`、`treatment`、`labels` | 取自分诊结论 |
| `acceptance` | 生成的验收标准 |
| `notified` | 是否发出了 P0 通知 |

## 12. issue：幂等与错误处理

| 场景 | 处理 |
|---|---|
| 同一问题重复执行 `create` | 问题已有 `issue_id` 时不满足前置条件；`--output` 模式下仍按根因位置定位，结果写到指定目录 |
| 同一根因的另一个问题 | 按根因位置追加，不新建 |
| 编号分配后写文件失败 | 编号不回收；交接文档 `status` 为 `failed`；`issues` 表不写入，下次运行重新分配编号 |
| 写文件成功、写索引失败 | 下一次 `sync` 或 `reindex` 以文件为准补齐索引 |
| 通知重复 | 幂等键去重，同一天同一事件只通知一次 |
| 非法的状态转换 | 命令报错退出，说明当前状态与允许的操作 |

## 13. 测试

### 13.1 单元测试

| 对象 | 覆盖 |
|---|---|
| `triage/steps/claims.py` | 3.3 每种探针与检查类型一条用例；断言不含任何推测字段 |
| `triage/steps/evidence_checks.py` | 位置不存在、行号越界、含糊措辞、条件必填字段缺失、现象来源的 `location` 与 `factRef` 两种写法、反证检查缺少入口或上游校验位置的正反样例 |
| `triage/steps/refute.py` | 4.7 合并表的每一行 |
| `domain.triage.severity`、`treatment`、`disposition` | 3.5、3.6、13.2 的每一行；缺省决策树的每条规则 |
| `issue/steps/slug.py`、`acceptance.py`、`body.py` | 每种探针一条用例；正文章节与 `issue-template.md` 的节标题一致 |
| `issue/steps/transitions.py` | 10.4 每个合法转换与每类非法转换 |
| `render/findings.py` | 节标题与 `findings-template.md` 一致；时区与绝对日期 |

### 13.2 回放测试

`tests/replay/triage/<用例>/` 与 `tests/replay/issue/<用例>/`，每个用例包括：

| 文件 | 内容 |
|---|---|
| `input/` | 上游交接文档(`aggregate-<问题编号>.json` 或 `triage-<问题编号>.json`)与数据库初始数据 |
| `repo.commit` | 取证所用的代码快照 commit；测试时从夹具仓库检出只读 worktree |
| `recordings/<角色或任务>-<问题编号>-<序号>.json` | 录制的执行器结果，`replay` 适配器按「角色 + 对象 + 调用序号」匹配 |
| `expected/` | 期望的交接文档、数据库变化(按表列出新增与修改的行)、发现报告或 Issue 文件 |

以 `--runner replay` 运行，断言输出与 `expected/` 一致。用例至少覆盖：确认成立并提 Issue、不成立并生成抑制规则、P0 不成立触发证伪复核且两次不一致、证据检查两次重做后转人工、`fixedOnMain` 判为等待部署、查重合并、追加到已有 Issue、`not-a-bug` 关闭后的连带处理。

17b 取舍：以上九类用例已由 `tests/unit/pipeline/` 中以假执行器驱动真实服务的测试逐条覆盖(`test_triage_service.py`、`test_triage_persist.py`、`test_issue_create.py`、`test_issue_transitions.py`)，`--runner replay` 的整链路由 `tests/unit/cli/test_smoke.py` 覆盖；不另建 `tests/replay/triage/` 与 `tests/replay/issue/` 的录制夹具。等有真实运行的录制后，由评测集(`eval`)以同一格式回放。

### 13.3 沙箱运行

`tightrein triage --input <交接文档> --output <目录>` 与 `tightrein issue create --input <交接文档> --output <目录>` 把交接文档、发现报告、Issue 文件写到指定目录，不写数据库、不写 `suppressions.yaml`、不发通知。评测运行器(03 篇第 2 章)以此模式在评测集上运行两个模块，按分诊条目表与「提 Issue 与报告」的条目打分。

本篇用到的基础层定义(编号、枚举、表、路径、配置)统一见 01-foundation.md。
