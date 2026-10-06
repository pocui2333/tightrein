# 编排层与入口层：orchestrator、cli、skills、packaging

本篇描述编排层与入口层的内部设计，以及把 skills 安装到各 agent 工具的方式：`orchestrator` 决定接下来调用哪个模块；`cli` 是全部功能的唯一命令入口；`skills` 告诉 agent 该调用哪个命令、如何解读结果；`packaging` 与 `third_party` 负责安装与版本锁定；launchd 负责定时触发。业务规则见 `docs/explanation/design/09-packaging.md` 与 `docs/explanation/design/15-standalone-run.md`。

编号、枚举、实体、表、路径与配置沿用 `01-foundation.md` 的定义。

## 1. 职责与边界

| 组件 | 负责 | 不负责 |
|---|---|---|
| `orchestrator` | 读取状态与时间表，判断到期任务；按固定顺序调用各模块的 service；按状态表续跑单个对象；中断恢复；运行记录；运行摘要与本机通知 | 任何模块内部的判断(是否值得修、是否通过验证等)；直接读写模块的业务表 |
| `cli` | 参数解析、选择器解析、调用 orchestrator 或模块 service、输出(人读或 JSON)、退出码、待确认操作的展示与执行入口 | 业务逻辑 |
| `skills` | 用自然语言说明何时用哪个命令、如何解读输出与退出码、在哪些关口必须停下等用户 | 自行实现任何判断或写操作；复述核心中已有的规则表 |
| `packaging` | 把同一份 `skills/` 与锁定的第三方 skill 安装到 Claude Code、Codex CLI、Antigravity CLI(agy)；安装后核对 | 为某个工具单独维护一份 skill |
| `third_party` | 锁定外部 skill 的来源、版本与哈希；安装记录 | 修改外部 skill 的内容 |

## 2. 文件划分

```
core/tightrein/
  orchestrator/
    service.py          run、next、continue 的入口
    schedule.py         时间表：由 project.yaml 的 schedule 计算应执行时刻、判断到期
    rules.py            一次 run 中的固定步骤顺序与每步的触发条件
    resume.py           续跑：对象识别结果 → 状态表 → 逐步执行，直到终点或关口
    recovery.py         中断识别与接管
    runlog.py           run 记录(runs 表中 stage 为 loop 的记录)与子运行的关联
    summary.py          运行摘要的交接文档与通知
    inbox.py            待用户决定的收件箱：事项与推荐做法(3.7)
    digest.py           每日汇总(progress 类型交接文档，3.7)
    breaker.py          熔断(3.10)
    pause.py            暂停与恢复(3.11)
    onboarding/         接入：阶段、清单的检查与回答、onboarding.md(3.12)
    policy/             编排拥有的关卡规则，只读配置的纯函数，流水线模块可以调用(00 篇依赖规则第 1 条的例外)
      autonomy.py       自主决定：Issue 自动放行与修复计划自动确认的规则(关卡表 config/gates.py 决定是否使用，3.8)
      lanes.py          修复的流程表：规模档、「类型 × 档」到通道、勘察、复现测试方式与评审方式(07 篇 4)
  cli/
    main.py             顶层解析器与通用参数
    commands/           每个顶层命令一个文件：run.py、status.py、next.py、continue_.py、find.py、
                        pending.py、collect.py、aggregate.py、problem.py、triage.py、issue.py、
                        fix.py、verify.py、release.py、learn.py、kb.py、worktree.py、
                        eval.py、install.py、third_party.py、schedule.py、skills.py、workspace.py
    selectors.py        15.3 的选择器解析，产出结构化的 Selector
    output.py           人读输出与 --json 输出
    exit_codes.py       退出码常量与异常到退出码的映射
    confirm.py          待确认操作的终端交互确认
  observability/
    notify.py           本机通知与去重(01 篇 6.3)
skills/
  loop/
    SKILL.md
    references/
      commands.md       命令速查：意图 → 命令 → 关键参数
      gates.md          每类关口的展示方式与用户同意后的命令
      exit-codes.md     退出码的含义与处理
      phrasing.md       常见说法到对象与目标的对应
  collect/  aggregate/  triage/  issue/  fix/  verify/  release/  learn/
core/tightrein/packaging/     安装代码在核心包内；插件清单与 plist 由代码生成，不用模板
  skills_check.py       skills check
  install.py            安装目标解析、冲突检查、安装与卸载、核对、安装记录读写
  claude.py             构建插件目录、claude plugin 命令
  third_party.py        锁定清单、目录哈希、下载与校验
  launchd.py            plist 内容与 launchctl 命令
third_party/
  skills.lock.yaml      外部 skill 的锁定清单
  installed.json        本机安装记录(不纳入版本管理)
```

## 3. orchestrator

### 3.1 一次 run 的步骤

`tightrein run` 在全局运行锁 `data/run.lock` 下执行，同一工作区同一时间只有一个 run。获取失败时，定时运行直接退出(退出码 7)并在 `schedule_state` 中记录「跳过：上一次运行尚未结束」。暂停时不发起运行(3.11)；工作区接入中时第 2 步起只做接入检查(3.12)。

`rules.py` 定义固定的步骤顺序。每一步先判断触发条件，满足才调用对应模块；每个模块调用都包在独立的错误边界中，失败只记录、不影响后续步骤(9.8 第 3 条)：

| 顺序 | 步骤 | 触发条件 | 调用 |
|---|---|---|---|
| 1 | 中断恢复 | 总是 | `recovery.py`(3.5) |
| 2 | 部署检测 | 总是 | `collect deployments`：只读查询部署记录，新部署写入 `deployments` |
| 3 | 部署后浅跑 | `deployments` 中存在尚未浅跑的成功部署 | `project worktree sync` 到该 commit，`schedule.onDeploy` 中列出的探针逐个 `collect`，随后 `aggregate --select run:<本次各探针运行>` |
| 3a | 新提交的增量巡检 | fetch 后主分支的最新提交与最近一次静态巡检的目标 commit 不同(从未巡检过的项目不触发，第一次是基线审查) | `collect --probe static --level incremental --commit <最新提交>`，随后 `aggregate` |
| 4 | 定时任务 | `schedule.tasks` 中到期的任务(3.2) | 任务配置的命令，采集类任务之后接 `aggregate` |
| 5 | 分诊 | 存在状态为 `new` 或 `regressed` 的问题 | `triage --select status:new,regressed`(数量上限由 `triage` 自己控制) |
| 6 | 创建 Issue | 存在去向为 `create-issue` 且没有 Issue 的问题 | `issue create` |
| 6a | 无人值守推进 | `gates.fix-session` 为 `auto` 且存在待修或进行中、排在前面的子任务已完成的 Issue；按处理标签与严重度排序(立即修先于排期修)，处于修复阶段的 Issue 不超过 `loop.maxActiveFixes`(缺省 1)，已过修复阶段的照常推进 | 还没有修复分支的用户需求与后续子任务先 `fix prepare`；之后按状态表续跑到 `release`(`Resumer(unattended=True, until=release)`)，修复不启动交互会话(07 篇 3.4 第 7 条)；熔断(3.10)；停下处写进 notes，失败记为异常 |
| 7 | PR 与部署跟踪 | 存在 `pending-merge` 的 Issue，或完成且等待部署后确认(`phase` 为 `deploy-check`)的 Issue | `release track` |
| 8 | 部署后确认 | 存在完成且等待部署后确认、包含其合并提交的部署已成功(没有配置部署来源时观察期已过)的 Issue | `verify staging <Issue 编号>`(07 篇 14)；确认通过的起草 PR 回复 |
| 9 | GitHub 镜像 | `issues.tracker` 为 `github` | `IssueService.mirror()`：读回 GitHub 上的开关状态，推送状态标签、评论与开关状态(06 篇 10.8)；整次跳过(公开仓库、读取失败)记为异常，未同步项列入运行摘要 |
| 10 | 学习 | 总是 | `learn lessons`：出问题时写经验，修好的缺陷生成并验证 Semgrep 规则 |
| 11 | 周任务 | 周任务到期(本周第一个工作日，与静态巡检全量扫描同一时刻) | `learn report` |
| 12 | 保留期清理 | 当天尚未执行 | `store/retention.py` |
| 13 | 健康检查 | 总是 | `learn health` |
| 14 | 运行摘要 | 总是 | `summary.py` 与每日汇总 `digest.py`(3.7) |

