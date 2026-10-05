# 流水线层：fix、verify、release

本篇描述生成修复(`pipeline/fix`)、验证(`pipeline/verify`)与合并发布(`pipeline/release`)三个模块的内部结构、命令、处理步骤、用户确认、读写的数据与测试方式。业务规则见 `docs/explanation/design/05-fix.md`、`06-verify.md`、`07-release.md`，边界见 `11-boundaries.md`，评分见 `12-agent-scoring.md`(评分只作衡量)；编号、枚举、实体、表、文件布局与配置沿用 `01-foundation.md`，模块内部结构、命令形态与评分记录的约定同 `06-triage-issue.md` 第 1 章。

## 1. 共同约定

### 1.1 用户确认与「待确认操作」

三个模块中需要用户确认的事项统一表示为「待确认操作」，由 `vcs` 或模块自身生成，存入 `pending_operations` 表(列见 01 篇 4.2)：

| 字段 | 说明 |
|---|---|
| `id` | 操作编号，格式 `OP-<四位序号>` |
| `stage`、`subject_id` | 发起的模块与对象(Issue 编号) |
| `kind` | 本篇用到的 `OperationKind`：`create-fix-worktree`、`fix-plan`、`local-migration`、`commit`、`merge-main`、`commit-merge`、`abort-merge`、`push`、`pull-request`、`cleanup` |
| `executor` | 本篇的操作都为 `vcs`，即确认后由核心执行 |
| `commands` | 将要执行的完整命令，按顺序列出；`fix-plan`、`local-migration` 为空 |
| `description`、`impact`、`reversible` | 作用的分支与文件、对工作区与历史的影响、是否影响远程、能否撤销以及如何撤销 |
| `preconditions` | 生成时的状态：HEAD commit、工作区改动的哈希、涉及的文件清单、计划文件的哈希、迁移文件的哈希等，按 `kind` 取不同组合 |
| `confirmations_required`、`confirmations_given` | 需要的确认次数：删除分支与 worktree 为 2，其余为 1；已得到的次数 |
| `status` | `pending`、`confirmed`、`rejected`、`executed`、`failed`、`expired` |
| `created_at`、`decided_at`、`executed_at`、`result` | 时间与执行输出摘要、失败原因 |

**流程**

1. 模块调用 `vcs` 的写操作构造函数(或自身的确认函数)，得到待确认操作，写入 `pending_operations`，渲染说明。
2. **终端前台运行**(标准输入是终端，且不是 `--scheduled`)：当场显示说明并询问，用户输入 `yes` 视为确认，其他输入视为拒绝。
3. **其他情况**(定时运行、agent 会话中调用、用户选择稍后处理)：交接文档 `status` 为 `blocked`，`blockedReason` 写明操作编号；运行摘要列出等待确认的操作。用户执行 `tightrein confirm <操作编号>` 或 `tightrein reject <操作编号> [--note <说明>]`；各模块也提供针对本模块的简写(例如 `fix confirm <Issue 编号>`)。
4. **执行前复核**：确认后由 `vcs.execute(op)` 执行。执行前重新读取 `preconditions` 中记录的各项状态，与生成时不一致(例如确认之后工作区又被修改)的，操作标为 `expired`，不执行，模块下次运行时重新生成。确认只对这一个操作编号有效。
5. **执行后续**：执行成功后状态为 `executed`，调用发起模块登记的后续处理(回写 Issue、推进到下一步)；执行失败时标为 `failed`，记录输出，模块停下说明原因。`fix-plan`、`local-migration` 没有命令，确认后直接调用后续处理。

定时运行中不执行任何写操作；`--dry-run` 与 `--output` 模式只渲染说明，不写入 `pending_operations`、不执行。

### 1.2 转待决定

修复与验证停下、需要用户处理时，Issue 转为「待决定」，原因记在 Issue 的 `hold` 字段(`reason`、`stage`、`since`、`details`)；`hold` 只出现在待决定的 Issue 上(design 4.6)：

- 编排层遇到带 `hold` 的 Issue 不自动继续，在运行摘要中列出原因。
- 用户确认继续时执行 `fix start --force`：先显示 `hold` 的原因，确认后清除 `hold` 并进入修复，「历史」一节记录。
- 下列情况转为待决定：修复中修改轮数超过 `thresholds.fix.reviewRounds`(3)或计划重出超过 `thresholds.fix.planRounds`(2)；基准版本上项目检查不通过；超限或写不出复现测试；连续 2 次合并前验证失败；根因在设计本身；无人值守修复停下；用户选择转为与代码作者讨论。

## 2. fix：职责与文件划分

### 2.1 职责

把已放行的 Issue 变成修复 worktree 中写好、自检通过、能交给验证的未提交改动：按「任务类型 × 规模档」分流到 A 快速、B 标准、C 大任务三条通道，
按第 0 到 9 步推进(design 5.2，[redesign/05-fix.md](../redesign/05-fix.md))，停在「改动都在工作区、未提交」。遵守最小修复与单个 PR 上限。

### 2.2 代码目录

```
core/tightrein/orchestrator/policy/lanes.py   流程表：tier_of、route、needs_scout、repro_mode、expects_pass、review_modes(纯函数，只读配置)
core/tightrein/pipeline/fix/
  service.py              入口：prepare、start、plan、confirm、apply、done、abandon；续接判断；第 0 到 9 步的推进
  steps/
    route.py              第 0 步分流：查流程表定通道，写 route.json；升档只升不降，记在这里
    progress.py           进度文档：progress.json 与 progress.md
    workspace.py          修复分支名与 worktree 路径；生成建分支与 worktree 的待确认操作；worktree 准备
    context.py            读取 Issue 文件、发现报告、分诊交接文档、上一次验证报告与评审意见；预取知识
    decisions.py          用户的决定与补充(--note、--reject --note、--accept-design)的记录与渲染
    repro.py              确定性来源的复现检查：生成器、写入 regressions
    scout.py              调用 fix-scout
    risk.py               风险判定：收集候选文件或实际 diff、分诊与计划标记，调用 domain.fix.risk(纯函数)
    plan.py               调用 fix-planner；计划的代码检查；计划含前端文件时调用 frontend-designer
    plan_gate.py          渲染计划、生成计划确认的待确认操作、处理确认与拒绝
    split.py              拆分出的后续子任务(含 C 通道的子 Issue)
    repro_test.py         第 5 步：调用 fix-executor 第一轮或 repro-writer 写复现测试，在基准版本上验证，登记为测试类复现检查
    execute.py            第 6 步：调用 fix-executor(续接第 5 步的会话；按不通过项修正)
    checks.py             程序检查：项目检查命令、在 worktree 上执行的复现检查、档与单个 PR 上限、计划外文件、受保护文件、diff 规则、交付规则
    review.py             第 8 步：按通道运行 fix-reviewer(轻量、深度)；丢弃不带位置或触发条件的问题
    triage_blockers.py    按四种性质分派不通过项(纯函数)
    report.py             汇总修复结果与评分，回写 Issue
  prompts/
    common.py
    fix_scout.py
    fix_planner.py
    frontend_designer.py
    repro_test.py         fix-executor 第一轮与 repro-writer 共用
    fix_executor.py
    fix_reviewer.py
  render/
    documents.py          交接文档 task、scout、plan、result、review、decision(编号 <Issue>-<种类>[-<序号>])
    plan.py               修复计划 data/fixes/<Issue 编号>/plan.md
    report.py             修复报告 data/fixes/<Issue 编号>/report.md
    issue_history.py      Issue「历史」一节的修复摘要行
```

### 2.3 skill 目录

```
skills/fix/
  SKILL.md
  references/
    fix-rules.md
    plan-template.md
    report-template.md
    roles/
      fix-scout.md
      fix-planner.md
      fix-executor.md
      repro-writer.md
      frontend-designer.md
      fix-reviewer.md
```

| 文件 | 内容要点 |
|---|---|
| `SKILL.md` | 修复会话的使用说明：会话由 `fix start` 启动，会话中只调用 `tightrein fix plan`、`tightrein fix confirm`、`tightrein fix apply`、`tightrein fix done` 与只读命令，不直接编辑代码；说明当前通道与 `progress.md` 中的步骤；每一步之后向用户转述结果；计划确认时逐项说明改动文件、改动量、受保护文件、三类需用户定夺的标记，并请用户明确表态；遇到 `blocked` 时说明原因与可选的处理命令；会话中断后如何续接 |
| `references/fix-rules.md` | 修复的共同规则，写给全部修复角色：只修 Issue 描述的问题；最小修复(不拆文件、不抽公共实现、不顺手重构、改名或调整格式，不删除无关代码)；不过度设计(只做计划要求的改动；只在系统边界校验，内部调用之间不重复校验；不为假想需求增加抽象、参数、配置项或开关；测试只写第 5 步那一个，写代码时不删改已有测试)；当前档与单个 PR 的上限由任务给出；受保护文件清单由任务给出；禁止写死值或针对复现输入特判，禁止修改测试与复现检查迁就实现，发现测试不合理时报告而不是绕过；任务外发现只列出；完成标准指向任务附带的验收标准。不要求交付前再验证一遍，不要求用子 agent 复核 |
| `references/plan-template.md` | 修复计划的结构：结论、复现检查、根因与修复思路、逐文件的改动步骤与每步的验证方式、联动改动、预估改动量、受保护文件与三类标记、验收标准对照、用户可感知的变化、本计划不做什么、需用户定夺的点。`render/plan.py` 的输出与本文件的节标题一致 |
| `references/report-template.md` | 修复报告的结构，见第 7 章 |
| `references/roles/*.md` | 六个角色的说明，见 2.4(`frontend-designer`、`repro-writer` 为本工具新增，不来自原有文件) |

### 2.4 角色说明的迁移

四个角色说明由原先放在被测项目 `.claude/agents/auto-dev/` 下的文件迁移而来(原 `auto-dev` 中的联网风险检索不保留：高风险修复标出风险并另加深度评审)，统一处理与 `06-triage-issue.md` 2.4 相同(删去 `tools` 字段、「主会话」改为「本工具」、项目结构说明改为预取的参考条目、条款编号改为直接陈述规则、输出改为 JSON、项目专属技术细节移入工作区知识库)。此外，原文件中服务于功能开发的内容一律删去：需求拆解、按模块并行、串行区与 `declarations`、方案评分与多方案取舍。

| 角色 | 来源 | 保留的规则 | 删去或改写的内容 | 新增的内容 |
|---|---|---|---|---|
| `fix-scout` | `code-scout.md` | 同一概念试多种命名；找到候选后打开文件读实际实现；必须查清：根因附近的已有实现、可复用的公共实现、数据结构现状、联动方(调用点、前后端字段、i18n、配置、部署脚本)、现有实现的问题；找不到如实说「无」 | 「扩展、重构、全新」三选一的结论；按模块并行调用；前端、后端、Python 的目录地图(移入参考条目) | 以 Issue 的根因位置为起点勘察；`affectedEndpoints` 与 `affectedPages`(改动会影响的接口路由与页面，供验证选择回归范围)；`designIssue`(现有实现的问题达到「局部修补无法根治」时单独给出根源与理由)；复现线索写进勘察结果，供第 5 步写复现测试 |
| `fix-planner` | `solution-architect.md` | 只能在现有架构里设计，不引入新中间件、新数据库、新基础设施、新框架、新依赖；新增依赖、删除文件、重构交用户；方案落到文件级，每步给出验证方式；新建文件要说明理由；列出联动改动；列出「本计划不做什么」；需要用户拍板的点逐条列出并附推荐与理由；受架构限制做不到时如实写出，不硬凑；不为假想需求增加抽象、参数或配置项 | 「先自己设计、再联网查成熟案例、再合并」的两个来源流程与联网检索(修复计划不联网)；多方案评分与 7:3 比分规则(修复只取根治问题的最小改动)；前端 UI 风格与指定作者页面清单；数据库变更、分层职责、i18n 的项目细则(移入参考条目，由预取提供) | 最小修复原则；改动量预估(文件数与行数，不含测试)，超出单个 PR 上限时拆分为有先后顺序的子任务；新增模块或新目录写进 `userDecisions`；受保护文件的逐项说明；三类需用户定夺的标记；逐条对照 Issue 的验收标准；`userVisibleChange`(用户能感知的变化，无则写「无」)；数据结构变更时写明将新增的迁移条目、能否撤销与撤销方式 |
| `fix-executor` | `plan-executor.md`，以及 `finding-fixer.md`(修正模式) | 照计划实施，不重新设计；计划没写到的细节按项目现有惯例补齐；小问题自己处理并记录偏离，大问题(根因在设计、必须改公共实现或接口契约或数据结构、影响其他模块)停下上报；实施过程中可以运行项目检查命令取得反馈；自述只写执行的命令与实际输出，不写「已验证」「工作正常」一类断言，没跑的写「未运行」；前后端字段名以计划为硬契约，与现有代码不一致时报告矛盾；临时测试文件跑完删除；不执行任何 git 写操作；只做计划要求的改动；不伪造检查通过；不缩减需求，不以空实现或写死的返回值充当交付；不接触凭证。修正模式另保留：只改评审指出的位置；不得用抑制注释、类型强转、改测试、删报错代码、空 `catch` 让检查变绿；修不了如实说明；不属于机械问题的(伪造检查、缩减需求、权限缺失、设计问题)标为超出范围不修 | 串行区与 `declarations`；并行 worktree 与合并；「i18n 排序由收口统一执行」；「每步完成后执行该步的验证」与交付前的逐项自查(最终检查由核心的确定性检查执行)；项目规范的条款摘录(改为「遵守仓库根目录的项目规则文件」，由 agent 在 worktree 中直接读取)；原文中带符号的正反示例 | 修复规则(`fix-rules.md`)全文，含不过度设计的约束；看不到其他 Issue 的复现检查，也不得去找；同一会话两轮：第一轮只写复现测试(输出 `runner/roles/repro-test.schema.json`)，第二轮写代码；输出先写 `analysis`，再写 `changedFiles`、`verification`、`deviations`、`bigIssue`、`incidental`、`outOfScope` |
| `fix-reviewer` | `delivery-auditor.md` 中需要判断的部分，以及 `auto-dev-exec.js` 中阻断项的四类划分 | 拿不到生成者的自我报告与推理过程；只相信自己读到的代码；没亲自确认的记为「未验证」；已接受的取舍不报为问题；不修改代码 | 读取 `verification-scope.md`、`defect-patterns.md`、`accepted-tradeoffs.md` 的固定路径(改为预取的缺陷模式与已接受的取舍条目)；自己执行构建与测试(由核心的确定性检查完成)；残留与临时文件的检查(并入确定性检查的交付规则)；「默认怀疑」与宽泛的检查清单；项目技术栈的适用性说明(移入参考条目)；界面实测 | 一次评审同时看 diff、计划、验收标准与第 7 步的结果文档；只报告影响正确性(根因没修掉、破坏已有调用方或其他入口、只针对复现测试的输入写死的特殊处理、新增错误路径)与不满足需求(验收标准未达成、缩减需求)的问题，不报风格、命名与没有具体触发条件的假想风险；每个问题必须带 `文件路径:行号` 与触发条件；深度评审另按任务给出的风险类别逐项检查(`05-fix.md` 5.8)；截图评审(由 `verify` 调用)只看页面截图与页面说明，判断遮挡、错位、溢出；逐项给出评审项的结果，可以给「无法判断」；每个不通过项标明性质(局部问题、计划没覆盖、规范要求用户确认、设计问题)；不因篇幅加分 |

