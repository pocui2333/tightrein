# implement：实施

## 是什么

把放行的 Issue 落成通过自检与审查的代码，缺陷与用户需求都适用。一条流程，八个小步骤，按已有信息跳过：

| 小步骤 | 控制键 | 文件夹 | 做什么 | 什么时候做 |
|---|---|---|---|---|
| 准备 | `implement.prepare` | `prepare/` | 建修复分支与 worktree，跑准备命令与基准检查 | 都做 |
| 定位 | `implement.locate` | `locate/` | 补全代码笔记中缺的部分 | 评估留下的笔记有核心位置且仍成立时跳过 |
| 方案 | `implement.design` | `design/` | 改哪些文件、每步改什么，程序核对；有前端文件时加前端设计说明 | 都做，输入是代码笔记 |
| 定案 | `implement.approve` | `approve/` | 真风险或超出自动门槛时等用户确认，其余自动 | 都做 |
| 编码 | `implement.code` | `code/` | 照方案改代码，逻辑、接口、数据处理类改动同时写测试 | 都做 |
| 自检 | `implement.check` | `check/` | 程序跑项目检查、改动量与规则检查 | 都做 |
| 审查 | `implement.review` | `review/` | 轻量审查；高风险加深度审查 | 都做 |
| 交付 | `implement.deliver` | `deliver/` | 交给发布 | 都做 |

## 流程

`implement.py` 的 `implement(runtime, issue) -> StepOutcome` 每次推进一步：从落盘的产物推导下一步(`decide`) → 做这一步 → 写这一步的 `handoff.json` → 再推导一次决定接着做、停在关卡、停下还是退回评估。

```
准备 → 定位(可跳过) → 方案 → 定案 ─(等用户)
                        ↑       ↓
                重出方案 ←─ 编码 → 自检 → 审查 → 交付
                        └─ 局部问题交回编码(≤ 3 轮，没有进展即停)
```

- **续跑点由产物推导**：最后一个完成的交接(按写入时间)与它的结论，加上之后用户有没有新决定。中断后不靠进度标记；没做完的那一步整个丢掉，worktree 退回上一个检查点(`facts.worktreeCommit`)。
- **检查点快照**：每一步写交接时程序把 worktree 的内容记成一个不挂在分支上的 commit(`prepare/workspace.py:snapshot`，临时索引加 `commit-tree`)，不动 HEAD、分支与暂存区，提交照常由发布做。
- **不通过项的分派**：自检、审查、编码交接中的 `blockers`(格式见 `check/findings.py`)按性质只处理最靠前的一类：设计问题停下；需要用户(含检查没跑起来)停在关卡；方案缺口重出方案；局部问题交回编码，只给这一类的项。
- **轮数与没有进展**：局部问题交回编码最多 `controls.implement.review.rounds`(3)轮，按这一版方案(或用户最近一次决定)之后算；连续两轮阻断项(位置 + 类型)相同或本轮 diff 哈希与上一轮相同(`protocol.limits.no_progress`)立即停下。
- **越界**：编码一轮结束后程序检查本轮改动，越出 worktree 或碰了禁改文件即撤回本轮全部改动；第一次带原因重做这一轮，再越界停下。
- **超出范围**：方案发现一个 PR 放不下(`oversize`)，Issue 经 `SPLIT_BACK` 退回评估拆成几个新 Issue，实施阶段不拆子任务。
- **用量上限**：每次推进前看 `resources.issueTokens`(`protocol.resources.IssueBudget`)，超了停下。
- **给人看的文档**：停在关卡写 `90-issue-pending.md`，停下写 `90-issue-failure.md`(`protocol/documents.py`)；Issue 的 `step`、`round`、`gate` 随之更新供 watch 与 status 显示。

### 推导下一步(`decide`)