- 触发条件只读取状态表与时间表，判断「有没有需要调用的对象」，不判断对象该如何处理。
- 需要用户的事项(放行 Issue、确认修复计划、确认 git 写操作、审核 PR)按关卡表(3.8)交用户时不在 run 中推进，相关模块停在 `blocked`，事项进入收件箱。`gates.fix-session` 为 `auto` 时 `continue` 同样以无人值守方式修复(不要求 `--interactive`)。
- 每一步之前检查暂停与预算(`RunContext.halting`)：暂停时其余步骤都不再执行，预算到达时其余调用模型的步骤不再执行(3.9、3.11)。
- **事件运行**(`RunRequest.events`，由 `tick` 在固定时刻之外发起)只执行 `EVENT_STEPS`：第 2、3、3a、7、8 步。
- `run --select <链> --subject <对象>` 与 `run --select +<模块> --probe <探针>` 按 15.3 的上下游链写法，只执行链上的模块，跳过其余步骤，但第 1、13、14 步照常执行。
- `learn improve`(自我改进的建议)只由用户运行，编排不触发。

### 3.2 时间表

**project.yaml 的 `schedule` 段**

| 键 | 含义 | 例子 |
|---|---|---|
| `tick.weekdays` | launchd 触发的星期(1 为周一) | `[1, 2, 3, 4, 5]` |
| `tick.hours` | launchd 触发的小时；省略表示每小时 | `[7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 22]` |
| `tick.minutes` | launchd 触发的分钟(缺省 `[0, 15, 30, 45]`) | `[0, 30]` |
| `runAt` | 工作日完整运行的时刻(缺省 `["09:00"]`) | `["09:00", "14:00"]` |
| `onDeploy` | 检测到新部署后运行的采集方法与档位 | `[{probe: api-fuzz, level: shallow}, {probe: platform-errors}]` |
| `tasks[]` | 定时任务：`name`、`days`(`workdays`、`daily`、`firstWorkdayOfWeek`)、`at`(本机时区时刻列表)、`command`(子命令与参数) | `{name: static, days: workdays, at: ["08:30", "13:30"], command: "collect --probe static"}` |
| `weekly` | 周任务的时刻 | `{at: "08:30"}` |
| `nonWorkingDays` | 非工作日的日期 | `["2026-10-12"]` |

**到期判断**(`schedule.py`，纯函数，时间由 `Clock` 提供)：

1. 对每个任务，按 `days` 与 `at` 算出「当前时间之前最近的应执行时刻」`due_at`。工作日为周一到周五并排除 `nonWorkingDays`；`firstWorkdayOfWeek` 取本周第一个工作日。
2. 读取 `schedule_state` 中该任务的 `last_started_at`。`last_started_at` 早于 `due_at` 即为到期。
3. 错过多个应执行时刻时只补执行一次，不逐个补跑；补执行时在 `schedule_state` 中记录错过的时刻数，供健康检查判定漏跑。
4. 执行后写入 `schedule_state`：任务名、`last_started_at`、`last_ended_at`、`last_status`、对应的运行编号。

launchd 按 `tick` 唤醒 `tightrein tick`(`Orchestrator.tick`)：暂停时什么都不做；工作日 `runAt` 到期(任务名 `daily-run`，到期判断同上)时做完整运行；其余时刻做事件运行(3.1)，检测到新提交时运行增量巡检，检测到新部署(`deploy-source`)时做部署后确认；接入中的工作区只在固定时刻运行。任务何时真正执行由 `tasks` 决定；`tick` 越密，事件越及时；同一次 run 中多个任务到期时按 `tasks` 的书写顺序执行。

### 3.3 示例项目工作区的到期任务

| 任务 | 触发 | 命令 |
|---|---|---|
| 部署检测与浅跑、新提交的增量巡检 | 每次 tick | 3.1 第 2、3、3a 步 |
| 静态巡检 | 工作日 1 到 2 次(1.6.4) | `collect --probe static`，每周第一个工作日的第一次为全量扫描 |
| 模糊测试深跑 | 每晚一次 | `collect --probe api-fuzz --level deep` |
| 周报 | 每周第一个工作日 | 3.1 第 11 步 |
| 生产发布记录 | 每天一次 | `release track` 中的 7.8 检查 |

### 3.4 续跑

`next` 与 `continue` 实现 15.10。状态到下一步的映射表只在 `domain/next_step.py` 中定义一次，编排层、`cli` 与测试共用，skill 中不复述。

**识别对象**

| 输入 | 处理 |
|---|---|
| 编号(`7`、`0007`、`P-0042`) | 直接取，编号格式按 `domain/ids.py` 解析 |
| 选择器(`status:new`、`run:<运行编号>`) | 按 15.3 解析 |
| 没有编号的描述 | 由 `loop` skill 转成 `tightrein find <文本> [--since <日期>] [--until <日期>] [--type problem\|issue]`；`find` 在问题标题、问题位置、Issue 标题、根因位置中做子串匹配，按时间过滤，返回候选列表与匹配字段 |
| 多个候选 | `find` 只返回候选，不选择；由 skill 列出让用户选，终端中由用户在命令行给出编号 |

相对时间(「昨天」)由 skill 换算为绝对日期后再传给 `find`，核心只接受绝对日期。

**识别目标**：`--until <模块>` 指定终点，终点按 `aggregate`、`triage`、`issue`、`fix`、`verify`、`release` 的顺序比较；省略时做到下一个需要用户的关口。`--from <模块>` 指定从哪一步重来。

**执行过程**(`resume.py`)：