`repro-writer` 的说明在 `roles/repro-writer.md`：安全、数据类在与写代码不同的会话中独立写复现测试，规则与 `fix-executor` 第一轮相同。`finding-fixer.md` 中的审核修复循环(轮数上限 `thresholds.fix.reviewRounds`)与阻断项分类由核心的 `steps/triage_blockers.py` 实现(4.11)；`closeout.md` 中提交前自查的部分由 4.9 的交付规则执行，`release` 的提交前置条件读取其结果(第 19 章)。

## 3. fix：命令、前置条件与会话

### 3.1 命令

| 命令 | 特有参数 | 作用 |
|---|---|---|
| `fix prepare <编号>` | — | 生成建分支与 worktree 的待确认操作；由 `issue approve` 组合调用，也可单独执行 |
| `fix start <编号>` | `--here`、`--force` | 进入修复会话(3.4)；`--here` 不启动新会话，在当前 agent 会话中按 `fix` skill 继续；`--force` 确认继续带 `hold` 的待决定 Issue |
| `fix plan <编号>` | `--note <说明>`、`--accept-design` | 第 0 到 4 步：分流、建立确定性来源的复现检查、按需勘察、出计划，生成计划确认或自动确认；`--note` 是用户的要求或补充(例如复现线索)；`--accept-design` 表示用户已同意按设计层面的根因修复，勘察或计划标出设计问题时不再中止；两者都写入 `decisions.json`，之后持续交给各修复角色(4.2) |
| `fix confirm <编号>` | `--reject`、`--note <说明>` | 确认或拒绝当前计划；拒绝时带 `--note` 即按说明重出计划 |
| `fix apply <编号>` | `--review-only` | 第 5 到 8 步：写复现测试、写代码、收集结果、评审与修改循环，生成结果文档与修复报告；`--review-only` 跳过写代码，只对当前改动做检查与评审 |
| `fix done <编号>` | — | 第 9 步：核对产出，Issue 进入合并前验证(进行中，`phase` 为 `verify`) |
| `fix abandon <编号>` | `--reason <原因>` | 停止本次修复：Issue 转为待决定(设置 `hold`)，改动保留在 worktree 中 |
| `fix cleanup <编号>` | — | 收尾清理(19.10)，由 `release` 的清理步骤实现 |

### 3.2 前置条件

| 命令 | 条件 | 不满足时的提示 |
|---|---|---|
| `prepare` | Issue 状态为 `todo`；项目要求个人前缀(3.3)时本机用户配置中有 `branchPrefix` | 「先执行 `issue approve 7`」或「先在 `~/.config/tightrein/config.yaml` 设置 `branchPrefix`」 |
| `start` | Issue 状态为 `todo`、`in-progress` 或 `pending-merge`，或带 `hold` 的 `needs-decision`(须加 `--force`)；修复 worktree 已创建 | 没有 `hold` 的待决定「先执行 `issue approve 7`」；带 `hold` 时显示原因并提示加 `--force`；worktree 不存在时「先执行 `fix prepare 7`」 |
| `plan` | Issue 为进行中且 `phase` 为 `fix`；worktree 存在 | 「先执行 `fix start 7`」 |
| `confirm` | 有 `pending` 的 `fix-plan` 操作 | 「没有待确认的计划，先执行 `fix plan 7`」 |
| `apply` | 计划已确认，且确认时的计划哈希与当前计划文件一致 | 「计划已变化，请重新确认」 |
| `done` | 最近一次 `apply` 结论为通过；工作区改动的哈希与 `apply` 结束时记录的一致 | 「`apply` 之后工作区有新改动，先执行 `fix apply 7 --review-only`」 |

### 3.3 prepare：建分支与 worktree

1. **分支名**：按项目约定(19.4 的 `pipeline/common/conventions.py`)生成，通用格式为 `<类型>/<Issue 编号>-<简称>`：类型由任务类型按 `git.branchTypes` 映射(立即修的 P0 为 `hotfix`)，简称取自 Issue 文件名(`06-triage-issue.md` 10.2)。`git.personalPrefix` 为真或项目的分支模板含个人前缀时，在前面加本机用户配置的 `branchPrefix/`，前缀不得出现在 `git.forbiddenPrefixes`(AI 与工具名称)中；分支名已存在且不属于本 Issue 时加 `-2` 后缀。
2. **幂等**：幂等键为 Issue 编号。分支与 worktree 已存在时直接复用，不生成操作。
3. **待确认操作**：由 `vcs.plan_create_fix_worktree` 构造，`kind` 为 `create-fix-worktree`，命令依次为 `git fetch origin`、`git worktree add -b <分支> <worktree 路径> origin/main`；说明中写明新分支基于 `origin/main` 的哪个 commit、只影响本地、撤销方式为 `git worktree remove` 与 `git branch -d`。
4. **执行后续**：记录基准 commit(`baseCommit`)；执行 worktree 准备：按 `git.worktreeLinks` 从项目主工作区建立符号链接，按 `checks.prepare` 执行准备命令(例如安装前端依赖)，输出存入 `data/fixes/<Issue 编号>/prepare.log`；Issue 回写 `branch`，「历史」追加一行。准备命令失败时 worktree 保留，Issue 转为待决定，`hold` 写明失败的命令。之后在基准版本上以全量模式运行一次 `checks.commands` 自检(第 1 步)；不通过说明是配置或环境问题，Issue 转为待决定(`hold` 为「项目检查在基准版本上不通过(配置错误)」)，不进入修复。

worktree 路径为 `workspaces/<项目>/worktrees/fix-<Issue 编号>/`。worktree 中只有仓库跟踪的文件与上述链接，不复制任何未跟踪的本地配置。

### 3.4 修复会话

修复需要用户确认计划，以交互方式运行：

1. `fix start` 检查前置条件与 `hold`，把 Issue 改为进行中、`phase` 为 `fix`(从合并前验证、提交或待合并重新进入时同样如此，「历史」记录原因)。
2. 调用 `runner.run_interactive(task, first_input=...)`，在修复 worktree 中启动 `stages.fix.session` 指定工具的交互会话。任务字段：`instructions` 为 `skills/fix/SKILL.md` 加首条输入(Issue 文件路径、发现报告路径、上一次验证报告路径与评审意见路径)；`access` 为 `read-only`；`allowedCommands` 只含 `tightrein fix *`、`tightrein next` 与只读 git 命令；`interactive` 为 `true`。
3. 带 `--here` 时不执行第 2 步：第 1 步完成后返回，由用户当前所在的 agent 会话加载 `fix` skill 继续。
4. 会话中由 skill 依次调用 `fix plan`、`fix confirm`(计划需要用户确认时，用户在会话中表态后由 skill 执行)、`fix apply`、`fix done`。每个子命令都是独立的核心调用，各角色在子命令内部经执行器以无人值守方式运行，结果写入 store；会话本身不产出任何结果。
5. **续接**：Issue 处于修复阶段(进行中，`phase` 为 `fix`)时再次 `fix start`，由 `service.resume_point()` 读取 `data/fixes/<Issue 编号>/` 下已有的文件判断停在哪一步(`ResumePoint`：没有计划或通道已升级而计划仍是旧通道的为 PLAN，其后依次为 CONFIRM、APPLY、DONE)；执行器支持续接时续接原会话，否则新开会话并把已有的计划与当前 diff 作为首条输入。
6. **终端方式**：不进入 agent 会话时，用户执行 `tightrein continue <编号> --until verify`，编排层按同样的顺序调用这些子命令，确认在终端中进行。
7. **无人值守**(关卡 `gates.fix-session: auto`，缺省 `user`)：编排层(`run` 的 `unattended` 步骤与 `continue`)不启动交互会话，`Orchestrator._fix_unattended` 按续接点依次调用 `fix prepare`(还没有 worktree 时)、`fix start --here`、`fix plan`、`fix auto-confirm`、`fix apply`、`fix done`，都是非交互任务；某一步之后续接点没有前进即停下。停在待确认操作上(计划需要用户确认、`gates.release-writes` 为 `user` 时的建分支)照常作为关口；其余失败(守卫违规、检查不通过且修改轮数用尽、写不出复现测试、超限等)由 `FixService.stop_unattended` 把 Issue 转为待决定(`hold` 为「无人值守修复停下」，已因修复中的原因转待决定的不改)并在 GitHub 镜像评论写明原因，镜像的状态标签随之为 `needs-decision`。`run` 推进的 Issue：待修与进行中、排在前面的子任务已完成；还没有修复分支的用户需求与后续子任务先 `fix prepare`(`gates.issue-approve` 为 `auto` 时视为已放行)，之后按状态表续跑到 `release`。

会话本身不能修改代码(`access` 为只读)；核心另外在 `fix done` 时核对工作区改动的哈希与 `apply` 结束时记录的一致，确保进入验证的改动都经过了程序检查与评审。

## 4. fix：处理步骤

### 4.1 总览

修复按第 0 到 9 步推进(design 5.2)。各通道经过的步骤：A 为 0、1、5 到 9(第 3、4 步由核心生成计划并自动确认)；B 为 0 到 9，第 2 步按需；
C 只做第 3、4 步，确认后拆成子 Issue，各子 Issue 从第 0 步开始。每一步更新进度文档 `progress.md`(`steps/progress.py`)，产出交接文档
(`render/documents.py`)。

| 步 | 小节 | 所在命令 | 实现 | 调用 | 输出 schema 或产出 |
|---|---|---|---|---|---|
| 0 分流 | 4.2、4.3 | `plan` | `steps/route.py`、`steps/repro.py`、`steps/context.py` | `orchestrator/policy/lanes.py` 纯函数；`store`、`retrieval` | `route.json`、`progress.md`、`task.md`；超限时 `decision.md` |
| 1 准备 | 3.3 | `prepare` 的后续 | `steps/workspace.py` | `vcs`；项目检查命令(全量) | `prepare.log` |
| 2 勘察 | 4.4 | `plan` | `steps/scout.py` | 执行器：`fix-scout` | `runner/roles/fix-scout.schema.json`；`scout.md` |
| 3 出计划 | 4.5、4.6 | `plan` | `steps/risk.py`、`steps/plan.py`、`steps/split.py` | `domain` 纯函数；执行器：`fix-planner`、`frontend-designer` | `handoff/outputs/fix-plan.schema.json`；`plan.md` |
| 4 确认计划 | 4.7 | `plan`、`confirm` | `steps/plan_gate.py` | 待确认操作 `fix-plan`；`orchestrator/policy/autonomy.py` | `confirmation.json` |
| 5 写复现测试 | 4.8 | `apply` | `steps/repro_test.py` | 执行器：`fix-executor` 第一轮或 `repro-writer` | `runner/roles/repro-test.schema.json`；`repro.json` |
| 6 写代码 | 4.8、4.9 | `apply` | `steps/execute.py`、`steps/checks.py` | 执行器：`fix-executor` 第二轮；`guards` | `runner/roles/fix-executor.schema.json` |
| 7 收集结果 | 4.9 | `apply` | `steps/checks.py` | 项目检查命令(全量)；复现检查 | `result.md` |
| 8 评审 | 4.10、4.11 | `apply` | `steps/review.py`、`steps/triage_blockers.py` | 执行器：`fix-reviewer` | `handoff/outputs/fix-review.schema.json`；`review-<轮>-<模式>.md` |
| 9 完成 | 4.12、4.13 | `apply`、`done` | `steps/report.py`、`service.py` | `store` | `handoff/outputs/fix.schema.json`；`report.md` |

### 4.2 分流与上下文

**分流**(第 0 步，`steps/route.py`)：任务类型、规模档与处理标签依次取 Issue 头信息(`taskType`、`sizeTier`、`treatment`)与第一个关联问题的分诊结论；
用户需求的 Issue 没有类型时取 `fix.manualTaskType`(缺省 `feature`)，分诊结论没有类型(旧结论)时按缺陷处理。`lanes.route(config, task_type, tier)`
查 `fix.lanes.<类型>.<档>`，类型没有单独一行时取 `default`，档未知按中档查；超限时不进入修复：写 `decision.md`(建议如何拆分需求)，
Issue 转为待决定(`hold` 为「超出单个任务的上限」)。结果写 `route.json`(通道、类型、档、处理标签、升级记录)；之后的升档(A 转 B、计划重评为更大的档)
只升不降，同样记在这里。A 通道的 Issue 没有预估改动文件时转 B。

**上下文**(`steps/context.py`)：

- **Issue 文件**是唯一来源(用户可能在审阅时改过正文)，读取「结论」「复现」「证据」「根因」「修复方向」「验收标准」各节与 frontmatter。用户需求的 Issue(`origin: manual`，06 篇 10.7)读取除「历史」外的全部正文，并注明正文即需求。
- 发现报告与分诊交接文档：根因位置、复杂度、三类标记、影响类别、预估改动(`estimate`)与修复方向。
- 上一次的 `verify-local-<Issue 编号>.json`(验证失败退回时)与上一轮评审意见(计划重出时)。
- `retrieval.context_for(task)` 预取：与根因文件相关的修复经验、字段契约、项目参考条目、同一文件的历史修复报告。
- 用户的决定与补充：`data/fixes/<Issue 编号>/decisions.json`(`steps/decisions.py`)记录 `fix plan --note`、`fix confirm --reject --note` 的原文与 `--accept-design` 的决定(来源、时间)；每次出计划与实施前读取，作为「用户的决定与补充」一节交给 `fix-scout`、`frontend-designer`、`fix-planner`、`fix-executor`、`repro-writer`。`--accept-design` 一经记录持续有效(之后的重出计划同样不因设计问题中止)，提示中写明不要把该设计问题作为中止理由。实施中发现的计划缺口不是用户的话，只作为本次重出计划的评审意见。
- 复杂度取分诊结论中的值，只决定各角色的上限与预算(13.3)；通道由流程表决定，评审深度由通道与 4.5 的风险判定决定。

### 4.3 复现检查