| 最后完成的一步 | 结论 | 下一步 |
|---|---|---|
| 没有 | — | 准备 |
| 任一步 | 通过 | 按顺序；定案之后开新一轮编码(轮次接着往下编号)，编码 → 自检 → 审查同一轮，审查通过后交付，交付通过后转发布(`DELIVER`) |
| 任一步 | 待决定 | 有了用户的新决定就重做这一步(由它取用决定)，否则继续等 |
| 编码、自检、审查 | 没通过 | 越界、检查没跑起来、按性质分派(见上) |
| 方案 | 没通过 | `oversize` 退回评估，其余停下 |
| 定案 | 没通过(用户否决) | 按原因重出方案，上限 `controls.implement.design.rounds`(1) |
| 交付 | 没通过 | 交接写了 `backTo`(自检或审查之后又有改动)回到那一步重做一次；其余停下 |
| 停下之后 | 用户给了新决定 | `approve`：编码类的接着开一轮，其余重做那一步；`reject`：重出方案 |

## 输入与输出

- 输入：`issues` 表中待修或实施中的 Issue、`00-issue-body.md`、`00-issue-notes.json`(代码笔记，评估与实施共用)、用户的决定(`issues.extra.decisions` 与放行历史)、知识库条目(`knowledge.match` 按代码笔记与方案的文件命中，最多 `knowledgeEntries` 条、`knowledgeTokens` 个 token，放进 `ImplementContext.knowledge`)。
- 上下文：`context.py` 的 `ImplementContext`，`load(runtime, issue)` 从 store 与各步交接读出；基准取 worktree HEAD 与主干的最近公共祖先，只算一次。
- 输出：每一步一份 `3x-implement.<步>[.r<轮>]-handoff.json`；`StepOutcome(subject, point, status, summary, next_point)`：`passed` 表示在推进，`pending` 表示停在关卡，`failed` 表示停下。

### 各步交接中流程要读的事实

| 事实 | 谁写 | 谁读 |
|---|---|---|
| `worktreeCommit`、`reruns`、`skipped`、`knowledgeSuggestions` | implement.py(每一步落盘前补上；`skipped` 缺省为 null，`knowledgeSuggestions` 缺省为空列表) | 续跑时退回检查点；同一步同一轮重跑的次数；status 显示跳过 |
| `blockers`：`[{check, kind, location, summary, category, trigger}]` | 编码、自检、审查 | 分派、没有进展判定、交回编码的修正说明 |
| `violations`、`redo` | 编码 | 越界重做与停下 |
| `diffHash`、`linesAdded`、`linesDeleted` | 编码(本轮通过时) | 没有进展判定；status、watch 显示改动量 |
| `commands`(`name`、`result`) | 自检 | 测试命令都通过才记检查点(code/checkpoint.py) |
| `callFailed` | 审查 | 审查没给出结果：只重跑审查一次，不重新编码 |
| `backTo` | 交付 | 自检或审查之后又有改动：回到那一步重做一次 |
| `gate`：`{decision, options, recommendation, reason, ifNot, command}` | 方案、定案(停在关卡时) | implement.py 写待审核文档；没给 `gate` 的步骤(自检、审查)自己写 |
| `auto` | 定案(每种结局都写) | status 显示是否自动确认 |
| `highRiskPaths` | 方案(通过时)、自检、交付 | status、watch；发布据交付的这一项决定合并是否人工 |
| `planHash` | 方案、定案 | 编码前核对确认的就是当前方案 |
| `files`、`steps`、`summary`、`hypothesis`、`acceptance`、`frontendDesign`、`risk` … | 方案 | 定案、编码、自检、审查 |
| `changedFiles`、`release`、`testsWritten`、`deviations` | 编码 | 审查、交付、发布 |
| `knowledgeSuggestions` | 定位、方案、编码、审查(模型给出的)；其余步骤为空列表 | 知识库(`knowledge.propose`) |
| `incidentalFindings`、`baseCommit` | 定位、编码(每条结构照 `collect/incidental/finding.schema.json`) | 采集的任务外发现(`collect.incidental`，按 `baseCommit` 记发现时的版本) |