```
对每个对象(按给出的顺序)：
  获取对象锁
  循环：
    读取对象当前状态
    step = next_step(对象, 状态)
    若 step 为终态                   → 停止，原因「无后续步骤」
    若 step 不能自动继续             → 停止，原因为关口类型
    若 step 的模块超过 --until       → 停止，原因「已到终点」
    调用该模块的 service，只处理这一个对象
    结果为 blocked                   → 停止，带回待确认操作或关口说明
    结果为 failed                    → 停止，带回失败原因
    记录一行进度
  释放对象锁
```

- 每完成一步输出一行进度；停止时输出停在哪里、为什么停、下一步命令。
- 交互修复：终端中 `continue` 到 `fix` 时由 `fix start` 启动所配置工具的交互会话，会话结束后若修复的交接文档为 `ok`，继续执行 `fix done` 与后续步骤。由 skill 调用(`--json`)时不再嵌套启动会话，停在关口 `interactive-fix`，给出两种方式：在终端执行 `tightrein fix start <编号>`，或在当前会话中执行 `tightrein fix start <编号> --here` 后按 `fix` skill 工作。
- **从某一步重来**：`--from` 只接受 `triage`、`fix`、`verify`。`triage` 对关联问题执行 `problem retriage`；`fix` 与 `verify` 通过 Issue 状态机的 `restart` 事件把 Issue 退回 `todo` 或合并前验证(`in-progress`，`phase` 为 `verify`)。下游的交接文档在 `handoffs` 中标记 `stale_at`，文件保留不删除，随后按状态表继续。

**next 的输出**：对象、当前状态(中文名)、下一步模块与命令、能否自动继续、不能继续时的关口类型。

### 3.5 中断恢复

命令被中断时先就地收尾，收尾不了的(进程被强杀、断电)由下一次 `run` 与 `continue` 接管。

就地收尾：

- Ctrl+C 是 `KeyboardInterrupt`；`tightrein` 入口把 SIGTERM、SIGHUP 转成同级的 `Terminated`。中断沿调用栈向上抛出，沿途的 `finally`、上下文管理器与事务回滚照常执行；三处子进程启动器(agent 工具、扩展脚本、git 与 gh)在等待期间收到中断时先终止子进程组再抛出，不留下孤儿进程。
- `cli/main` 在关闭数据库前调用 `recovery.close_own`：本进程开始、仍为 `running` 的运行改为 `interrupted`(被中断)或 `failed`(异常，或模块漏了结束运行)，释放本进程持有的全部对象锁，每个运行写一条 `gate` 事件。

下一次接管(`recovery.py`，在每次 `run` 与 `continue` 开始时执行；`watch` 用同一判定把它们显示为已中断)：

1. 找出 `runs` 中状态为 `running` 的记录。每个运行记有开始它的进程号与主机(`holder_pid`、`holder_host`)；没有这两列的旧记录读取 `locks` 中对应的持有者进程号。
2. 进程仍在运行或在其他主机上：不处理(可能是另一个手动会话)。
3. 进程已不存在：把该运行的 `status` 改为 `interrupted`、写入 `ended_at`，接管并释放它持有的对象锁，写一条事件记录接管。
4. 被中断的对象不需要单独补做：各模块只在一步完成时才更新对象状态，对象停留在中断前的状态，下一次 `continue` 或 run 的对应步骤按状态表重新执行这一步；对外操作由幂等键保证不重复(15.7)。
5. 交互修复被中断时 Issue 停在修复阶段(`in-progress`，`phase` 为 `fix`)，按状态表续接修复会话。

### 3.6 运行记录

- 每次 `run` 与 `continue` 产生一条 `stage=loop` 的运行记录，编号 `R-<日期>-<时分秒>-loop`；其中调用的每个模块各自产生运行记录，`parent_run_id` 指向这条记录，`trace_id` 相同。
- 运行记录的 `status` 取 `RunStatus`；`loop` 记录只会是 `running`、`ok`、`failed`、`blocked`、`interrupted` 之一：任一子运行失败时为 `failed`，否则有等待用户的事项时为 `blocked`，否则为 `ok`。
- 编排层自身的事件(步骤触发判断、跳过原因)以 `operation=gate` 写入事件日志，`decision` 记录「执行」或「跳过」，`reason` 记录触发条件的结果。

### 3.7 运行摘要、收件箱与每日汇总

**收件箱**(`inbox.py`，redesign/09-loop.md 第 4 节)：所有需要用户处理的事项集中在一处，每件附推荐做法(`recommendation`)与命令：待确认操作(含撤销 PR、迁移确认)、待确认的修复计划、人工队列、待决定的 Issue(待放行、转人工与熔断)、交互修复、待合并的 PR、改动命中高风险路径的决策简报(`merge-decision.md`，推荐取简报的「推荐与理由」)、CI 必需检查失败(07 篇第 12 章)、接入问题(3.12，推荐答案)、等待部署、待处理的学习建议(`learn-suggestion`，命令 `tightrein learn accept <编号>`；控制措施与改进建议附决定文档，接受只记录决定，配置与提示由用户修改)。处理标签为立即修的置顶，再按严重度排序(design 13.2)；前一个子任务尚未完成、排队中的后续子任务不列。GitHub 镜像中待决定的 Issue 带 `needs-decision` 状态标签(06 篇 10.8)。`run`、`status` 与 loop skill 都从这里读取。

**运行摘要**由 `summary.py` 从本次各子运行的交接文档汇总，写交接文档 `loop-<运行编号>`(JSON，给程序读取：`waiting` 即收件箱、`steps`、`produced`、`unsynced`、`reroutes`、`halted`、`anomalies`)。给人读的是**每日汇总** `data/reports/daily-<日期>.md`(`digest.py`，progress 类型交接文档，运行摘要与收件箱合为一份)，每次运行结束按当天的数据重写(`daily-<日期>.json` 记当天各次运行与历史)：

| 小节 | 内容 |
|---|---|
| 结论 | 待处理几项、今天运行几次、几次有步骤失败 |
| 检查清单 | 收件箱各项(需要用户，`blocked`)，没有时一项「没有待处理的事项」；其后是「接入中的项目」(3.12) |
| 已完成 | 今天各次运行的产出：新发现、回归、已解决、分诊、新建或追加的 Issue、PR、自动决定(自动放行与需要用户决定、自动合并与未自动合并的原因) |
| 卡点 | 暂停与预算到达(`halted`)、未同步到 GitHub(06 篇 10.8)、网络换路(02 篇 4.8)、最近一次运行的异常(失败的模块、健康检查的立即通知项、日志写入失败) |
| 需要决定 | 每件的推荐做法与命令 |
| 下一步、引用、历史 | 每件的命令；今天各次运行的 JSON 交接文档；每次运行一行 |

只执行了部署检测的事件运行(3.2)不重写每日汇总。

**本机通知**由 `observability/notify.py` 发出：

- 方式取本机用户配置的 `notify.method`：`macos` 时调用 `osascript -e 'display notification ...'`，`none` 时不发。
- 每次 run 结束发一条：标题为「tightrein：<工作区>」，正文为结论与待处理事项数，并给出每日汇总的路径。没有任何新产出且没有等待事项的定时运行不发通知。
- P0 问题、部署失败、决策简报、健康检查的立即通知项由各模块单独发出，不等运行摘要。
- 每条通知以幂等键「事件类型 + 对象 + 日期」去重。通知失败不影响运行，失败原因写入「异常」。