确定性来源的复现检查在第 0 步由核心生成，供部署后确认与回归；问题是否真实存在由第 5 步的复现测试确认(4.8)。复现检查写在工作区
`regressions/<Issue 编号>/`：清单 `check.yaml` 与它引用的检查文件，由核心写入，任何 agent 都不能写这个目录。已存在时直接复用，计划重出时不重新生成。

| 问题的探针 | 生成方式 |
|---|---|
| api-fuzz | `repro.from_api_fuzz`：取信号中的请求(方法、路由、参数、请求体、角色)，期望按检查类型生成：5xx 类为「状态码小于 500」，越权类为「状态码为 401、403 或 404」，契约类为「响应符合接口描述中的 schema」 |
| e2e | `repro.from_e2e`：引用巡检用例与失败的步骤，期望为该步骤通过且无 `console_error`、`failed_request` |
| static | `repro.from_static`：缺陷模式或规则名与位置，期望为「在该方法中不再命中」 |
| 其他(内部错误、业务告警、访问日志、项目探针、incidental、agent 审查给出的静态问题)与用户需求 | 不生成；只有第 5 步写的复现测试 |

`check.yaml` 的格式见 04 篇 7.1：`issue`、`problems`，以及每条检查的 `id`、`kind`(`api`、`page`、`static`、`test`)、`role`、`file`、`location`、`targets`、`command`(测试类)、`requires`(`local-run` 扩展给出的服务名的子集，生成时由核心以 `api` 与 `page` 两种模式的启动计划校验；示例项目中的取值为 `backend`、`frontend`、`compute`)、`precondition`(可选：前置条件检查，例如「列表接口返回非空」)；请求、步骤与期望写在 `file` 指向的检查文件中。写入后每条检查在 `regressions` 表登记一行，`hash` 为清单条目与所引用文件的哈希，用于检测篡改：之后每次运行复现检查前核对，不一致时直接报错。

测试文件在复现目录与修复分支之间的流转：第 5 步的复现测试登记副本为 `<检查编号><后缀>`，只在复现检查目录中(计入哈希)；
`manifest.place_tests` 把它写到 worktree 的 `file`；`fix apply` 每轮检查前再放一次，内容被改动或删除时恢复并记局部问题；测试文件随修复由
`release` 提交、随 PR 合入，部署后确认时在部署 commit 上重跑(14 章)。

### 4.4 勘察

第 2 步，只在 B 通道且 `lanes.needs_scout` 为真时运行：Issue 缺根因位置或范围，或类型在 `fix.scout.taskTypes`(缺省 `security`、`data`)中。

| 字段 | 取值 |
|---|---|
| `instructions` | `roles/fix-scout.md` + `fix-rules.md` + Issue 各节 + 根因位置 + 预取条目摘要 + 用户的决定与补充 |
| `workdir` | 修复 worktree(此时尚无改动) |
| `access` | `read-only` |
| `allowedCommands` | 只读 git 命令与文本搜索 |
| `limits` | `stages.fix.roles.fix-scout.limits.<复杂度>` |
| 模型档 | `stages.fix.roles.fix-scout.capability`，默认轻量档 |
| `outputSchema` | `runner/roles/fix-scout.schema.json`：`analysis`、`existing`、`reusable`、`dataStructure`、`linkage`(每项带位置)、`problems`、`designIssue`、`affectedEndpoints`、`affectedPages`、`incidental` |

代码检查：每处位置在 worktree 中真实存在。结果渲染为 `scout.md`，Issue「历史」写引用。`designIssue` 非空时不再出计划，按 4.11 的「设计问题」处理(用户已 `--accept-design` 时照常继续)。

### 4.5 风险判定

**风险判定**(`steps/risk.py`)：调用纯函数 `domain.fix.risk(files, added_removed_lines, flags, impact_kind, endpoint_files, rules) -> FixRisk`，返回 `level`(`FixRiskLevel`：`normal`、`high`)与命中的 `categories`(`RiskCategory`：`schema`、`authz`、`contract`)及每条命中的依据(文件、行、规则)。规则取自配置 `review.riskRules.<类别>.paths`、`review.riskRules.<类别>.patterns` 与 `review.deepTriggers`(判定条件见 `05-fix.md` 5.8)，函数本身不含任何路径或模式。

| 时机 | 输入 | 用途 |
|---|---|---|
| 第 3 步，出计划前(A 在生成计划时) | 候选文件(根因文件与 `fix-scout` 的 `linkage` 文件)；分诊的 `flags` 与 `impact.kind`；`authz-endpoints` 输出中端点处理方法所在的文件 | 在计划中标出风险，交给 `fix-planner`；结果写入 `plan.json` 的 `risk` |
| 第 7 步通过之后 | 相对 `baseCommit` 的实际改动文件与新增、删除的行；计划的 `flags` 与 `migration`；同上的分诊与端点信息 | B 通道为高风险时在轻量评审之外另加深度评审(4.10) |

两次判定的结果都写入交接文档的 `risk`，并在修复报告中列出命中的依据。

### 4.6 修复计划

第 3 步。B、C 通道由 `fix-planner` 出计划；A 通道由核心按 Issue 生成计划：文件取分诊预估(`estimate.files`)，步骤与验收标准对照取修复方向与验收标准，
三类标记为空，随后自动确认(4.7)。

| 字段 | 取值 |
|---|---|
| `instructions` | `roles/fix-planner.md` + `fix-rules.md` + `plan-template.md` + Issue 各节 + 勘察结论(如有) + 风险判定结果 + 受保护文件清单(`protectedPaths`) + 单个 PR 上限 + 上一次验证报告或评审意见(含实施中发现的计划缺口) + 用户的决定与补充(4.2)；C 通道另要求出整体方案并拆分 |
| `workdir`、`access` | 修复 worktree，`read-only` |
| 模型档 | `stages.fix.roles.fix-planner.capability`，默认强档 |
| `outputSchema` | `handoff/outputs/fix-plan.schema.json` |

`fix-plan` 的主要字段：`analysis`、`risk`(4.5 的第一次判定)、`lane` 与 `tier`(由核心写入)、`summary`、`hypothesis`(根因假说：`cause` 一条因果链，`evidence` 位置与读到的代码事实，`edits` 修改前代码的位置与在那里改什么；A 通道的程序计划取 Issue 的根因小节与根因位置)、`steps`(文件、改动内容、验证方式)、`files`(计划改动的全部文件，新建文件标明理由)、`estimate`(文件数、行数，都不含测试文件)、`split`(拆分出的后续子任务，见下)、`protectedTouches`(每个受保护文件的改动内容与理由)、`flags`(设计问题、数据结构或存量数据、公共实现或接口契约)、`migration`(将新增的迁移条目、能否撤销、撤销方式)、`newDependencies`、`deletions`、`acceptanceMapping`(Issue 每条验收标准由哪一步满足)、`userVisibleChange`、`affectedEndpoints`、`affectedPages`、`notDoing`、`userDecisions`。

**代码检查**(`plan.check`)：

| 检查 | 不通过时 |
|---|---|
| `files` 中已有文件真实存在 | 重出 |
| `files` 与 `protectedPaths` 的交集都出现在 `protectedTouches` 中 | 重出 |
| `estimate` 与 `split.followUps` 中每个子任务的预估不超过单个 PR 上限 `thresholds.change`(缺省 5 个文件、200 行，不含测试文件) | 重出，要求拆分 |
| Issue 的每条验收标准都在 `acceptanceMapping` 中 | 重出 |
| `hypothesis` 的证据与修改位置在 worktree 中真实存在；每处修改位置的文件在 `files` 中；`files` 中要修改的已有非测试文件至少有一处修改位置 | 重出 |
| `flags.design` 为真 | 不重出，按 4.11 的「设计问题」处理 |

勘察与出计划共用 `thresholds.fix.planRounds`(2)次重做，仍不通过时 Issue 转为待决定(设置 `hold`)。

**重评规模档**：计划通过检查后，核心按计划预估(有拆分时取本计划与全部后续子任务的合计)用 `lanes.tier_of` 重评规模档，只升不降：
升为大档转 C，超限按 4.2 转待决定；通道变化记入 `route.json` 与 Issue「历史」。

**拆分**：预估超出单个 PR 上限，或通道为 C 时，计划本身(`steps`、`files`、`estimate`、`acceptanceMapping`)只描述第一个子任务，且第一个须满足本 Issue 的全部验收标准(根因修复在前)；其余按顺序写进 `split.followUps`(`title`、`goal`、`files`、`estimate`、`acceptance`)，`split.reason` 写拆分原因，各子任务单独合入也成立(不留半成品状态)。计划确认后(用户或自动确认，`FixService.on_executed`)，`steps/split.py` 为每个后续子任务建一个 Issue：与用户需求的 Issue 相同(不关联问题、创建即待修)，头信息带父 Issue 的任务类型与按预估定的规模档，从第 0 步分流；`parent` 为父 Issue，`dependsOn` 为前一个子任务的 Issue(第一个后续 Issue 依赖当前 Issue)，正文「问题」写父 Issue、序号与前一个 Issue，「验收标准」为子任务的 `acceptance` 加固定三条；当前 Issue 的「历史」与 GitHub 镜像评论写明拆分。同一 Issue 只建一次(幂等键 `split:<Issue 编号>`)。后续 Issue 在前一个完成(`done`，PR 已合并)之前不能开始：`fix prepare`、`fix start` 与无人值守推进停下并写明(`split.waiting_on`)，运行摘要「等待用户」不列；GitHub 镜像照常由 `issue-mirror` 步骤建立，父 Issue 与前一个子任务分别为子 Issue 关系与阻塞关系(06 篇 10.8)。新增模块或新目录由 fix-planner 写进 `userDecisions` 交用户决定。

**前端设计**：计划通过代码检查后，`files` 中有匹配 `stages.fix.roles.frontend-designer.paths`(01 篇 5.2，写法同 `protectedPaths`)的前端文件时，运行一次 `frontend-designer`(不要求全部是前端文件；没有前端文件时不运行)。

| 字段 | 取值 |
|---|---|
| `instructions` | `roles/frontend-designer.md` + Issue 各节 + 已通过检查的计划 + 计划中的前端文件 + 预取条目 |
| `workdir`、`access` | 修复 worktree，`read-only` |
| 模型档与工具 | `roleCapabilities.frontend-designer`(默认强档)；工具与模型按角色设置 `stages.fix.roles.frontend-designer.{tool, model, capability}`，例如交给 agy |
| `outputSchema` | `runner/roles/frontend-designer.schema.json`：`pages`(页面与组件结构)、`layout`(布局与信息层级)、`interactions`、`states`(加载、空、出错)、`styling`(取值与来源，优先既有设计变量与组件)、`mobile`、`copy`(文案与多语言键名)、`planConflicts`(设计上与计划冲突之处) |

设计说明写进计划的 `frontendDesign`(随 `plan.json` 由用户确认，`plan.md` 的「前端设计」一节)，并作为交接文档的 `frontendDesign`(`files`、`design`、`error`)；`fix-executor` 的任务附「按设计实现，偏离时写进 `deviations` 并说明理由」。执行器没有返回结果时不重试、不阻断：计划照常给出，确认说明与交接文档写明原因，`fix-executor` 只按计划实施。重出计划时随新计划重新判定与运行。

### 4.7 计划确认

第 4 步。A 通道的计划由核心生成后直接记录确认(`confirmation.json`，说明「A 通道：Issue 即计划，自动确认」)，不生成待确认操作；B 通道满足自主决定规则时自动确认，
否则交用户；C 通道一律交用户。问题是否真实存在由第 5 步的复现测试确认，验证模块不另做修复前复现。

1. `render/plan.py` 把计划渲染为 `data/fixes/<Issue 编号>/plan.md`，计划的 JSON 存为 `plan.json`。
2. 生成 `kind` 为 `fix-plan` 的待确认操作，`preconditions` 记录 `plan.json` 的哈希。说明中单独列出：通道与规模档、拆分出的后续子任务(如有)、受保护文件、新增依赖、删除文件、数据结构变更与迁移条目、三类需用户定夺的标记。
3. 用户的选择：

| 选择 | 命令 | 处理 |
|---|---|---|
| 按计划执行 | `fix confirm 7` | 记录确认(操作编号、时间、计划哈希)，写 `user_action` 事件 |
| 调整后重出 | `fix confirm 7 --reject --note <要求>` | 带着要求回到 4.6 |
| 转为与代码作者讨论 | `fix abandon 7 --reason <原因>` | Issue 转为待决定并设置 `hold` |

4. **自动确认**(关卡 `gates.plan-confirm: auto`，缺省 `user`；只对 B 通道)：`FixService.auto_confirm` 在出计划后按 `orchestrator/policy/autonomy.py` 的规则判断：没有待定问题(`userDecisions`)、三类标记都未命中、没有受保护文件的改动、迁移、新增依赖与删除文件、`estimate` 不超过 `thresholds.autonomy.planMaxFiles`(3)与 `planMaxLines`(100，两者加载配置时校验不大于 `thresholds.change`)、计划不改动 `credentialFiles` 或 `review.riskRules.authz` 命中的文件(必须交用户的关卡 `permissions-secrets`)、前端设计说明(如有)的 `planConflicts` 为空。满足时经 `run_unattended` 确认(等同 `fix confirm`，Issue「历史」写「自动确认修复计划：满足 …」、`gate` 事件、GitHub 镜像评论「修复计划已确认」)；不满足时停在本关口，「历史」与镜像评论写「修复计划需要用户确认：…」(同一份计划只写一次)。C 通道的计划不自动确认。

### 4.8 写复现测试与写代码

**第 5 步 写复现测试**(`steps/repro_test.py`)：`lanes.repro_mode` 决定方式：类型在 `fix.repro.skipTypes`(缺省 `docs-config`)中时跳过；
在 `fix.repro.independentTypes`(缺省 `security`、`data`)中时由 `repro-writer` 在另一会话中写；其余由 `fix-executor` 第一轮写，会话编号留给第 6 步续接。

| 字段 | 取值 |
|---|---|
| `instructions` | 角色说明 + `fix-rules.md` + Issue(A)或计划与勘察给出的复现线索(B) + 测试类的允许命令前缀与 `testPaths` |
| `workdir`、`access` | 修复 worktree，`workspace-write`；任务只允许改动测试文件(`RunnerTask.tests_only`，第二层边界检查拒绝其他改动) |
| 模型档 | `roleCapabilities.fix-executor`、`roleCapabilities.repro-writer`，默认强档 |
| `outputSchema` | `runner/roles/repro-test.schema.json`：`analysis`、`status`、`file`、`command`、`location`、`covers`、`reason` |