用户的决定(`context.Decision`)有两个来源，`load` 合并后按时间排序：

- 关卡上的决定：命令行 approve、reject(或 `approve/approve.py:record`)追加到 `issues.extra.decisions`；`Decision.point` 取记下的 `step`(Issue 当时所在的小步骤)，没有时取 `point`；
- 停下之后的放行：Issue 已转为待决定，命令行按状态机走 `APPROVE`，只记进 `issues.extra.history`；实施开始之后用户的这类放行也算一个 approve 决定(补充说明取历史的 `note`)，续跑时才看得到。

## 配置

| 键 | 含义 |
|---|---|
| `controls.implement.knowledgeEntries`、`knowledgeTokens` | 注入提示的知识条目条数与 token 上限 |
| `controls.implement.prepare.links` | 从主工作区软链接进 worktree 的被忽略目录(如 `node_modules`) |
| `controls.implement.locate.*`、`implement.design.*`、`implement.design.frontend.*`、`implement.code.*` | 模型、时限、轮数等控制字段 |
| `controls.implement.design.rounds` | 方案程序核对不过的重出次数，也是方案被否决或发现缺口后重出的次数 |
| `controls.implement.design.riskRules` | 风险判定的路径与内容规则(schema、authz、contract) |
| `controls.implement.design.frontend.paths` | 前端文件的缺省路径模式(另加项目事实的 `frontendPatterns`) |
| `controls.implement.code.checkpointRollbackFindings` | 不通过项达到多少且多于检查点时退回检查点 |
| `controls.implement.review.rounds` | 局部问题交回编码的轮数上限 |
| `boundaries.changeCap`、`boundaries.autoApprove`、`boundaries.protected.*`、`boundaries.gates.design` | 改动量上限、自动确认门槛、受保护文件、方案确认关卡 |
| `project.commands`(工作区 settings.json) | 准备命令(`prepare`)与项目检查(`test`、`lint`、`typecheck`、`build`) |

## 设计依据

- **一条流程，按已有信息跳过**：评估已把 Issue 控制在「一件事、单个 PR 放得下」，不再分三条通道(44 号计划第 5 节)；放不下的退回评估拆分，不在实施里拆子任务、不留「给后续子任务的验收标准」。
- **续跑点由产物推导**：旧 `FixService.resume_point` 的做法(没有方案 → 方案；未确认 → 定案；没有通过的结果 → 编码；哈希变了 → 审查)，改为直接看各步交接；检查点与退回见 `protocol/recovery.md`。
- **分层代码笔记**：定位后程序截取原文与签名(`assess/notes.py`)，方案、编码、审查共用，不再各自通读代码(#8：单个 Issue 的勘察加方案曾耗几百万 token)。
- **重出方案复用定位结论**：#9 中一个阻断项让流程从勘察重来，又被接口中断打断；重出方案直接回到方案，不回到定位。
- **没有进展就停、轮数上限 3**：Aider 固定为 3 轮，Self-Refine 收益集中在前几轮；旧 0018 跑了 10 多轮、1450 万 token(`protocol/limits.md`)。
- **越界撤回重做一次**：`protocol/boundaries.md`「越界的处理」。
- **检查点快照不提交到分支**：提交、推送由发布按项目约定一次做；中间状态写成不挂分支的 commit，既能精确退回，又不污染分支历史。

## 不做什么

- 不拆子任务(`split.py`、`deferredAcceptance` 删去)，不写「修复前失败、修复后通过」的复现测试(`repro.py` 等删去)，不分快速、标准、大任务三条通道(`route.py` 删去)，不做交互修复会话。
- 低风险的方案与编码合在一个会话(第二批)现在不做。
- 不提交、不推送、不提 PR：由发布做。