### 3.8 审批关卡表

关卡集中定义在配置的 `gates` 段，读取在 `config/gates.py`(vcs、流水线与编排都要读，放在配置层)；模型不能在运行时决定是否请示，也不能绕过。每个关卡取 `user`(交用户)或 `auto`(按规则由程序处理)，缺省都为 `user`：

| 关卡 | `auto` 时 | 接入位置 |
|---|---|---|
| `issue-approve` | 按 `autonomy.approve` 的规则放行新建 Issue，用户需求创建即放行(06 篇 9.3) | `IssueService` |
| `plan-confirm` | B 通道的修复计划按自主确认规则确认(A 通道总是自动，C 通道总是交用户；07 篇 4.7) | `FixService.auto_confirm` |
| `fix-session` | run 以非交互任务推进已放行的 Issue，`continue` 不要求交互会话(07 篇 3.4 第 7 条) | `Orchestrator._fix`、`rules._unattended` |
| `release-writes` | 建分支、提交、合并主干、推送、提 PR、PR 评论、提撤销 PR 生成后直接执行(02 篇 4.5) | `vcs/unattended.release_direct` |
| `mirror-writes` | GitHub Issue 镜像的写入直接执行(06 篇 10.8) | `pipeline/issue/steps/github.py` |
| `merge` | 满足自动合并条件时合并 PR(07 篇 19.6) | `ReleaseService._track_pull` |

下面五项必须交用户，schema 只允许 `user`，`gates.auto` 对它们总是返回假：

| 关卡 | 程序如何保证 |
|---|---|
| `high-risk-merge` | 改动命中 `release.autoMergeBlockPaths` 时不自动合并，写决策简报 |
| `delete` | 删除修复 worktree、分支(`cleanup`)不在直接执行的白名单内，需要两次确认 |
| `permissions-secrets` | 计划改动 `credentialFiles` 或 `review.riskRules.authz` 命中的文件时不自动确认 |
| `over-task-limit` | 超出单个任务上限(`thresholds.change`、超限档)时转待决定(07 篇 4.6、4.9) |
| `needs-decision` | 待决定的 Issue 不被无人值守推进(只取待修与进行中) |

`gates.table` 给出生效的关卡表。等待用户时对象停在关口，不占用资源；批准后按状态表从原处继续。

### 3.9 预算

三层上限(`budget` 段，美元，全部环节合计，缺省都为空即不限)：`perRunUsd`(每次运行)、`perDayUsd`(每天，本机时区)、`perWeekUsd`(每周，从周一开始)；`perWeekUsd` 为空时按 `perWeekPercent`(订阅额度的百分比) × `subscriptionWeekUsd`(订阅额度每周相当的金额)换算。累计读 `budget_usage`(`runner/limits.GlobalBudget`)：

- 执行器启动任务前检查每天与每周的上限(与 `stages.<环节>.budgetPerDay` 一起)，到达时不启动，返回 `limit-reached`、`daily-budget`；
- `run_steps` 在每一步之前检查三层(每次运行的已用为当前总额减去运行开始时的总额 `RunContext.budget_baseline`)，到达后其余会调用模型的步骤(`Step.models`)不再执行，原因记在交接文档 `halted.budget` 并写进每日汇总的「卡点」；无人值守推进在每个对象之前同样检查。

### 3.10 熔断

`breaker.py`：无人值守推进(`Resumer` 的 `observe` 回调)中，同一对象的一步失败计一次失败，执行后状态没有前进且没有停在关口计一次无进展；连续失败达到 `thresholds.loop.breakerFailures`(3)或同一步无进展达到 `breakerRepeats`(3)时停止处理该对象：经 `FixService.hold` 转待决定(`hold` 为「熔断」，GitHub 镜像评论写明原因)，进收件箱。计数存 `breaker_counts`；状态前进即清零，熔断后清零重新计数。停在关口(待确认操作、等待用户、等待部署)不计数。

### 3.11 暂停

`tightrein pause [--note <说明>]` 不带 `--workspace` 时全局暂停(标记文件 `~/.local/state/tightrein/paused`)，带 `--workspace` 时只暂停该工作区(`workspace_meta.paused`)；`tightrein resume` 同样区分。暂停后 `run` 与 `tick` 不发起新的运行(`run` 返回退出码 3，不写运行记录)；进行中的运行在当前步骤完成后停下(`RunContext.halting`)，无人值守推进不再开始下一个对象，原因记在 `halted.pause`。用户当场发起的 `continue` 与各模块命令不受影响。`status` 显示暂停状态(`pause.py`)。

### 3.12 接入

新工作区先处于接入中(redesign/10-onboarding.md)。`orchestrator/onboarding/`：

- **阶段**：`workspace_meta.phase` 为 `onboarding` 或 `running`。迁移 010 给已有数据库写 `running`(已有工作区不强制重新接入)，`project init` 新建时写 `onboarding`。接入中 `run` 只做接入检查与每日汇总(步骤 `onboarding`)，`tick` 只在固定时刻运行；不采集、不修代码、不提 PR、不建 Issue。
- **清单**(`service.Onboarding._evaluate`，按配置动态生成；状态存 `onboarding_items`，渲染为工作区根目录的 `onboarding.md`，progress 类型；每项注明自动完成、需要用户回答(附推荐答案)、失败待处理)：

| 项 | 自动完成 | 需要用户回答(推荐答案) | 失败待处理 |
|---|---|---|---|
| `stack` 识别技术栈 | 按 `onboarding.stackMarkers` 命中的标记文件 | — | 仓库不存在 |
| `checks` 检查命令在基准版本上通过 | 只读 worktree 切到主分支后全量运行 `checks.commands` 通过 | 没有配置检查命令(按 `onboarding.checkCommands` 推荐) | 有命令失败 |
| `conventions` 项目约定 | 工作区或项目中已写明 | `conventions.infer_from_repo` 的推断结果(没有统一风格时推荐通用格式)，确认后写入 `git.conventions` | — |
| `spec` 接口描述(有被测地址时) | `spec-export` 已配置且试运行成功 | 未配置(推荐仓库中找到的 OpenAPI 文件，或不接入) | 试运行失败 |
| `target` 被测地址(有被测地址时) | 健康检查通过 | — | 请求失败 |
| `platform:deploy-source`、`platform:log-source` | 已配置且试运行成功 | 未配置(部署工作流 → `core/github-actions`；`vercel.json` 时说明手动配置的写法；否则不接入) | 试运行失败 |
| `accounts` 测试账号(有被测地址时) | 各角色的钥匙串条目存在(只读账号属性，不读密码) | 未配置(推荐匿名) | 条目不存在 |
| `roles` 角色能力表(配置了账号与 `authz-roles` 时) | 试运行成功 | — | 试运行失败 |

  试运行走 `admin ext run` 的同一路径(`extensions/commands.run_point`)。清单全部完成(回答过的问题算完成)、没有失败项时转为运行中并写历史。