程序检查：改动只有 `testPaths` 中的文件且包含输出的 `file`；`command` 以 shlex 拆分后开头须为某条 `checks.commands` 的允许前缀(完整命令、去掉末尾含 `/` 的路径参数后的部分、`affected.command` 中 `{tests}` 之前的部分)，其余参数中有一个是该文件或以 `<文件>::` 开头(`pipeline/checks/project_checks.repro_test_cwd`)。然后在基准版本(只加了这个测试)上运行，退出码按 `regressions.testFailureExitCodes` 判定，无法执行的不算；失败时登记了 `expectedSignature` 的须在输出中出现该签名，没有登记的按 `regressions.testFailurePatterns.testBroken` 排除测试本身的问题，重试后仍是环境问题的停下报告：类型在 `fix.repro.passOnBaseTypes`(缺省 `refactor`，表征测试)中的须通过，其余须失败。

| 结果 | 处理 |
|---|---|
| 合格 | 登记为本 Issue 的测试类复现检查(与已有的确定性复现检查合在一份清单)，结果写 `repro.json`(修复前复现只在这里做一次) |
| 缺陷类(`bug`、`security`、`data`、`frontend`、`dependency`)在基准版本上就通过 | `not-reproduced`：问题不成立，Issue 转为 `needs-decision` 并退回分诊(`retriage-requested`) |
| 其他不合格 | 带原因交回重写，重写续接同一会话，最多 `thresholds.fix.planRounds` 次 |
| 写不出(`cannot-write`)或重写用尽 | 转待决定：Issue 转为 `needs-decision` 并设置 `hold`(「写不出合格的复现测试」)，补充复现线索后 `fix start --force` 继续 |

**第 6 步 写代码**(`steps/execute.py`)：

| 字段 | 取值 |
|---|---|
| `instructions` | `roles/fix-executor.md` + `fix-rules.md` + 已确认的计划 + 第 5 步的复现测试 + 计划带 `frontendDesign` 时「按前端设计说明实现」的要求(4.6) + Issue 的验收标准；修正模式下另加不通过项 |
| `workdir` | 修复 worktree |
| `access` | `workspace-write` |
| `allowedCommands` | 按 `checks` 配置的项目检查命令、只读 git 命令、文本搜索 |
| `limits` | `stages.fix.roles.fix-executor.limits.<复杂度>`，预算按 4.2 |
| 模型档 | `stages.fix.roles.fix-executor.capability`，默认强档 |
| 会话 | 续接第 5 步(或上一轮)的同一会话：`Runner.run(..., resume_session=<会话编号>)`，第一次调用就按会话续接；复现测试由 `repro-writer` 独立写、或工具没有返回会话编号时新开会话并附上第 5 步的测试；回放模式忽略 |
| `interactive` | `false`：在 `fix apply` 内以无人值守方式运行；`fix apply` 由用户、修复会话或(`gates.fix-session` 为 `auto` 时)编排层的无人值守修复触发 |
| `outputSchema` | `runner/roles/fix-executor.schema.json`：`analysis`、`status`(完成、遇大问题中止)、`changedFiles`、`verification`(命令与实际输出)、`deviations`、`bigIssue`、`incidental`、`outOfScope` |

**guards**

| 时机 | 检查 |
|---|---|
| `before(task)` | 记录 worktree 的 git 状态(HEAD、分支、远程配置、已跟踪文件的哈希)；确认进程环境中没有凭证 |
| `after(task, result)` | 改动只在 worktree 内；没有新提交、新分支，远程配置未变；没有改动计划确认时未列出的受保护文件；没有改动测试、复现检查与验证脚本；会话记录中没有读取 `regressions/`、`evals/` 与凭证文件的操作 |

`after` 发现违规时本次实施判为不通过，违规项作为「规范要求用户确认」性质处理(4.11)，不自动修正。

`fix-executor` 返回「遇大问题中止」时不进入 4.9，`bigIssue` 按其描述归入「计划没覆盖」或「设计问题」。按计划改完验证仍不通过、且原因不在根因假说之内时，`fix-executor` 不在修改位置之外试改，同样以大问题中止，由「计划没覆盖」重出计划。

### 4.9 程序检查与收集结果

第 6 步写代码之后与第 7 步由 `steps/checks.py` 判定，每项给出不通过的性质：

| 检查 | 实现 | 不通过时的性质 |
|---|---|---|
| 当前档 | 实际改动(不含测试文件)用 `lanes.tier_of` 求档，超出当前档时：A 通道转 B(保留复现测试，下一步 `fix plan`)；B 通道超档但在单个 PR 上限内时记录升档后继续 | — |
| 单个 PR 上限 | `git diff --numstat <baseCommit>` 统计文件数与行数(含未跟踪的新文件，不含 `testPaths` 匹配的测试文件)；超过 `thresholds.change`(10 个文件或 500 行) | 局部问题：交回 `fix-executor` 按计划收敛(撤回计划外与非必要的改动)，同一次 `fix apply` 中再次超出时 Issue 回到 `todo` 并设置 `hold`(「改动量仍超出上限」)交用户 |
| 项目检查 | 按 `checks.commands` 以全量模式逐条执行(第 7 步)；每条命令带 `when`(改动文件匹配的路径模式时才执行)与 `mustNotModify`(执行后工作区不得出现新改动，用于 i18n 排序一类命令)；输出存入 `rounds/<轮次>/checks/`；命令无法启动记为「未运行」 | 局部问题 |
| 计划外文件 | 改动文件不在 `plan.files` 中 | 计划没覆盖 |
| 受保护文件 | 改动了计划确认时未列出的受保护文件 | 规范要求用户确认 |
| diff 规则(12.4) | 删改了已有测试文件或复现检查(路径模式 `testPaths`)；新增跳过测试、关闭告警、抑制类型检查的标记(`skipMarkers`)；由 `guards.check_diff` 执行(02 篇 3.9)；新增行中出现复现检查输入中的特征字面量(交给评审核对是否为特判) | 前两项为局部问题，要求撤销对应改动；第三项作为评审输入 |
| 复现测试与在 worktree 上执行的复现检查 | 本 Issue 的复现测试(修复后须通过)与 `static`、`test` 类检查直接在 worktree 上执行；相关的其他 Issue(最近通过、针对的代码文件与改动文件有交集)同样执行，必须保持通过 | 局部问题 |
| 复现测试被改动 | 本 Issue 的复现测试按登记副本放回；被改动或删除时恢复并记不通过。内容一致的复现测试不参与测试改动、计划外文件、临时文件、改动量与疑似写死的检查，评分的补丁也去掉它；评审看到完整补丁，说明中注明它由本工具加入 | 局部问题 |
| 交付规则 | 新增行匹配 `checks.residuePatterns`(调试输出、注释掉的废弃代码等)；worktree 中未跟踪且不在计划中的文件(临时文件)；规则由项目规则文件中的交付要求转写而来 | 局部问题 |

第 7 步的结果写 `result.md`：每项检查的结论与输出摘要、改动的文件与行数、实际规模档；不通过时回第 6 步。`api` 与 `page` 类复现检查需要启动服务，
不在修复中执行，由 `verify local` 执行(第 13 章)。检查中的路径模式、命令与上限都来自配置，`checks.py` 不含任何项目或技术栈的取值。

### 4.10 评审

第 8 步。第 7 步全部通过、4.5 的第二次风险判定完成后，按 `lanes.review_modes(config, lane, actual_tier, checks_passed, high_risk)` 运行 `fix-reviewer`：
A 通道轻量评审，实际改动为微档且检查都通过、`fix.review.skipMicro` 为真时跳过；B 通道轻量评审，高风险另加深度评审；C 通道不直接评审(各子 Issue 走 A 或 B)。
每次评审渲染为 `review-<轮>-<模式>.md`。

| 字段 | 轻量评审 | 深度评审 |
|---|---|---|
| `instructions` | `roles/fix-reviewer.md` + 评审项(修复条目表带编号与判定方式，12.2) + Issue 的结论、根因位置与验收标准 + 已确认的计划 + `git diff <baseCommit>` + 第 7 步的结果文档与 `suspected-hardcode` 命中 | 盲审：`roles/fix-reviewer.md` + 评审项 + Issue 的验收标准 + 最终 diff + 第 7 步的实际结果与特判检查；不给 Issue 正文、计划与写代码模型的说明 |
| 不包含 | `fix-executor` 的自述与会话记录 | 同左 |
| `workdir`、`access`、`allowedCommands` | 修复 worktree，`read-only`，只读 git 命令与文本搜索 | 同左 |
| 工具与模型 | `stages.fix.review.light`，默认标准档 | `stages.fix.review.deep`，默认强档，须与 `fix-executor` 所用的工具或模型不同(配置校验时检查) |
| `limits` | `stages.fix.review.light.limits` | `stages.fix.review.deep.limits` |
| `outputSchema` | `handoff/outputs/fix-review.schema.json`：`analysis`、`mode`(`ReviewMode`：`light`、`deep`)、`items`(评分项、结果、理由)、`blockers`(类别、位置、触发条件、问题、性质、根源判断)、`unverified` | 同左 |

评审专门检查是否只针对测试数据写了特殊处理(`hardcode`)。

**问题的过滤**：`blockers` 的类别只能是 `ReviewFindingKind` 中的一种(`root-cause-unfixed`、`caller-broken`、`hardcode`、`new-error-path`、`requirement-unmet`、`requirement-reduced`；深度评审另有 `authz`、`data-structure`、`contract`)。缺少 `文件路径:行号`、位置在 worktree 中不存在、或缺少触发条件的问题由 `review.py` 丢弃，记入交接文档的 `discardedFindings`，不计为不通过。

没有不通过项、也没有「无法判断」时评审通过。给出「无法判断」时，`apply` 以 `blocked` 结束，交用户判断：用户用 `fix confirm 7 --note <判断>` 视为通过并记录判断，或用 `fix confirm 7 --reject --note <要求>` 重出计划。

### 4.11 不通过的处理

`triage_blockers.classify(blockers)` 把程序检查、guards 与评审的不通过项按性质分组，按下表处理。同时存在多种性质时，按设计问题、规范要求用户确认、计划没覆盖、局部问题的顺序只处理最靠前的一种，其余一并写入报告。

| 性质 | 例子 | 处理 | 上限 |
|---|---|---|---|
| 局部问题 | 漏改一处调用点、构建失败、残留调试代码、改动了测试 | 回到第 6 步：`fix-executor` 以修正模式处理逐项原因(续接同一会话)，然后重新执行第 7、8 步 | 修改轮数最多 `thresholds.fix.reviewRounds`(3)轮；仍不通过时 Issue 转为待决定(设置 `hold`) |
| 计划没覆盖 | 验收标准没有达成、需要修改计划外的文件 | 回到第 3 步重出计划(A 通道先转 B)，重新确认 | 计划重出最多 `thresholds.fix.planRounds`(2)次 |
| 规范要求用户确认 | 新增依赖、删除文件、改动未确认的受保护文件、guards 违规 | 停下，`apply` 以 `blocked` 结束，说明各项；用户用 `fix plan 7 --note <决定>` 把决定纳入新计划，或 `fix abandon` | — |
| 设计问题 | 根因在设计本身，局部修补无法根治 | 停下；Issue 转为待决定(设置 `hold`)，「历史」记录根源、局部修补为何不彻底；由用户决定找代码作者讨论，还是扩大修复范围后重新开始 | — |

每一轮的检查结果、风险判定、评审结果与分组写入 `data/fixes/<Issue 编号>/rounds/<轮次>/`。

### 4.12 修复报告

评审通过(或 A 通道微档跳过评审)后，`report.py` 汇总 `fix-executor` 与评审给出的「未运行」「未验证」条目，生成交接文档 `fix-<Issue 编号>.json` 与修复报告 `report.md`，记录此刻工作区改动的哈希(`diffHash`)，Issue 的「历史」追加修复摘要：通道与规模档、改了哪些文件、为什么这样改、风险判定与评审方式、检查结果、遗留事项。

### 4.13 完成

第 9 步。`fix done` 核对 3.2 中的条件，进度文档标记完成，Issue 进入合并前验证(进行中，`phase` 为 `verify`)，写 `run_script` 事件；复现测试已在 worktree 中，随修复由 `release` 提交；任务外发现已写入交接文档的 `outputs.incidentalFindings`，由 `collect` 的 incidental 探针读取。

## 5. fix：验收标准与评审项的落地

修复条目表(12.2)各项的落地位置：

| 条目 | 方式 | 落地 |
|---|---|---|
| 复现检查修复前失败、修复后通过 | 代码 | 修复前：第 5 步在基准版本上验证复现测试(4.8，`repro.json`)；修复后：复现测试与静态类在 4.9，`api` 与 `page` 类在 `verify local` 第 3 步 |
| 项目检查(构建、测试、i18n)全部通过 | 代码 | 4.9(全量)，`verify local` 第 1 步再执行一次 |
| 与本次改动相关的其他 Issue 的复现检查保持通过，不向修复 agent 展示 | 代码 | `verify local` 第 4 步；guards 检查会话记录中没有读取 `regressions/` |
| 改动量在当前档与单个 PR 上限内，改动文件都在计划中，没有触及未确认的受保护文件 | 代码 | 4.9 |
| 没有删改已有测试与复现检查，没有新增跳过标记，没有调试残留与临时文件 | 代码 | 4.9 的 diff 规则与交付规则 |
| 没有针对复现输入写死的分支 | 代码初筛加评审 | 4.9 的 diff 规则给出 `suspected-hardcode` 命中，4.10 判定 |
| 改动满足 Issue 的验收标准，根因被修掉，没有破坏已有调用方 | 评审 | 4.10 |
| 高风险修复：命中类别的检查项全部满足 | 深度评审 | 4.10 深度评审；没有深度评审时记为 `not-applicable` |

- 各项结果写入 `scores`，`stage` 为 `fix`；由 `verify` 给出的项同样以 `stage` 为 `fix`、对象为 Issue 编号写入，便于按修复统计。
- 条目由 `rubric.render` 写进提示：`fix-scout`、`fix-planner`、`fix-executor` 与写复现测试的角色在修复规则之后看到「验收标准」(只有条目文字)，`fix-reviewer` 看到带编号与判定方式的「评审项」；不要求 `fix-executor` 交付前另做自查。
- 12.3 的「重做若干次后交用户」对应 4.6 的计划重出上限(`thresholds.fix.planRounds`)、4.11 的修改轮数上限(`thresholds.fix.reviewRounds`)与第 13 章的连续验证失败上限，都转为待决定并记下 `hold`。

## 6. fix：读写的数据

| 读取 | 用途 |
|---|---|
| Issue 文件、`issues` 表 | 输入与状态 |
| `triage_results`、`triage-<问题编号>.json`、`data/findings/<问题编号>.md` | 根因、复杂度、标记 |
| `verify-local-<Issue 编号>.json` | 验证失败退回时的输入 |
| `pending_operations` | 建分支与计划的确认记录 |
| 知识条目(经 `retrieval`) | 上下文 |

| 写入 | 内容 |
|---|---|
| 修复 worktree | 复现测试(第 5 步，`fix-executor` 或 `repro-writer`)与代码改动(第 6 步，`fix-executor`) |
| `regressions/<Issue 编号>/check.yaml`、`regressions` 表 | 确定性来源的复现检查与第 5 步登记的测试类复现检查 |
| `data/fixes/<Issue 编号>/` | `route.json`、`progress.json`、`repro.json`、`prepare.log`、`plan.json`、`confirmation.json`、`decisions.json`、`rounds/<轮次>/`、`report.md`；交接文档 `progress.md`、`task.md`、`scout.md`、`plan.md`、`result.md`、`review-<轮>-<模式>.md`、`decision.md` |
| Issue 文件与 `issues` 表 | 状态、`phase`、`branch`、`hold`、「历史」 |
| `pending_operations` | 建分支与计划确认 |
| `scores` | 修复条目表中由本模块判定的项 |
| `data/runs/<运行编号>/handoff/fix-<Issue 编号>.json` | 交接文档 |
| `data/runs/<运行编号>/transcripts/<角色>-<Issue 编号>.jsonl` | 各角色与会话的记录 |

**交接文档 outputs**(`handoff/outputs/fix.schema.json`)：

| 字段 | 说明 |
|---|---|
| `lane`、`tier` | 通道与实际规模档 |
| `issueId`、`branch`、`worktree`、`baseCommit` | 修复工作区 |
| `plan` | 计划文件路径、哈希、确认的操作编号与时间 |
| `reproCheck` | 复现检查编号、类型、哈希；复现测试、静态类与测试类的修复后结果 |
| `changedFiles` | 每个文件的新增与删除行数 |
| `diffHash` | `apply` 结束时工作区改动的哈希 |
| `checks` | 每条项目检查的名称、命令、退出码、日志路径 |
| `risk` | 两次风险判定的结果：等级、命中的类别与依据 |
| `rounds` | 每轮的程序检查、评审方式与结果、不通过项的性质、被丢弃的问题 |
| `summary` | 修复手段、改了什么、解决了什么(取自计划与 `fix-executor` 的输出，供 PR 描述第 2 段) |
| `userVisibleChange` | 用户能感知的变化，无则为「无」(供 PR 描述第 3 段) |
| `affectedEndpoints`、`affectedPages` | 勘察与计划给出的并集，供验证选择回归范围 |
| `migration` | 数据结构变更与迁移条目，供验证的迁移检查 |
| `deviations`、`unverified`、`leftovers` | 偏离计划之处、未验证项、遗留事项 |
| `incidentalFindings` | 任务外发现：`file`、`line`、`symbol`、`text`，由 `fix-scout` 与 `fix-executor` 输出的 `incidental` 整理而来 |
| `protectedTouches`、`split` | 用户确认过的受保护文件改动与拆分出的后续子任务 |

`status`：评审通过为 `ok`；等待用户(计划确认、无法判断、规范要求确认)为 `blocked`；转待决定或程序错误为 `failed`，`blockedReason` 写明原因。

## 7. fix：人读文档

进度、任务、勘察、结果、评审与决定请求按交接文档格式渲染(`render/documents.py`，[redesign/00-handoff-documents.md](../redesign/00-handoff-documents.md))，编号为 `<Issue>-<种类>[-<序号>]`；`report.md` 由 JSON 交接文档渲染，验证与发布读取 JSON 交接文档。

**修复计划** `data/fixes/<Issue 编号>/plan.md`：按 `plan-template.md` 渲染，第一节「结论」写一句话修复思路、改动量、是否需要用户特别关注的事项。

**修复报告** `data/fixes/<Issue 编号>/report.md`：

| 部分 | 内容来源 |
|---|---|
| frontmatter | `type: fix-report`、`id`(`fix-<Issue 编号>`，与交接文档编号相同)、`issueId`、`status`、`summary`、`tags`、`runId`、`branch`、`baseCommit`、`createdAt`、`updatedAt` |
| 结论 | 是否通过、改动量、需要用户注意的事项 |
| 改动内容 | `summary` |
| 涉及文件 | `changedFiles`，按文件名排序 |
| 复现检查 | 类型、修复前的结果(复现测试取 `repro.json`)、修复后的结果或「由 PR 阶段的本机检查或部署后确认执行」 |
| 通道与风险判定 | `lane`、`tier`；`risk`：等级、命中的类别与依据、评审方式 |
| 检查与评审结果 | `checks`、`rounds` |
| 偏离计划之处 | `deviations`，无则写「无」 |
| 未验证项 | `unverified`，逐条写原因 |
| 遗留事项 | `leftovers` |
| 任务外发现 | `incidentalFindings`，节标题固定为「任务外发现」 |

## 8. fix：幂等与错误处理

| 场景 | 处理 |
|---|---|
| 重复 `prepare` | 幂等键为 Issue 编号，已有分支与 worktree 时复用 |
| 重复 `plan` | 覆盖 `plan.json` 与 `plan.md`，旧版本改名保留；未执行的旧 `fix-plan` 操作标为 `expired` |
| 重复 `apply` | 已有合格的复现测试时跳过第 5 步，在当前 worktree 状态上从第 6 步开始；已通过评审且改动哈希未变时直接返回上次结果 |
| 中断 | 每一步的产出都已写入 `data/fixes/<Issue 编号>/`，`resume_point()` 据此从中断的步骤继续 |
| 执行器 `schema-invalid`、`limit-reached`、`failed` | 计为一次未通过的尝试，计入 4.6、4.8 与 4.11 的上限 |
| guards 违规 | 按「规范要求用户确认」停下，不自动修正，违规详情写入报告 |
| 项目检查命令本身无法运行(例如依赖未安装) | 记为「未运行」，`apply` 以 `blocked` 结束，提示检查 `checks.prepare` |
| 预算超出 | 停止当前步骤，`status` 为 `blocked`，说明已花费与上限 |

## 9. verify：职责与文件划分

### 9.1 职责

只做独立于写代码模型的检查(redesign/06-verify.md)。修复前复现、项目检查与修复后复现在修复内部完成(修复第 5、7 步)，验证不再重复：

| 阶段 | 检查 | 运行条件 | 位置 |
|---|---|---|---|
| PR 阶段 | 项目 CI 的必需检查 | 主分支有必需检查时(没有 CI 时跳过) | 发布跟踪读取，自动合并的条件之一(第 12 章) |
| PR 阶段 | 其他问题的回归 | 总是 | `verify local`：采用修复第 7 步的结果，需要服务的检查在本机服务上补跑(第 13 章) |
| PR 阶段 | 截图评审 | 改动涉及前端且配置了页面检查(`page-routes` 或工作区的 e2e 用例)，并配置了 `local-run` | `verify local` |
| PR 阶段 | 本机启动服务跑接口测试 | 配置了 `local-run` 且改动涉及接口 | `verify local` |
| 部署后 | 按问题来源确认问题不再出现 | 总是 | `verify staging`(第 14 章) |

不对 PR 的 diff 单独跑静态巡检。不调用 LLM 判断结果；只有页面截图交 `fix-reviewer` 的截图评审或用户查看。

### 9.2 代码目录

```
core/tightrein/pipeline/checks/                 修复、验证与发布共用
  project_checks.py       项目检查：checks 配置的执行函数、测试类复现检查的命令白名单(repro_test_cwd)
  ci.py                   项目 CI 必需检查的状态判断(gh pr checks --required 的 bucket)
  regressions/            复现检查的执行器：api、page、static、test 四类(04 篇第 7 节)；WORKTREE_KINDS、SERVICE_KINDS
core/tightrein/pipeline/verify/
  service.py              入口：local、screenshots、staging
  steps/
    local_run.py          本机启动：经 local-run 扩展取得启动计划，启动、判断就绪与收尾(第 11 章)
    ports.py              端口检查与本工具遗留进程的识别
    migration.py          迁移检查与确认：迁移文件的路径模式取自启动计划
    regression.py         接口浅跑、页面巡检
    scope.py              由 fix 交接文档、diff 与 authz-endpoints、page-routes 的输出计算受影响的接口与页面、相关的其他复现检查
    screenshots.py        截图的收集与查看(调用 fix-reviewer 截图评审或交用户)
    verdict.py            各项结果的三档判定与 PR 阶段的整体结论(纯函数)
    confirm.py            部署后确认的判定：观察期、哪些问题按观察期确认、整体结论(纯函数)
    staging.py            部署后确认的前置判断：包含合并提交的部署、等待是否超期
  prompts/
    screenshot_review.py  组装 fix-reviewer 截图评审任务
  render/
    report.py             验证报告
    issue_history.py      Issue「历史」一节的验证摘要
```

`regression.py` 复用 `sources.api_fuzz` 与页面运行器 `pipeline/checks/pages` 的执行部分，以 `target` 参数指向本机服务。

### 9.3 skill 目录

```
skills/verify/
  SKILL.md
  references/
    local-run.md
    report-template.md
```

| 文件 | 内容要点 |
|---|---|
| `SKILL.md` | 两个阶段的用途与命令；如何读验证报告与三档结论；端口被占、迁移待确认、截图待查看、部署后确认等待或回归时分别怎样处理；「弱证据」不能当作「验证通过」 |
| `references/local-run.md` | 由原先放在被测项目的 `.claude/skills/auto-dev/reference/browser-verify.md` 迁移其中与项目无关的做法：先检查端口再启动、迁移检查以 diff 判断而不连库、连接配置由程序自己读取、成功与失败两种就绪信号、路由从路由清单读取而不猜测、优先点击导航、同名按钮要限定容器、每个页面脚本挂 `console` 与 `pageerror` 与请求监听、截图后必须查看、跳变类问题按固定间隔采样、需要创建真实数据时停下询问、三档结论与「弱证据写成通过等同伪造检查通过」；新增两种启动模式与端口规则、`local-run` 扩展的作用、遗留进程的处理与收尾。验证一律用本工具启动的服务，保证每次条件一致。与具体项目有关的启动命令、环境与端口由项目扩展给出(10 篇 6.7)，前端经验写入工作区的 `knowledge/reference/` |
| `references/report-template.md` | 验证报告的结构，见 15.2 |

## 10. verify：命令与前置条件

| 命令 | 特有参数 | 作用 | 前置条件 |
|---|---|---|---|
| `verify local <编号>` | `--confirm-migration` | PR 阶段的本机检查 | Issue 处于合并前验证(进行中，`phase` 为 `verify`)；`fix-<编号>.json` 为 `ok`；worktree 改动哈希与其中的 `diffHash` 一致 |
| `verify staging <编号>` | — | 部署后确认；由编排层在部署跟踪发现包含合并提交的部署成功(或没有配置部署来源而观察期已过)后调用，也可手动执行 | Issue 完成且等待部署后确认(`phase` 为 `deploy-check`)；`deployments` 中有包含合并提交的成功部署(没有配置部署来源时由 `release track` 在观察期后记入) |
| `verify screenshots <编号>` | `--ok` 或 `--issue <说明>` | 用户对待查看的截图给出结论 | 最近一次 `verify local` 以「截图待查看」`blocked` |

没有单独的修复前复现命令：修复第 5 步已在基准版本上验证复现测试(4.8)。

## 11. verify：本机启动

### 11.1 接口

`local_run.py` 提供上下文管理器 `LocalService(worktree, mode, report_dir)`，所有需要本机服务的验证都通过它启动，进入时启动并等待就绪，退出时收尾。同一时间只允许一个本机服务：进入前获取对象锁 `local-run`，退出时释放。

启动什么、怎样启动由 `local-run` 扩展给出(10 篇 3.8)：进入时以 worktree、模式与端口调用扩展，得到启动计划(`services`、`unavailable`、`migrationPaths`)；核心只按计划执行，不含任何项目或技术栈的启动知识。没有 `local-run` 的实现时 `LocalService` 不启动任何服务，需要本机服务的 `api`、`page` 类检查一律记为「未验证」，原因「未提供 local-run 扩展」(10 篇 4.1)。

### 11.2 模式与端口

| 模式 | 用于 | 传给扩展的端口 | 启动的服务 |
|---|---|---|---|
| `api` | 只需要后端的验证：`api` 类复现检查、接口浅跑 | `backend` 取 `localRun.ports.api` | 扩展给出的后端服务 |
| `page` | 需要页面的验证：`page` 类复现检查、页面巡检 | `backend` 取 `localRun.ports.backendForPages`，`frontend` 取 `localRun.ports.frontend` | 扩展给出的后端与前端服务 |

按第 13 章第 2 步决定模式：改动涉及前端且配置了页面检查时用 `page` 模式，并让 `api` 类检查也指向 `page` 模式的后端；`page` 模式所需端口被占用时退回 `api` 模式，页面类检查全部记为「未验证」，原因写明被占用的端口与占用进程，并通知用户空出端口后重跑。

示例项目中的取值：`api` 模式后端监听 5100，不占用开发常用的 5000 端口，与用户自己正在运行的服务互不影响；`page` 模式前端开发服务器把 `/api` 固定代理到 5000，所以后端监听 5000、前端监听 8080。

**端口检查**(`ports.py`)：启动前检查启动计划中每个服务的 `port` 是否在监听。被占用时查看占用进程是否为本工具上一次遗留的进程(上一次运行的 `services.json` 中记录了进程编号与启动命令，两者都匹配才算)：是则终止它并记录事件后继续；否则不强行启动，不终止任何不属于本工具的进程。`api` 模式的端口被其他程序占用时，`api` 类检查记为「未验证」。

### 11.3 迁移检查

`migration.py` 在启动服务前执行：

1. `git diff --name-only <baseCommit>` 查看改动文件是否匹配启动计划的 `migrationPaths`。示例项目中的取值：`src/Migrations/MigrationList.cs`(10 篇 6.7)。
2. 没有匹配：启动时不会执行任何新迁移，继续。
3. 有匹配：提取新增的迁移条目，连同 `fix-<编号>.json` 中 `migration` 给出的「能否撤销与撤销方式」，生成 `kind` 为 `local-migration` 的待确认操作，`preconditions` 记录迁移文件的哈希；本次验证以 `blocked` 结束，说明将应用到测试库的条目。
4. 用户确认后执行 `verify local <编号> --confirm-migration`(或 `tightrein confirm <操作编号>` 后重跑)，迁移文件哈希一致时才启动。

核心与扩展都不读取任何配置文件与凭证，不连接数据库；迁移是否执行只从 diff 判断。

### 11.4 启动与就绪