- **回答**：`tightrein project init [--workspace <目录>] [--repo <仓库>]`(目录不存在时新建最小的 `project.yaml`；检查后在终端逐项提问，回车采用推荐，`skip` 跳过，其他输入作为值；非交互时只检查并列出问题)；`tightrein project answer <项> --recommended|--skip|--value <值>`(loop skill 用它把用户的回答写回)；直接改 `project.yaml`，或在 `onboarding.md` 的数据块中把某项改为 `done`，下次检查采纳。写回配置由 `config/edit.py` 按行修改单个两级键，不改动其余内容与注释，核对不一致时不写并提示手动修改。
- **显示**：`status` 显示「<项目>：接入中，还差 N 项需要回答」；每日汇总有「接入中的项目」；收件箱列出各问题与推荐答案。`tightrein project check` 对任何工作区生成一次清单检查，运行中的不改阶段。

## 4. cli

### 4.1 命令树

```
tightrein
  run [--scheduled] [--select <链>] [--subject <对象>] [--probe <探针>]
  tick
  status
  pause [--note <说明>]                    不带 --workspace 为全局
  resume
  workspace init [--repo <仓库>] [--name <名称>] [--main-branch <分支>] [--language <语言>]
  workspace check
  workspace answer <项> --recommended|--skip|--value <值>
  next <对象>
  continue <对象>... [--select <选择器>] [--until <模块>] [--from <模块>]
  find <文本> [--since <日期>] [--until <日期>] [--type problem|issue]
  pending [--stage <模块>]
  confirm <操作编号>
  reject <操作编号> [--note <说明>]

  collect --probe <探针> [--level <档位>] [--reparse <运行编号>] [--import-archive <目录>]
  collect deployments
  aggregate [--rebuild] [--reproduce live|skip] [--no-wait]
  ignore <问题> --reason <原因> [--until <条件>]
  false-positive <问题> --reason <原因> [--expires <日期>]
  merge <问题A> <问题B>
  reopen <问题>
  triage [--limit <数量>]
  triage queue
  retriage <问题> [--note <补充信息>] [--verdict <判定> --reason <原因>]
  issue create
  issue sync
  issue list [--status <状态>] [--severity <等级>]
  issue show <编号>
  issue edit <编号>
  issue approve <编号>
  issue close <编号> --reason <原因> [--note <说明>] [--duplicate-of <编号>]
  issue reopen <编号>
  issue reindex
  fix prepare <编号>
  fix start <编号> [--here] [--force]
  fix plan <编号> [--note <说明>]
  fix confirm <编号> [--reject] [--note <说明>]
  fix apply <编号> [--review-only]
  fix done <编号>
  fix abandon <编号> --reason <原因>
  fix cleanup <编号>
  verify local <编号> [--confirm-migration]
  verify staging <编号>
  verify screenshots <编号> --ok|--issue <说明>
  release <编号>
  release commit <编号> [--accept-findings]
  release sync <编号> [--continue|--abort]
  release push <编号>
  release pr <编号>
  release track
  release revert <编号> --reason <原因>
  release summary <编号>
  learn report|metrics|health|lessons|curate|improve|suggestions|accept|reject      (见 08 分篇第 3 节)

  kb search <关键词> [--type <类型>] [--tags <标签>] [--status <状态>] [--limit <数量>]
  kb get <编号>
  kb related <编号>
  kb stale
  kb sync [--full]
  kb eval [--baseline <评测编号>]
  kb queries [--since <日期>]
  kb mcp
  worktree init
  worktree sync [--commit <commit>]
  worktree list
  ext list
  ext run <扩展点> [--input <文件>] [--commit <commit>]
  ext test [--stack <技术栈>] [--workspace-extensions]
  eval run --module <模块> [--cases <用例>] [--version <commit>] [--worktree] [--runner <工具>] [--model <模型>] [--repeats <次数>]
  eval resume|report <评测编号>
  eval verify [--module <模块>] [--scorers]
  eval add --module <模块> --from <交接文档> --commit <commit>
  eval seal

  install [--tool claude|codex|agy|all | --repo-only] [--check] [--dry-run]
  uninstall [--tool claude|codex|agy|all | --repo-only]
  third-party verify
  third-party lock [<名称>...] [--ref <commit>]
  schedule install
  schedule uninstall
  schedule show
  skills check
  config show [--key <键>]
```

- 流水线模块的命令(`collect` 到 `learn`)接受 15.2 的全部参数；未写出的子命令参数由对应分篇定义。
- 问题的人工操作(`problem ignore`、`problem false-positive`、`merge`、`reopen`、`problem retriage`)是顶层命令，与 2.9、3.9 的写法一致；`reopen` 作用于问题，`issue reopen` 作用于 Issue。
- `admin kb sync` 同步 FTS 索引并重新生成各目录的 `INDEX.md`(16.4)；`kb` 的其余子命令见 03 分篇 1.7，`eval` 的子命令见 03 分篇 2.7。
- `ext` 的三个子命令见 10 分篇第 8 章：查看扩展点的解析结果、单独调用一次扩展点、以夹具测试扩展。
- `project worktree init` 生成创建只读 worktree 的待确认操作；`project worktree sync` 调用 `vcs` 的只读 worktree 切换函数，执行 `git fetch origin` 与 `git checkout --detach <commit>`，不建分支、不提交；省略 `--commit` 时取 staging 当前部署的 commit(02 分篇 4.6)。
- `--runner replay` 可与 `--replay-from <运行编号或目录>` 同用，指定回放的录制集(02 分篇 2.12)。
- `project config` 列出四层配置合成后每个键的生效值与来源层(`core`、`stack:<名称>`、`project`、`user`)；`--key` 只显示该键及其下级键，并列出各层中的值(01 分篇 5.1)。

### 4.2 通用参数

| 参数 | 适用 | 作用 |
|---|---|---|
| `--workspace <路径>` | 全部 | 工作区；省略时取本机用户配置的 `defaultWorkspace` |
| `--json` | 全部 | 以 JSON 输出(4.3)；skill 调用时一律使用 |
| `--now <时间>` | 全部 | 替换当前时间(15.8) |
| `--dry-run` | 全部会产生写入的命令 | 只列出将要做什么(15.5) |
| `--select <选择器>` | 流水线模块、`continue` | 15.3 |
| `--input <交接文档>` | 流水线模块 | 15.4 |
| `--output <目录>` | 流水线模块 | 15.5 |
| `--ignore-state` | 流水线模块，只在 `--output` 下有效 | 跳过前置条件中的状态检查(15.6) |
| `--runner <工具>`、`--model <模型>` | 会调用执行器的命令 | 覆盖 `project.yaml`；`--runner replay` 回放录制结果 |
| `--target <地址>`、`--commit <commit>` | `collect`、`verify`、`triage` | 15.8 |
| `--verbose` | 全部 | 人读输出中附带事件日志中的步骤明细 |

`--ignore-state` 在没有 `--output` 时直接报用法错误，保证正常模式下不能跳过状态检查。