| 项 | 做法 |
|---|---|
| 启动顺序 | 按 `services` 的顺序启动；带 `after` 的服务等所依赖的服务就绪后再启动 |
| 命令与环境 | 在 worktree 的 `services[].cwd` 中以 `services[].argv` 启动，不经过 shell；环境变量只加 `services[].env`，其余环境变量经过凭证清除 |
| 进程 | 每个服务在独立的进程组中启动，进程编号、命令与端口写入报告目录的 `services.json` |
| 日志 | 标准输出与错误输出写入报告目录的 `services/<服务名>.log` |
| 就绪判断 | 逐行读取日志，同时匹配 `readyPatterns` 与 `failPatterns`，失败信号出现即判为启动失败，避免崩溃时一直等到超时；成功信号出现后请求 `readyUrl` 确认端口可达；超过 `localRun.readyTimeoutSeconds` 视为失败 |
| 启动失败 | 本次需要该服务的检查全部记为「未验证」，原因附日志中匹配到的失败行；不重试 |
| 不启动的服务 | 启动计划的 `unavailable`。`requires` 含这些服务的检查记为「未验证」，部署后确认时再验证 |

示例项目中的取值：后端由技术栈扩展 `aspnetcore` 以 `dotnet run` 启动，环境为 `ASPNETCORE_ENVIRONMENT=Development`，成功信号为 `Now listening on`、`Application started`，失败信号为 `Unhandled exception`、`error CS` 等；前端由示例项目的项目扩展以 `npm run serve` 启动；Python 计算服务在 `unavailable` 中(10 篇 5.7、6.7)。

### 11.5 收尾

退出上下文时(包括异常与中断)：向每个进程组发送终止信号，等待 `localRun.stopTimeoutSeconds` 后仍未退出的强制结束；确认端口已释放；日志保留在报告目录；释放 `local-run` 锁。进程意外中断没有执行收尾时，下一次启动由 11.2 的遗留进程识别处理。

## 12. verify：项目 CI 的必需检查

PR 创建之后才有 CI 的结果，因此由发布跟踪读取(`ReleaseService._auto_merge`，19.6)：主分支有必需检查(`GhReader.required_checks`)时，`GhReader.pr_checks` 执行 `gh pr checks <编号> --required --json name,state,bucket`(`--json` 时退出码不反映检查结果)，`pipeline/checks/ci.py` 按 `bucket` 判定：

| 状态 | 条件 | 处理 |
|---|---|---|
| `skipped` | 主分支没有必需检查 | 不作为条件 |
| `pending` | 有进行中的检查 | GitHub 原生自动合并会等待，不作为不满足的原因 |
| `passed` | 全部通过 | 满足 |
| `failed` | 有 `fail` 或 `cancel` | 「CI 必需检查未通过：<名称>」作为不自动合并的原因，写 Issue 历史，进收件箱(09 篇 3.7) |

结果记在 release 交接文档的 `autoMerge.ci`。

## 13. verify：PR 阶段的本机检查

`verify local <编号>`：

| 步骤 | 做法 | 通过条件 |
|---|---|---|
| 1. 其他问题的回归 | 采用修复第 7 步的结果：`fix-<编号>.json` 的 `otherRegressions` 逐条成为 `other:<Issue>/<检查>` 项，不重跑 | 全部保持通过(未执行的记为未验证，不阻断) |
| 2. 是否启动服务 | `scope.py` 计算受影响的接口(`affectedEndpoints`，加上 `authz-endpoints` 输出中 `sourceFile` 属于改动文件的端点)与页面(`affectedPages`，加上 `page-routes` 输出中 `componentFile` 属于改动文件的页面路由)。没有配置 `local-run`(组装时 `VerifyDeps.local` 为空)，或既不涉及接口、也不涉及前端(或没有配置页面检查)时不启动服务，原因写进 `skipped` | — |
| 3. 接口测试 | 11.3 迁移检查后，用 `LocalService` 启动服务(涉及前端且配置了页面检查时 page 模式，否则 api 模式)，执行本 Issue 与相关其他 Issue(最近结果为 `passed`、位置属于受影响的接口或页面)的接口与页面类复现检查；受影响接口用 `probes.api_fuzz` 浅跑，`--include-path` 限定到这些路由 | 复现检查通过；浅跑没有新的失败(信号按指纹命中修复前就存在、且不属于本 Issue 的问题时列为已有问题，不计失败) |
| 4. 截图评审 | 涉及前端且配置了页面检查时，用页面运行器(`pipeline/checks/pages`)执行工作区的巡检用例并截图，见 13.2 | 巡检通过，没有布局问题 |

### 13.1 三档结论

`verdict.py` 为每个检查项给出结果，再得出整体结论：

| 单项结果 | 条件 |
|---|---|
| 验证通过 | 执行了，结果满足通过条件，并保存了证据：命令、输出、截图路径 |
| 弱证据 | 执行了，但前置条件检查不满足(例如页面上没有能触发问题的数据)，或巡检用例报告步骤因数据缺失而跳过 |
| 未验证 | 没有执行：端口被占、服务启动失败、依赖不在本机启动的服务；逐条写明本应验证的具体行为与原因 |
| 失败 | 执行了，结果不满足通过条件 |

| 整体结论 | 条件 | 去向 |
|---|---|---|
| 通过 | 没有失败项 | Issue 进入提交阶段(`phase` 为 `submit`)；弱证据与未验证的条目写入 Issue「历史」，并由 `release` 写入 PR 描述 |
| 失败 | 有失败项 | Issue 退回 `todo`，验证报告作为下一次 `fix start` 的输入；连续达到 `thresholds.verify.maxConsecutiveFailures` 次失败时转为 `needs-decision` 并设置 `hold` |
| 等待用户 | 迁移待确认、截图待查看 | 交接文档 `blocked`，Issue 状态不变 |

修复后复现的评分 `fix.repro-after-fix` 由修复的条目表给出，验证不写评分。

### 13.2 截图查看

1. 收集第 4 步的截图与对应的用例、页面说明。
2. 所配置工具支持读取图片时，调用 `fix-reviewer` 的截图评审：`instructions` 为 `roles/fix-reviewer.md` 的截图评审部分加页面说明；`workdir` 为报告目录；`access` 为 `read-only`；`readPaths` 为截图文件；工具与模型取 `stages.verify.screenshotReview`，默认标准档；`outputSchema` 为 `handoff/outputs/fix-review.schema.json`(`mode` 为 `screenshot`)。
3. 结果：每张截图为「无问题」「有问题」「无法判断」。「有问题」计为失败项；「无法判断」或工具不支持读取图片时，整体结论为「等待用户」，用户用 `verify screenshots <编号> --ok` 或 `--issue <说明>` 给出结论，`--issue` 计为失败项。

## 14. verify：部署后确认

`verify staging <编号>` 按问题的来源确认，全部由程序完成(`steps/confirm.py` 判定)：

| 对象 | 方式(`method`) | 通过 | 回归 |
|---|---|---|---|
| 本 Issue 的接口与页面类复现检查(来自 api-fuzz、e2e) | `replay`：对 `target.baseUrl` 重放当初失败的请求或用例；幂等键 `deploy-check:<Issue>:<部署 commit>:target` | 通过 | 失败 |
| 本 Issue 的静态类与测试类复现检查(来自静态巡检与修复第 5 步) | `rerun`：获取对象锁 `readonly-worktree`(时限 `thresholds.verify.readonlyLockMinutes`)，只读 worktree 切到部署 commit 后重跑；幂等键 `deploy-check:<Issue>:<部署 commit>:worktree` | 通过 | 失败 |
| 来自服务端日志(错误追踪、监控平台)的关联问题，以及复现检查不能给出结论(没有检查、未执行或前置条件不满足)时的全部关联问题 | `observe`：部署之后是否再出现(`last_seen_at` 晚于部署时间)，观察期 `thresholds.verify.observationHours`(48) | 观察期满未再出现 | 再出现 |

| 整体结论 | 条件 | 处理 |
|---|---|---|
| 回归 | 任一对象回归 | 先调用 `ReleaseService.revert` 提撤销合并的 PR(待确认操作，幂等键 `revert:<Issue>:<合并提交>`，交用户决定是否合并)，再写 `staging-failed`：Issue 重新打开为待修，「历史」记录部署 commit 与证据 |
| 通过 | 全部通过；或复现检查无法执行而关联问题已按观察期确认通过 | `staging-verified`：同步关联问题、回填分诊结果，清空 `phase`；保存运行即评测的用例 `data/eval/cases/<Issue 编号>.json` 与 `.input.json`(design 8.7，材料不全时不保存并写明原因)；编排随后起草 PR 回复 |
| 等待 | 其余(检查未执行、观察期未满) | 交接文档 `blocked`，下次运行继续；超过 `thresholds.verify.stagingWaitDays` 时在原因中提醒 |

用户需求的 Issue 没有关联问题与复现检查，部署即确认。登录 staging 所需的测试账号密码从钥匙串读取，只在内存中使用。

## 15. verify：读写的数据与报告

### 15.1 读写

| 读取 | 用途 |
|---|---|
| Issue 文件、`fix-<编号>.json` | 状态、`baseCommit`、`diffHash`、受影响范围、迁移说明、`otherRegressions` |
| `regressions` 表与 `regressions/` | 复现检查 |
| `deployments`、`problems` | 部署后确认的触发、部署时间与问题最近出现的时间 |
| 钥匙串 | 测试账号密码 |

| 写入 | 内容 |
|---|---|
| `data/verify/<编号>/<日期>-<阶段>/` | `report.md`、`services.json`、`services/*.log`、`screenshots/`、`raw/`(各探针的原始输出) |
| `regressions` 表 | 每个复现检查的最近结果与运行时间 |
| Issue 文件与 `issues` 表 | 状态、`phase`、`hold`、「历史」的验证摘要 |
| `pending_operations` | 迁移确认；回归时的撤销 PR(经 release) |
| `idempotency_keys` | 部署后确认的检查与撤销 |
| `data/runs/<运行编号>/handoff/verify-<阶段>-<编号>.json` | 交接文档 |

**交接文档 outputs**(`handoff/outputs/verify.schema.json`)：

| 字段 | 说明 |
|---|---|
| `issueId`、`phase` | `local`、`staging`(旧的 `reproduce` 记录由迁移 010 删除，文件保留) |
| `target` | 服务地址与模式，或 staging 地址 |
| `commit`、`baseCommit` | 被验证的代码与基准 |
| `items` | 每个检查项：编号、类别(本 Issue 复现、相关的其他复现、接口浅跑、页面巡检、截图、部署后确认；项目检查、补做项、全量回归只出现在旧文档中)、命令、结果(四种之一)、证据路径、原因 |
| `conclusion` | 通过、失败、等待用户 |
| `unverified` | 未验证条目：本应验证的行为与原因 |
| `skipped` | PR 阶段按条件跳过的检查与原因 |
| `confirmations` | 部署后确认：每个对象的来源、方式(`replay`、`rerun`、`observe`)、结论与说明 |
| `revert` | 回归时提的撤销 PR(操作编号与说明) |
| `migration` | 迁移检查结果与确认的操作编号 |
| `consecutiveFailures` | PR 阶段连续失败的次数 |

### 15.2 验证报告

| 部分 | 内容 |
|---|---|
| frontmatter | `type: verify-report`、`id`(`verify-<阶段>-<Issue 编号>`，与交接文档编号相同)、`issueId`、`phase`、`status`、`summary`、`tags`、`runId`、`commit`、`createdAt` |
| 结论 | 整体结论与一句话理由；失败时写第一个失败项 |
| 步骤与结果 | 每一步的命令、结果、耗时、证据路径 |
| 三档汇总 | 验证通过、弱证据、未验证的条目分别列出 |
| 未验证项 | 逐条写本应验证的具体行为与原因 |
| 截图 | 截图路径与查看结论 |
| 服务日志 | 日志路径；启动失败时摘录匹配到的失败行 |

## 16. verify：幂等与错误处理

| 场景 | 处理 |
|---|---|
| 重复 `verify local` | 每次都完整执行，生成新的报告目录；交接文档按规则覆盖并保留旧版本 |
| 重复 `verify staging` | 检查按幂等键直接取上次的结果，观察期照常重新判断；撤销 PR 按幂等键只提一次；回归后 Issue 已不在部署后确认，不会重复处理 |
| 验证中断 | `LocalService` 收尾；未完成的验证没有交接文档，下次从头执行 |
| 复现检查哈希不一致 | 报错停止，说明复现检查被改动 |
| 探针执行出错(不是检查失败) | 该项记为「未验证」并附错误，不计为失败 |
| 本机服务被外部终止 | 之后的检查记为「未验证」，原因写明服务已退出 |

## 17. release：职责与文件划分

### 17.1 职责

把验证通过的修复送进 `main`：提交、同步主干、推送、提 PR；之后只读跟踪 PR、部署与生产发布，直到部署后确认完成，最后由用户发起清理。缺省每个 git 写操作单独确认、不执行合并 PR；项目可在关卡表中把 `gates.release-writes` 设为 `auto`(建分支、提交、合并主干、推送、提 PR、发评审评论、提撤销 PR 直接执行)、把 `gates.merge` 设为 `auto`(满足全部条件后合并 PR 或开启 GitHub 自动合并，19.6)；关卡表见 09 篇 3.8。不涉及生产发布。纯脚本，不调用 LLM。分支、提交与 PR 的格式、自动合并、撤销与部署跟踪的设计见 [redesign/07-release.md](../redesign/07-release.md)。

### 17.2 代码目录

```
core/tightrein/pipeline/release/
  service.py              入口：commit、sync、push、pr、track、revert、summary、cleanup；链式推进到下一个确认点
  steps/
    precheck.py           提交前置条件：状态、最后一轮确定性检查的结果
    commit.py             提交信息与文件清单；生成 commit 操作
    sync.py               比较 origin/main；生成 merge-main 操作；冲突报告与解决后的 commit-merge 操作
    push.py               生成 push 操作；推送被拒的处理
    pull_request.py       分支名复核与建议名、标题、描述内容；生成 pull-request 操作
    track_pr.py           PR 状态查询与处理
    auto_merge.py         自动合并的路径规则与条件判断(gates.merge)
    track_deploy.py       需要手动部署的路径提示(包含合并提交的部署由 pipeline/common/deploys.py 判断)
    track_master.py       生产发布记录
    cleanup.py            收尾清理的操作
    work_summary.py       工作总结
  render/
    commit_list.py        提交确认的说明
    conflicts.py          冲突报告 data/fixes/<编号>/conflicts.md
    pr_comment.py         AI 评审结论的 PR 评论、部署后确认通过的 PR 回复草稿
    summary.py            工作总结与运行摘要中 release 一节
```

### 17.3 skill 目录

```
skills/release/
  SKILL.md
  references/
    git-operations.md
    pr-template.md
```

| 文件 | 内容要点 |
|---|---|
| `SKILL.md` | 何时使用(验证通过后提交与提 PR、查看 PR 与部署状态、清理)；每个待确认操作展示时要转述的内容(命令、分支、文件、影响、是否影响远程、能否撤销)，并请用户明确同意；同意只对这一个操作有效；推送被拒、合并冲突、PR 被关闭时的处理方式；本工具不合并 PR |
| `references/git-operations.md` | 7.10 的写操作确认汇总；不使用 `rebase`、`--force`、`reset --hard`、`commit --amend`；冲突时两侧的说明方式与「互斥实现交用户选择」 |
| `references/pr-template.md` | 通用的 PR 标题与描述(正文三段、关联、验证结果)、项目 PR 模板的填写方式 |

## 18. release：命令与前置条件

| 命令 | 特有参数 | 作用 | 前置条件 |
|---|---|---|---|
| `release <编号>` | — | 从当前进度连续执行 commit、sync、push、pr，停在下一个待确认操作 | Issue 处于提交阶段(进行中，`phase` 为 `submit`) |
| `release commit <编号>` | `--accept-findings` | 提交 | Issue 处于提交阶段；修复最后一轮的确定性检查全部通过，或用户以 `--accept-findings` 确认照常提交(接受的未通过项写进 Issue 历史与 PR 描述) |
| `release sync <编号>` | `--continue`、`--abort` | 同步主干；冲突解决后继续或放弃合并 | Issue 处于提交阶段或 `pending-merge`；工作区干净(`--continue` 除外) |
| `release push <编号>` | — | 推送 | Issue 处于提交阶段；分支上有未推送的提交；`origin/main` 没有未合并的新提交 |
| `release pr <编号>` | — | 提 PR 或更新描述 | Issue 处于提交阶段；分支已推送 |
| `release track` | — | 跟踪全部有 PR 的 Issue 的 PR、部署与生产发布状态；定时运行 | 无 |
| `release revert <编号>` | `--reason <原因>` | 提撤销合并的 PR(19.12) | Issue 为 `done` 且有合并提交 |
| `release summary <编号>` | — | 生成工作总结 | Issue 为 `done` |
| `fix cleanup <编号>` | — | 收尾清理 | Issue 为 `done` 或 `cancelled` |

## 19. release：处理步骤

### 19.1 提交(7.2)

1. **前置条件**(`precheck.py`)：Issue 处于提交阶段(进行中，`phase` 为 `submit`)；`fix-<编号>.json` 的 `status` 为 `ok`，最后一轮的确定性检查(含交付规则)全部通过。不满足时列出未通过的检查项、位置与问题，交接文档 `blocked`，提示先执行 `fix apply <编号> --review-only` 或 `fix start`。
2. **提交信息**：按项目约定的提交格式(19.4)生成，通用为 Conventional Commits `<类型>(<范围>): <一句话>`，空一行后写原因；类型由任务类型按 `git.commitTypes` 映射，范围、一句话与原因取 `fix-executor` 输出的 `release` 字段(项目语言)，旧修复没有时一句话取 Issue 标题、原因取修复摘要、范围省略。
3. **文件清单**：`vcs.status(worktree)` 列出改动的文件，按文件名排序；与 `fix-<编号>.json` 的 `changedFiles` 比对，不一致时停止并说明差异(说明 `apply` 之后工作区有变化)。
4. **待确认操作**：`kind` 为 `commit`(`gates.release-writes: auto` 时与合并主干、推送、提 PR 一样生成后直接执行，见 02 篇 4.5；`release <编号>` 随之继续下一步，Issue 离开提交阶段即停)，命令为 `git add -- <文件…>` 与 `git commit -F <提交信息文件>`(提交信息写在 `raw/vcs/<操作编号>/` 下)；说明中列出提交信息与文件清单，影响为本地分支新增一个提交，撤销方式为 `git reset --soft HEAD~1`(由用户自行执行)。
5. **幂等**：幂等键为「Issue 编号 + 改动内容的哈希」；工作区没有改动时不生成操作。
6. **执行后续**：记录 commit，Issue「历史」追加一行。

### 19.2 同步主干(7.3)

1. `vcs.fetch()`，比较修复分支与 `origin/main`：`git rev-list --count HEAD..origin/main`。
2. 没有新提交：进入推送。
3. 有新提交：确认工作区干净，生成 `kind` 为 `merge-main` 的操作，命令为 `git fetch origin` 与 `git merge --no-ff --no-edit origin/main`；说明中列出 `origin/main` 上的新提交与它们改动的文件、与本修复改动文件的交集、影响为修复分支新增一个合并提交、撤销方式为合并前的 commit。只使用 merge。
4. **无冲突**：合并提交完成后，Issue 回到合并前验证(`main-merged`：进行中，`phase` 为 `verify`；从 `pending-merge` 进入时同样如此)，交接文档 `nextAction` 为「重新执行合并前验证」；验证通过后回到提交阶段，再执行 `release` 时同步检查已无新提交，直接推送。
5. **有冲突**：`merge-main` 执行失败，`vcs` 以 `MergeConflict` 返回冲突文件清单，合并停在进行中的状态。`render/conflicts.py` 生成冲突报告：每个冲突文件列出修复分支一侧与 `origin/main` 一侧的相关提交(`git log` 限定到该文件)、冲突的片段；两侧对同一功能各有实现时明确标注「互斥实现，需要用户选择保留哪一侧」。交接文档 `blocked`。本工具不自行选择任何一侧，也不修改冲突文件。
6. **冲突解决后**：用户在 worktree 中解决冲突后执行 `release sync <编号> --continue`：检查所有冲突文件已没有冲突标记、`git diff --check` 通过；对每个冲突文件比较解决结果与两侧版本，记录取舍(与修复侧一致、与主干侧一致、两侧合并)；生成 `kind` 为 `commit-merge` 的操作(`git add -- <冲突文件…>`、`git commit --no-edit`)。执行后按第 4 步处理。
7. **放弃合并**：`release sync <编号> --abort` 生成 `kind` 为 `abort-merge` 的操作(`git merge --abort`)，说明它会丢弃合并中的全部改动并回到合并前的状态。

### 19.3 推送(7.4)

1. 生成 `kind` 为 `push` 的操作，命令为 `git push -u origin <分支>`；说明中列出目标远程分支与将推送的提交(`git log --oneline origin/<分支>..HEAD`，首次推送为 `origin/main..HEAD`)，影响远程，撤销需要用户在远程删除分支或另行推送。
2. **推送被拒**：操作标为 `failed`，记录 git 的输出，停下说明原因；通常是远程有新提交，提示执行 `release sync`。不使用 `--force`，`vcs` 也不提供强制推送。
3. 执行后续：Issue「历史」记录推送的提交。

### 19.4 提 PR(7.5)

**项目约定**(`pipeline/common/conventions.py`，fix 与 release 共用；`resolve(config, repo) -> Conventions`，每项带来源)：分支、提交与 PR 模板按优先级取：

1. 工作区 `project.yaml` 的 `git.conventions.branch`、`git.conventions.commit`(格式串，占位符 `{prefix}`、`{type}`、`{issue}`、`{slug}`、`{scope}`、`{summary}`)与 `git.conventions.prTemplate`(仓库内的模板路径)；
2. 项目中写明的约定：PR 模板(`.github/PULL_REQUEST_TEMPLATE.md` 等，GitLab、Bitbucket 的对应文件)；commitlint 配置继承 `config-conventional` 时提交采用 Conventional Commits；`CONTRIBUTING.md`、`AGENTS.md`、`CLAUDE.md` 中同一行写了分支(或提交)并在反引号中给出带占位符的模板，占位符全部认得、全文只有一个这样的模板时采用；
3. 从历史推断(`infer`，最近 `git.inference.sampleSize` 条提交与远程分支，统一比例达到 `git.inference.minRatio` 时给出结果)：只供接入时确认，不自动采用；
4. 通用格式(`domain/release_format.py`)：分支 `{prefix}{type}/{issue}-{slug}`，提交 `{type}{scope}: {summary}`。

个人前缀只在 `git.personalPrefix` 为真或采用的分支模板含个人前缀时加。

1. **分支名复核**：符合约定的分支格式，个人前缀不在 `git.forbiddenPrefixes` 中；不符合时停止，给出建议名称，询问是否先改名(改名是新的待确认操作)。
2. **标题**：`fix-executor` 输出的 `release.prTitle`(一句祈使语气的完整话)；没有时取 Issue 标题。
3. **描述**：按 19.5 生成，写入 `data/fixes/<编号>/pr-body.md`。
4. **幂等**：幂等键为分支名。`vcs.plan_pull_request` 构造时查询 `pr_for_branch(branch)`：查到已有打开的 PR 时，描述有变化才生成命令为 `gh pr edit <编号> --body-file <文件>` 的操作，否则不生成。
5. **待确认操作**：`kind` 为 `pull-request`，没有已存在的 PR 时命令为 `gh pr create --base main --head <分支> --title <标题> --body-file <文件>`；说明中列出标题、描述全文路径与目标分支 `main`，影响远程，撤销方式为在 GitHub 上关闭 PR。
6. **执行后续**：解析返回的 PR 链接与编号，写入 `pulls` 表与 Issue 的 `pr` 字段，Issue 状态改为 `pending-merge`，发本机通知提醒用户审核，返回链接；`release.reviewComment` 为真时把修复最后一轮评审的结论以评论发到 PR(19.6)。

### 19.5 PR 描述的生成

内容全部取自已有的文档，不调用 LLM(`domain/release_format.pull_body`)，不分固定小节：

| 部分 | 内容来源 |
|---|---|
| 正文三段 | 解决什么问题、为什么这样做、局限：取 `fix-executor` 输出的 `release.problem`、`approach`、`limitations`；没有时问题取 Issue 的「问题」、做法取修复摘要 |
| 关联 | `Closes #<编号>`(Issue 有 GitHub 镜像时；镜像仓库与 origin 不同时写 `Closes owner/name#<编号>`)、父 Issue 与排在前面的子任务的链接 |
| 验证结果(程序生成) | 复现测试修复前失败、修复后通过；修复后的项目检查；合并前验证的各项(弱证据与「未验证」逐条列出)；照常提交时接受的未通过项 |

项目有 PR 模板时(`fill_template`)，按模板的标题把同样的内容放进对应段落(标题含问题、背景、概要、summary → 问题；why、方案、实现 → 做法；局限、风险 → 局限；测试、验证 → 验证结果；关联、issue → 关联)，认不出的标题保留模板原文，复选框原样保留。PR 合并进默认分支时 GitHub 按 `Closes` 自动关闭镜像。重新生成描述时(同步主干后、修改后再次推送)同样按上表，保证描述与当前分支一致。

### 19.6 PR 状态跟踪(7.6)

`release track` 对每个 `pending-merge` 的 Issue 执行只读查询 `vcs.pr_view(repo, number)`(02 篇 4.3)，结果写入 `pulls`：

| PR 状态 | 处理 |
|---|---|
| 已合并 | `pr-merged`：Issue 为 `done`(关闭原因 `fixed`，`phase` 为 `deploy-check`)，记录合并时间与合并提交，进入部署跟踪 |
| 已关闭且未合并 | `pr-closed`：Issue 为 `cancelled`(关闭原因 `fix-rejected`)；关联问题转为 `ignored`，恢复条件为严重度升级或在新版本中再次出现；PR 上的用户说明原文追加到 Issue「历史」；关闭理由为「不是缺陷」「无法复现」一类时，对应分诊结论的 `outcome` 回填 `false-confirm` |
| 仍然打开，超过 `thresholds.release.prReminderWorkdays`(3 个工作日) | 运行摘要与周报中提醒，每天最多一次 |
| 仍然打开，`mergeable` 为冲突 | 通知用户，提示执行 `release sync <编号>` 在本地合并 `main` 解决，不在网页上解决冲突 |

**AI 评审结论写进 PR**(`release.reviewComment`，缺省 true)：PR 创建后，`render/pr_comment.py` 把修复最后一轮评审的结论(各评审模式通过或有阻断项、评审文档的结论)写成评论，注明只作说明、不是批准，生成 `kind` 为 `pr-comment` 的操作(`gh pr comment`；`gates.release-writes: auto` 时直接执行)；同一轮评审只发一次，记进交接文档 `reviewComment`。

**自动合并**(关卡 `gates.merge: auto`，缺省 `user`)：PR 仍然打开时由 `ReleaseService._auto_merge` 判断：

1. **路径规则**：修复改动的文件命中 `release.autoMergeBlockPaths`(写法同 `protectedPaths`，核心缺省覆盖 CI 工作流、依赖清单与锁文件、迁移、权限认证、密钥配置，技术栈与项目以 `autoMergeBlockPaths+` 追加)时不自动合并，写决策简报 `data/fixes/<编号>/merge-decision.md`(decision 类型：背景为命中的文件与规则，选项为审阅后合并、补充评审后合并、关闭 PR，附推荐与理由)并通知，同一组命中只写一次。
2. 用 `vcs.pr_merge_facts`(`gh pr view --json state,isDraft,mergeable,mergeStateStatus,headRefOid,reviews`)读取合并所需字段，`GhReader.required_checks` 读主分支的分支保护与规则集。
3. **有必需检查**：`steps/auto_merge.own_blockers` 判断不看 GitHub 检查状态的条件(合并前验证结论为通过；修复最后一轮检查全部通过且没有以 `--accept-findings` 接受的未通过项；PR 头部 commit 等于本工具最近一次推送的 commit)，并且 CI 必需检查没有失败(第 12 章，`autoMerge.ci`)，满足时构造 `merge-pull-request` 并带 `--auto` 执行，开启 GitHub 原生自动合并(同一头部 commit 只开启一次)，由 GitHub 在检查通过后合并。
4. **没有必需检查**：`steps/auto_merge.blockers` 另外要求 PR 打开、不是草稿、`mergeable` 为 `MERGEABLE`、`mergeStateStatus` 为 `CLEAN` 或 `HAS_HOOKS`、没有评审者最近一次评审为 `CHANGES_REQUESTED`；满足时构造 `merge-pull-request` 并立即执行，随即重读 PR，按上表「已合并」推进。

两种方式的合并方式都取 `release.mergeMethod`(缺省 squash，可选 merge)，带 `--match-head-commit` 并删除远程修复分支(本地分支与 worktree 仍由 19.10 删除)。任一条件不满足则不合并，原因写进交接文档 `autoMerge`、track 的输出行与运行摘要「自动决定」，原因变化时写一行 Issue「历史」。

用户要求修改后再合并时，执行 `fix start <编号>`(Issue 从 `pending-merge` 转为进行中、`phase` 为 `fix`)，之后重新验证、提交、推送，PR 随推送自动更新，描述按 19.4 第 4 步更新。

### 19.7 部署跟踪(7.7)