### 4.3 输出

- **人读输出**：第一行是结论；随后是明细；停止时最后一行是下一步命令。
- **JSON 输出**：只向标准输出写一个 JSON 对象，日志与进度写到标准错误：

| 字段 | 说明 |
|---|---|
| `command` | 执行的命令 |
| `status` | `ok`、`blocked`、`failed` |
| `exitCode` | 与进程退出码相同 |
| `subject` | 处理对象 |
| `result` | 命令特有的结果，结构与对应交接文档的 `outputs` 一致 |
| `stoppedAt` | 续跑停下的位置：对象、状态、关口类型 |
| `next` | 建议的下一条命令 |
| `pendingOperations` | 待确认操作的编号、说明与命令 |
| `errors` | 错误列表：类型、说明、处理建议 |

### 4.4 待确认操作

`vcs` 的写操作与各模块的确认事项不直接执行，而是生成「待确认操作」，写入 `pending_operations` 表，编号 `OP-<四位序号>`(01 篇 4.2)：

- **终端中**：`approve.py` 当场展示将执行的命令、作用的分支与文件、对工作区与历史的影响、是否影响远程、能否撤销，等待输入 `yes`；同意只对这一次操作有效。
- **非交互调用**(标准输入不是终端或使用 `--json`)：命令以退出码 4 结束，`pendingOperations` 中给出操作；用户同意后由调用方执行 `tightrein approve <操作编号>`，拒绝时执行 `tightrein reject <操作编号> [--note <说明>]`。
- `approve` 执行前重新检查前置条件(分支、工作区状态)与幂等键。
- 待确认操作超过 7 天未处理标为 `expired`，需要时重新运行对应命令生成。

### 4.5 退出码

| 退出码 | 含义 | 典型情况 |
|---|---|---|
| 0 | 成功 | 命令完成；`run` 中没有模块失败(存在等待用户的事项也返回 0) |
| 1 | 执行失败 | 模块返回 `failed`、外部依赖失败、内部错误 |
| 2 | 用法错误 | 参数或选择器不合法、`project.yaml` 或用户配置校验失败 |
| 3 | 前置条件不满足 | 15.6 的前置条件，输出中给出应先执行的命令 |
| 4 | 停在需要用户的关口 | 待确认操作、待放行、待确认修复计划、交互修复、候选对象需要用户选择 |
| 5 | 达到上限 | 预算、轮数或时间上限 |
| 6 | 边界违规 | `guards` 判定越界 |
| 7 | 锁被占用 | 运行锁或对象锁被其他进程持有 |
| 8 | 输出不合格 | 执行器结果重试后仍不符合 schema |
| 130 | 被中断 | Ctrl+C |
| 128 + 信号编号 | 被终止 | SIGTERM(143)、SIGHUP(129) |

`exit_codes.py` 把各层抛出的具名异常映射为退出码；未映射的异常一律为 1，并在输出中给出事件日志路径。

## 5. skills

### 5.1 统一写法

| 约定 | 内容 |
|---|---|
| 标准 | Agent Skills 标准：每个 skill 一个目录，目录名即 skill 名，内含 `SKILL.md`，可选 `references/` |
| frontmatter | 只写 `name` 与 `description`。`name` 与目录名相同，小写字母、数字与连字符；`description` 写清做什么、何时使用，包含用户常用的中文说法，不超过 1024 个字符 |
| 正文长度 | `SKILL.md` 正文不超过 500 行 |
| references | 只从 `SKILL.md` 直接引用，参考文件之间不互相引用；超过 100 行的参考文件顶部加目录；`references/roles/`、`references/tasks/` 下的角色与任务说明由核心作为执行器任务的 `instructions` 加载，不要求被 `SKILL.md` 引用；其余参考文件(包括同样由核心加载的单个文件，例如 `evidence-standard.md`)都须在 `SKILL.md` 的参考资料清单中列出；`admin skills check` 检查这两条 |
| 内容边界 | 只写：何时使用、该调用哪个命令与参数、如何解读 `--json` 输出与退出码、在哪些关口停下等用户、禁止事项。不写业务规则表、阈值、状态表，这些由核心维护，skill 通过命令的输出获得 |
| 命令调用 | 一律带 `--json`；不直接读写工作区中的数据库、交接文档与配置；不执行任何 git 写操作 |
| 确认 | 退出码 4 时把 `pendingOperations` 的说明原样展示给用户，得到用户明确同意后才执行 `approve`；用户的同意不能由 agent 推断 |
| 语言 | 正文用中文 |

`tightrein admin skills check` 机械检查以上约定：frontmatter 只有两个字段且 `name` 与目录名一致、正文行数、`references/` 中的文件都被 `SKILL.md` 引用且没有互相引用、长参考文件有目录、正文与参考文件中出现的 `tightrein <命令>` 都存在于命令树中。该检查在单元测试与 `admin install` 前都会运行。

### 5.2 loop skill

**frontmatter**

- `name: loop`
- `description`：说明它用于推进 tightrein 的缺陷闭环：查看全局状态与待处理事项、按问题或 Issue 的当前状态继续到下一个关口、从某一步重来；并列出典型说法，例如「继续修 7 号」「那个材料查询 500 的问题现在怎么样了」「把昨天发现的问题都处理一下」「7 号一直做到提 PR」「现在有什么要我处理的」。

**正文要点**

| 节 | 内容 |
|---|---|
| 总则 | 进度就是状态，永远先读状态再行动；判断下一步只看命令输出，不自行推断；所有命令加 `--json` |
| 看全局 | 用户问整体情况时执行 `tightrein status --json`，先说暂停与接入状态，再按收件箱各项(附推荐做法)、异常、产出的顺序汇报 |
| 接入问题 | 接入中的工作区执行 `tightrein project check --json` 列出待回答的问题与推荐答案；用户用自然语言回答后以 `tightrein project answer <项> --recommended\|--skip\|--value <值>` 写回 |
| 暂停 | 用户要求暂停或恢复时执行 `tightrein pause`、`tightrein resume`(只对一个工作区时带 `--workspace`) |
| 识别对象 | 有编号直接用；没有编号时执行 `tightrein find`，把相对时间换算为绝对日期；多个候选时列出编号、标题、状态让用户选，不猜 |
| 识别目标 | 用户说了做到哪一步的换成 `--until`；只问进度的用 `next`；说「从某步重来」的用 `--from` |
| 执行 | 执行 `tightrein continue <对象> [--until] [--from]`；每步完成后用一句话汇报；停下时说明停在哪里、为什么停、下一步是什么 |
| 关口 | 按退出码与 `stoppedAt` 的关口类型处理：待确认操作原样展示，等用户同意后 `approve`；待放行的 Issue 展示 Issue 摘要，由用户决定是否 `approve`；修复计划由 `fix` skill 负责确认；交互修复给出两种方式由用户选；待合并的 PR 只给链接 |
| 失败 | 退出码 1、5、6、8 时汇报 `errors` 中的说明与事件日志路径，不重试、不换参数绕过；退出码 3 时按输出执行前置命令前先告诉用户；退出码 7 时告诉用户有其他运行正在进行 |
| 禁止 | 不执行 git 写操作；不编辑工作区中的文件；不替用户确认；不跳过关口；不用 `--ignore-state` |
| 参考资料 | `references/commands.md`、`references/gates.md`、`references/exit-codes.md`、`references/phrasing.md` |

**参考资料要点**

| 文件 | 内容 |
|---|---|
| `commands.md` | 意图到命令的对照：全局状态、单个对象进度、继续、重来、查找、待确认操作的查看与确认、各模块的单独运行 |
| `gates.md` | 每种关口类型(`pending-operation`、`issue-approval`、`fix-plan`、`interactive-fix`、`pr-review`、`manual-queue`、`candidate-choice`、`awaiting-deploy`)：向用户展示什么、用户同意或给出选择后执行什么命令 |
| `exit-codes.md` | 4.5 的退出码与 skill 的处理方式 |
| `phrasing.md` | 常见说法到对象、目标参数的对照，以及需要反问用户的模糊说法 |

### 5.3 其他 skill 的共同结构

每个模块的 `SKILL.md` 包含相同的几节：用途与何时使用、命令(单独运行的写法与常用参数)、输出解读、关口与禁止事项、参考资料清单。模块特有的内容在各自的分篇中定义；`learn` 见 08 分篇第 2.3 节。

## 6. packaging

### 6.1 各工具的安装位置

| 工具 | 读取 skill 的位置 | 本工具的安装方式 | 调用名 |
|---|---|---|---|
| Claude Code | 个人 `~/.claude/skills/<名称>/SKILL.md`、项目 `.claude/skills/`、插件 `<插件>/skills/<名称>/SKILL.md` | 构建为本地插件 `tightrein`，经本地插件市场安装到用户范围 | `/tightrein:loop` 等，带插件名前缀，不与 Claude Code 自带的同名 skill 冲突 |
| Codex CLI | 用户级 `~/.agents/skills`；仓库内从当前目录到仓库根目录的各级 `.agents/skills`；系统级 `/etc/codex/skills`；支持链接目录 | 在 `~/.agents/skills/<名称>` 建立指向 `skills/<名称>` 的链接 | `loop` 等 |
| Antigravity CLI(agy) | 用户级 `~/.agents/skills` | 与 Codex CLI 共用 `~/.agents/skills` 下的链接，同一路径只建一次 | `loop` 等 |

各工具的安装路径都可以在本机用户配置的 `install.targets.<工具>.path` 中改写；安装脚本按配置的路径安装，未配置时使用上表的默认位置。

### 6.2 Claude Code 插件

`packaging/claude/target.py` 构建以下目录：

```
~/.cache/tightrein/packaging/claude/
  .claude-plugin/marketplace.json       本地插件市场，名称 tightrein-local，列出插件 tightrein，source 为 ./tightrein
  tightrein/
    .claude-plugin/plugin.json          name、version、description
    skills/
      loop/ collect/ ... learn/         从 skills/ 复制
      differential-review/ ... sharp-edges/     锁定清单中的第三方 skill，从 third_party 的缓存复制
```

- 插件目录中的 skill 用复制而不是链接，因为 Claude Code 安装插件时会把插件复制到自己的缓存目录。
- `version` 为本工具版本号加 skills 与第三方 skill 内容哈希的前 8 位，内容变化时版本随之变化。
- 首次安装执行 `claude plugin marketplace add <构建目录>` 与 `claude plugin install tightrein@tightrein-local`；更新时执行 `claude plugin marketplace update tightrein-local` 与 `claude plugin update tightrein@tightrein-local`；卸载执行 `claude plugin uninstall tightrein@tightrein-local` 与 `claude plugin marketplace remove tightrein-local`。
- 安装后用 `claude plugin list` 核对插件与版本。

### 6.3 安装流程

`tightrein admin install [--tool <工具>]` 调用 `packaging/install.py`：

1. 运行 `admin skills check`，不通过则停止。
2. 运行 `admin third-party verify`，缓存缺失的按锁定清单下载，哈希不符则停止。
3. 对每个目标工具检查冲突：目标位置已存在同名条目、且不是本工具上次安装的(不在 `installed.json` 中)时停止，列出冲突路径，由用户处理后重跑；不覆盖他人的 skill。
4. 按 6.1 安装。链接方式下链接指向仓库中的 `skills/<名称>` 与第三方 skill 的缓存目录，修改 skill 后无需重装；插件方式下每次 skills 变化后重新执行 `admin install`。
5. 核对：每个安装位置都能读到 `SKILL.md`，其 `name` 与预期一致，内容哈希与来源一致。
6. 写 `third_party/installed.json`：工具、安装路径、方式、各 skill 的内容哈希、安装时间。

- `--dry-run` 只列出将要创建、更新与删除的路径与命令。
- `admin install --check` 只做第 5 步的核对，报告缺失、过期(哈希与仓库不一致)与冲突的条目。
- `admin uninstall` 只删除 `installed.json` 中记录的、由本工具创建的链接与插件，删除前列出清单并确认。
- `--repo-only`(与 `--tool` 互斥)只做本工具仓库内的部分：第 1、2 步，在 `skills/<名称>` 建立指向缓存的链接，写 `installed.json` 的 `repo` 段；不构建插件、不执行 `claude plugin`、不碰任何工具目录。`--dry-run`、`--check`(只核对 `skills/<名称>` 的链接，工具名记为 `repo`)同样适用。`admin uninstall --repo-only` 只删除 `repo` 段记录的链接，仍有工具的安装记录时停止(这些工具经 `skills/<名称>` 取得第三方 skill)；下载缓存保留。
- 各次核对都包含已锁定的第三方 skill 在 `skills/<名称>` 的链接。

## 7. third_party

### 7.1 锁定清单

`third_party/skills.lock.yaml`：

| 字段 | 说明 |
|---|---|
| `lockVersion` | 清单格式版本 |
| `skills[].name` | skill 名称，与其 `SKILL.md` 的 `name` 一致，例如 `differential-review`、`variant-analysis` |
| `skills[].source` | 来源仓库地址 |
| `skills[].ref` | 完整的 40 位 commit，不使用分支或标签 |
| `skills[].path` | skill 目录在来源仓库中的路径 |
| `skills[].license` | 来源仓库声明的许可证 |
| `skills[].treeHash` | 整个 skill 目录的哈希，`sha256:` 前缀 |
| `skills[].files[]` | 每个文件的相对路径与 `sha256` |
| `skills[].lockedAt` | 锁定日期 |

### 7.2 哈希与校验