部署记录经扩展点 `deploy-source`(10 篇 3.9)读取，平台读取是方法目录中的只读方法(`core/github-actions`、`core/github-deployments`、`core/vercel`)；匹配合并提交与观察期的判断在核心的 `pipeline/common/deploys.py`，`release track`、采集的目标版本、编排的部署检测与 `verify staging` 共用。对每个完成且等待部署后确认(`phase` 为 `deploy-check`)的 Issue：

1. `ExtensionClient.deploy_source` 读取最近的部署记录(`id`、`commit`、`status`(running、succeeded、failed、skipped)、`environment`、`url`、时间)。
2. 在记录中找包含合并提交的部署：先找 commit 相同的最新一次；它被跳过或没有时，找 commit 包含合并提交(`is_ancestor`)的最早一次；部署的 commit 在本地不存在时先 fetch 一次，仍不存在的不计入。
3. 结果按 commit 写入 `deployments`(已存在则更新)，`status` 取 `DeploymentStatus`；交接文档 `deployments` 记 `source: deploy-source`。
4. **没有配置部署来源**：合并时间加 `release.deploy.observationHours`(缺省 24)之后按已部署记录(`source` 为 `merge-time`)，在此之前的跟踪结果写明「未配置部署来源，以合并时间加观察期为准」。

| 结果 | 处理 |
|---|---|
| 部署成功 | 记录部署时间；编排层据此调用 `verify staging` |
| 部署失败 | 立即发本机通知并附部署链接；Issue「历史」记录；是否由本修复引起由用户判断 |
| 仍在进行 | 下次运行继续跟踪 |

改动文件匹配 `target.manualDeployPaths` 时，Issue「历史」与通知中提示用户联系负责人手动部署这部分改动。示例项目中的取值：`src/compute/`，即需要手动更新的计算节点。

**部署后确认通过后**：`render/pr_comment.py` 起草「已在测试环境验证通过」加验证摘要，写入 `data/fixes/<编号>/pr-comment.md` 并展示给用户，由用户确认内容后自行回复到 PR 中；这句回复不由本工具发表。

### 19.8 生产发布记录(7.8)

每次 `release track` 对已合并、尚未记录进入 `master` 的 Issue，在 `vcs.fetch()` 之后执行 `vcs.branches_containing(repo, merge_commit)`；包含 `origin/master` 时在 `pulls` 记录日期，Issue「历史」追加一行。Issue 完成后仍继续检查，直到记录为止。

### 19.9 工作总结

`release summary <编号>` 在 PR 合并后由用户选择执行，生成 `data/fixes/<编号>/summary.md`：一个标题加不超过 4 条，只写功能不写实现(内容取自 Issue「结论」、`userVisibleChange` 与验证结论)，附合并前验证中的 2 张截图路径(优先受影响页面、有数据的截图)。没有截图时：合并前验证的页面类检查没有通过(未执行或未验证，例如端口被占、启动失败)或没有合并前验证的结果，写明「页面验证未执行(原因)，界面变化未确认」；否则写「无界面变化」。

### 19.10 收尾清理(7.9)

Issue 完成且部署后确认通过后，运行摘要提示用户执行 `fix cleanup <编号>`：

1. 生成 `kind` 为 `cleanup` 的操作，`confirmations_required` 为 2，命令依次为 `git worktree remove <worktree 路径>`、`git branch -d <分支>`、`git fetch --prune`。
2. 第一次确认时展示风险说明：worktree 中未提交的内容会丢失(先检查工作区是否干净，不干净时列出文件并停止)；`git branch -d` 只删除已合并的分支，未合并时 git 会拒绝；远程分支由用户合并 PR 时在 GitHub 上删除。第二次确认要求输入分支名。
3. `git branch -d` 被拒绝时停止并说明分支未合并，本工具不使用 `-D`。
4. `regressions/<编号>/` 不删除，继续作为回归测试；`data/fixes/<编号>/` 按保留期由 `retention.py` 处理。

Issue 取消时同样可以执行清理，第 3 条的拒绝情况在说明中提前写明。

### 19.11 确认汇总

| 操作 | 步骤 | `kind` | 确认方式 |
|---|---|---|---|
| 建分支、建 worktree | 3.3 | `create-fix-worktree` | `issue approve` 时当场确认 |
| 计划确认 | 4.7 | `fix-plan` | `fix confirm` |
| 应用迁移到测试库 | 11.3 | `local-migration` | `verify local --confirm-migration` |
| `git add`、`git commit` | 19.1 | `commit` | 确认提交信息与文件清单 |
| 合并 `origin/main` | 19.2 | `merge-main`、`commit-merge`、`abort-merge` | 单独确认；冲突由用户逐个文件解决 |
| `git push` | 19.3 | `push` | 确认目标分支与提交 |
| `gh pr create`、`gh pr edit` | 19.4 | `pull-request` | 确认标题、描述与目标分支 |
| 删除 worktree 与本地分支 | 19.10 | `cleanup` | 用户主动发起，二次确认 |
| 在 PR 上发评审结论评论 | 19.6 | `pr-comment` | 确认评论内容 |
| 合并 PR 并删除远程分支，或开启 GitHub 自动合并 | 19.6 | `merge-pull-request` | 只在 `gates.merge: auto` 且满足全部合并条件时由本工具执行；否则由用户在 GitHub 上操作 |
| 提撤销合并的 PR | 19.12 | `revert-pull-request` | 确认分支、原因与 PR 内容；撤销 PR 不自动合并 |
| 关闭 PR | 19.6 | — | 用户在 GitHub 上操作，本工具不执行 |

关卡 `gates.release-writes: auto` 的项目，`create-fix-worktree`、`commit`、`merge-main`、`push`、`pull-request`、`pr-comment`、`revert-pull-request` 生成后直接执行(02 篇 4.5)；`gates.plan-confirm: auto` 时 `fix-plan` 满足规则即自动确认(4.7)。`commit-merge`、`abort-merge`、`local-migration` 不受关卡表放行，`cleanup` 属于必须交用户的关卡 `delete`。

### 19.12 撤销合并

`release revert <编号> --reason <原因>`(`ReleaseService.revert`)：Issue 为 `done` 且有合并提交时，生成 `kind` 为 `revert-pull-request` 的待确认操作：从 `origin/main` 建分支(按约定的分支格式，类型 `hotfix`，描述 `revert-<简称>`)与临时 worktree，`git revert --no-edit <合并提交>`(`release.mergeMethod` 为 merge 时加 `-m 1`)，推送，`gh pr create`(标题「Revert: <原 PR 标题>」，描述写原因、原 PR 与 Issue、交用户决定是否合并)。执行后记进交接文档 `revert` 与 Issue 历史，并通知；不改变 Issue 状态。

## 20. release：读写的数据

| 读取 | 用途 |
|---|---|
| Issue 文件 | 状态、标题、「结论」 |
| `fix-<编号>.json`、`verify-local-<编号>.json` | 确定性检查结果、改动文件、PR 描述的内容 |
| `pulls`、`deployments`、`pending_operations` | 跟踪与确认状态 |

| 写入 | 内容 |
|---|---|
| 修复分支与远程 | 经确认的提交、合并、推送、PR |
| `pending_operations`、`idempotency_keys` | 待确认操作与幂等键 |
| `pulls` | PR 编号、链接、分支、标题、状态、`mergeable`、合并提交、合并时间、关闭时间、关闭说明、评审意见(编号、作者、正文摘要、时间)、最近检查时间、最近提醒时间、进入 `master` 的日期 |
| `deployments` | 部署 commit、部署编号、状态、链接、部署时间、检测时间 |
| Issue 文件与 `issues` 表 | `pr`、状态、关闭原因、「历史」 |
| `problems`、`problem_events`、`triage_results.outcome` | PR 被关闭时的连带处理 |
| `data/fixes/<编号>/` | `conflicts.md`、`pr-body.md`、`pr-comment.md`、`merge-decision.md`、`summary.md` |
| `data/runs/<运行编号>/handoff/release-<编号>.json` | 交接文档 |

**交接文档 outputs**(`handoff/outputs/release.schema.json`)，每次运行在上一版的基础上累积：

| 字段 | 说明 |
|---|---|
| `issueId`、`branch` | — |
| `commits` | 每个提交的 commit、提交信息、文件清单、确认的操作编号 |
| `syncs` | 每次同步：合并的 `origin/main` commit、冲突文件与每个文件的取舍、合并提交 |
| `push` | 远程分支、推送的提交、时间 |
| `pr` | 编号、链接、标题、描述文件的哈希、状态、`mergeable`、合并提交、合并与关闭时间、用户说明 |
| `deployments` | 部署编号(以合并时间为准时为 `merge-time`)、commit、结论、链接、来源(`deploy-source` 或 `merge-time`) |
| `masterAt` | 进入 `master` 的日期 |
| `pendingOperations` | 本 Issue 当前等待确认的操作编号 |
| `cleanup` | 清理的操作编号与结果 |
| `autoMerge` | `gates.merge` 为 `auto` 时最近一次合并判断：是否已合并、是否开启 GitHub 原生自动合并、CI 必需检查的状态(`ci`)、不满足的条件、决策简报路径、合并操作编号、判断时间 |
| `reviewComment` | 已发到 PR 的评审结论：PR 编号、评审轮次、评论操作 |
| `revert` | 撤销合并的 PR：原因、分支、操作编号、PR 链接 |

## 21. release：幂等与错误处理

| 场景 | 处理 |
|---|---|
| 重复执行 `release` | 各步骤按幂等键判断：没有新改动不提交、没有新提交不推送、已有 PR 只在描述变化时更新 |
| 确认后状态已变化 | 操作标为 `expired`，重新生成 |
| 待确认操作执行失败 | 标为 `failed`，记录输出；模块停下说明原因，不自动重试 |
| `gh` 未登录或网络失败 | 只读查询失败时本次跟踪跳过该 Issue，运行摘要中说明；写操作失败按上一条处理 |
| 合并进行中被中断 | 下次 `release sync` 检测到合并进行中，按冲突未解决处理 |
| 推送被拒 | 19.3 第 2 步 |
| 通知重复 | 按「事件类型 + 对象 + 日期」去重 |

## 22. 测试

### 22.1 单元测试

| 对象 | 覆盖 |
|---|---|
| `fix/steps/workspace.py` | 分支名生成、规则校验、禁用前缀、同名分支加编号 |
| `fix/steps/repro.py` | 每种探针的生成器；`hash` 篡改检测 |
| `fix/steps/repro_test.py` | 测试类的路径与命令白名单的正反样例；基准版本上须失败、须通过(重构)、缺陷类通过时 `not-reproduced`、写不出 |
| `orchestrator/policy/lanes.py` | 流程表每一格、类型没有单独一行时取 `default`、档未知按中档、超限；勘察、复现测试方式与评审方式的判定 |
| `fix/steps/plan.py` 的代码检查 | 4.6 表中每一行 |
| `fix/steps/checks.py` | 改动量统计(含未跟踪文件)、当前档与单个 PR 上限、`when` 匹配、`affected` 的映射与映射为空时的整组运行、`mustNotModify`、diff 规则与交付规则的正反样例 |
| `fix/steps/risk.py` 与 `domain.fix.risk` | `05-fix.md` 5.8 规则表的每一格各一条命中与未命中的样例；规则为空时一律为常规 |
| `fix/steps/review.py` | 不带位置、位置不存在、缺少触发条件、类别不在枚举中的问题被丢弃并记入 `discardedFindings` |
| `fix/steps/triage_blockers.py` | 4.11 每种性质与多种性质并存时的选择 |
| `verify/steps/verdict.py` | 13.1 两张表的每一行 |
| `verify/steps/ports.py` | 端口空闲、被遗留进程占用、被其他进程占用 |
| `domain/release_format.py`、`common/conventions.py`、`release/steps/pull_request.py` | 通用格式与 PR 模板填写、项目约定的四级优先级与历史推断、标题与描述三段的来源 |
| `release/steps/sync.py` | 冲突取舍的判定(与修复侧一致、与主干侧一致、两侧合并) |
| 待确认操作 | 前置状态变化导致 `expired`；二次确认；`--dry-run` 与 `--output` 不写入 |

### 22.2 本机启动脚本

用夹具服务替代真实后端与前端：一个按参数输出成功信号、失败信号或不输出任何信号的小程序，监听指定端口；`local-run` 以输出固定启动计划的假扩展替代。测试覆盖：没有 `local-run` 扩展时全部记为未验证、`after` 的启动顺序、成功就绪、失败信号立即返回、超时、端口被遗留进程占用时的清理、端口被其他进程占用时不启动、异常退出时的收尾(进程组被终止、端口释放、锁释放)、迁移文件有改动时停止并生成确认。

### 22.3 回放测试

`tests/replay/fix/<用例>/`、`tests/replay/verify/<用例>/`、`tests/replay/release/<用例>/`：

| 文件 | 内容 |
|---|---|
| `input/` | 上游交接文档、Issue 文件、数据库初始数据 |
| `repo/` | 夹具仓库的 commit 与一个本地裸仓库作为 `origin` |
| `recordings/` | `fix` 各角色的录制结果；`fix-executor` 的录制结果包含一份补丁，`replay` 适配器把它应用到 worktree，以重现代码改动 |
| `bin/gh` | 按夹具返回 PR 与部署记录的假 `gh` 程序，放在测试的 `PATH` 最前面 |
| `probes/` | `api_fuzz`、`e2e` 执行部分的录制结果 |
| `expected/` | 期望的交接文档、数据库变化、`pending_operations`、人读文档、worktree 的最终 diff |

待确认操作在测试中以「自动确认」或「自动拒绝」的夹具设置处理。用例至少覆盖：A 通道一次通过的修复(轻量评审)、A 通道超档转 B、B 通道命中权限规则的高风险修复(轻量加深度评审)、局部问题两轮修正后通过、计划没覆盖导致重出、guards 违规停下、设计问题转待决定、复现测试在基准版本上通过(退回分诊)、合并前验证因端口被占记为未验证、截图待查看、迁移待确认、同步主干有冲突、推送被拒、PR 被关闭为修复未采纳、部署被跳过后跟踪最新一次、部署后复现失败重新打开。

### 22.4 沙箱运行

`--output <目录>` 模式下：`fix` 在临时 worktree 中实施(不使用 Issue 的修复 worktree)，产出写到指定目录；`verify` 照常启动本机服务，报告写到指定目录，不改 Issue 状态；`release` 只渲染提交信息、文件清单、冲突报告、PR 标题与描述，不生成也不执行任何待确认操作。评测运行器(03 篇第 2 章)以此模式对 `fix` 的评测用例打分：修复条目表中由代码判定的各项由 `verify local` 在沙箱中给出，评审项由 `evaluation` 的模型评审给出(03 篇 2.6.4)。

本篇用到的基础层定义(编号、枚举、表、路径、配置)统一见 01-foundation.md。