- **目录哈希**：对 skill 目录中的全部文件(不含 `.git`)，按相对路径排序，逐行拼接「相对路径、一个制表符、文件内容的 sha256、换行」，再对拼接结果求 sha256。不计文件权限与修改时间，结果与平台无关。
- **下载**：按 `source` 与 `ref` 下载该 commit 的源码归档(GitHub 的 `https://codeload.github.com/<所有者>/<仓库>/tar.gz/<commit>`)，只解出 `path` 下的目录，放到本工具仓库内的缓存 `local/third_party-cache/<名称>/<commit>/`(`local/` 已被 `.gitignore` 忽略)。不在缓存中执行任何 git 命令。
- **校验时机**：`admin install` 前；`collect` 的静态巡检启动执行器前，由 `packaging/common.py` 提供的校验函数核对已安装副本；`admin third-party verify` 手动执行。任一文件哈希不符即停止，报出文件路径与期望哈希、实际哈希，不自动重新下载覆盖。
- **锁定与更新**：`admin third-party lock [<名称>...] [--ref <commit>]`：没有 `--ref` 时取来源仓库默认分支的最新 commit，按 design 9.11 核实后下载，写入 commit、路径、许可证、逐文件哈希与核实数据；改写已锁定的条目时列出与当前锁定版本的文件差异，用户确认后才改写；清单的提交由用户自行完成。

## 8. launchd 定时配置

### 8.1 放置位置与加载

- 文件：`~/Library/LaunchAgents/local.tightrein.<项目名>.plist`，用户级 LaunchAgent，在用户登录的图形会话域中运行，可以读取登录钥匙串中的测试账号密码。
- `tightrein project schedule install` 由 `schedule.tick`(`project.yaml` 覆盖核心缺省值)生成 plist，展示全文与将执行的命令，确认后写入并执行 `launchctl bootstrap gui/<用户 ID> <plist 路径>`。
- `project schedule uninstall` 执行 `launchctl bootout gui/<用户 ID>/local.tightrein.<项目名>` 后删除 plist；`project schedule show` 输出 plist 与 `launchctl print gui/<用户 ID>/local.tightrein.<项目名>` 的状态。
- 修改 `schedule.tick` 后重新执行 `project schedule install`，先 `bootout` 再 `bootstrap`。
- 手动立即触发一次：`launchctl kickstart gui/<用户 ID>/local.tightrein.<项目名>`。

### 8.2 plist 内容要点

| 键 | 取值 | 说明 |
|---|---|---|
| `Label` | `local.tightrein.<项目名>` | 与文件名一致 |
| `ProgramArguments` | `[<虚拟环境>/bin/tightrein, tick, --workspace, <工作区绝对路径>]`(固定时刻完整运行，其余时刻只检查事件，3.2) | 全部使用绝对路径；launchd 不经过 shell，不展开 `~` |
| `StartCalendarInterval` | 字典数组，每个字典含 `Weekday`、`Minute`，给出 `tick.hours` 时再含 `Hour` | 由 `tick` 展开；省略的键视为通配 |
| `WorkingDirectory` | 本工具仓库的绝对路径 | — |
| `EnvironmentVariables` | `PATH`(包含 git、gh、node、所用技术栈扩展声明的外部工具与各 agent 工具所在目录)、`LANG` | 不放任何凭证 |
| `StandardOutPath`、`StandardErrorPath` | `<工作区>/data/logs/launchd.out.log`、`launchd.err.log` | 由保留期清理按大小截断 |
| `RunAtLoad` | `false` | 加载时不立即运行 |
| `ProcessType` | `Standard` | 采集与分诊需要正常的资源配额 |

- `StartCalendarInterval` 与 cron 不同：电脑休眠时错过的触发会在唤醒后执行一次。时间表的到期判断(3.2)按「最近的应执行时刻」补执行一次，与之配合；关机期间错过的由健康检查的漏跑判定报告。
- 同一时刻只有一个 run：launchd 本身不会并发启动同一个任务，手动 `run` 与定时 run 之间由运行锁互斥。

## 9. 错误处理

| 情况 | 处理 |
|---|---|
| `project.yaml` 或用户配置校验失败 | 报出完整键名，退出码 2；定时运行同时发通知，因为此时所有任务都无法执行 |
| run 中某个模块失败 | 记录该子运行为 `failed`，继续后续步骤；运行摘要的「异常」一节列出原因与日志路径 |
| 运行锁被占用 | 定时运行退出码 7 并记录跳过；手动运行提示正在进行的运行编号 |
| 对象锁被占用 | `continue` 跳过该对象并在输出中说明，其余对象继续 |
| 状态表中没有该状态的映射 | 视为程序错误，停止并报出对象与状态，不猜测下一步 |
| `find` 没有候选 | 返回空列表，skill 请用户换一种描述或给出编号 |
| 待确认操作执行时前置条件已变化 | 标为 `expired`，提示重新运行生成它的命令 |
| 事件日志写入失败 | 不中断，运行摘要中报告 |
| 通知失败 | 不中断，运行摘要中报告 |
| 安装冲突、哈希不符、`admin skills check` 不通过 | 停止安装，列出全部问题，不做部分安装 |
| `launchctl` 命令失败 | 输出命令与错误原文，plist 文件保留，便于手动排查 |

## 10. 测试

| 对象 | 测试方式 |
|---|---|
| `schedule.py` | 固定 `Clock`：工作日与非工作日、`firstWorkdayOfWeek` 遇到节假日、错过多个时刻只补一次、跨午夜与跨周 |
| `rules.py` | 构造临时数据库中的各种状态，断言每一步是否触发；某一步抛错时后续步骤仍执行 |
| `resume.py` | 用 `domain/next_step.py` 的映射表逐项构造对象：自动继续到关口、`--until` 截止、`--from` 的三种重来、结果为 `blocked` 与 `failed` 时停止；模块 service 用替身 |
| `recovery.py` | 持有者进程不存在时接管锁并标记 `interrupted`；进程存在时不处理 |
| `summary.py`、`notify.py` | 给定子运行的交接文档断言摘要各节；通知幂等去重；`notify.method=none` 时不调用外部命令 |
| `cli` | 每个命令的参数解析；`--ignore-state` 无 `--output` 时报错；`--json` 时标准输出只有一个合法 JSON；每类异常到退出码的映射；非交互调用时待确认操作以退出码 4 返回 |
| `approve` | 前置条件变化时转为 `expired`；幂等键已完成时跳过 |
| `admin skills check` | 对仓库中的全部 skill 运行，作为单元测试的一部分；另备违规样例：多余的 frontmatter 字段、名称与目录不一致、超过 500 行、参考文件互相引用、引用不存在的命令 |
| `packaging` | 在临时 HOME 中安装：链接指向正确、冲突时停止、`--dry-run` 无副作用、`admin install --check` 报告过期条目、`admin uninstall` 只删除自己创建的条目；Claude Code 插件只断言构建出的目录与清单内容，不在测试中调用 `claude` |
| `third_party` | 目录哈希的确定性(文件顺序、权限变化不影响结果)；文件被改动一个字节时校验失败 |
| launchd | 断言由 `tick` 展开的 `StartCalendarInterval` 与 plist 其余键；不在测试中调用 `launchctl` |
| 端到端 | `tests/replay/` 中准备一次完整 run 的夹具：数据库快照、录制的 agent 结果、期望的运行摘要，以 `--runner replay --now` 运行并比对 |

本篇用到的基础层定义(编号、枚举、表、路径、配置)统一见 01-foundation.md。
