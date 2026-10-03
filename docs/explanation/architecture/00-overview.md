# 总体架构

本文描述 tightrein 的整体结构：分几层、有哪些组件、组件之间怎样依赖、代码放在哪个目录。各组件的内部设计在本目录后续的分篇文档中展开；业务规则(每个模块做什么、判断标准是什么)在 `docs/explanation/design/` 中。

## 1. 设计目标

| 目标 | 架构上的对应 |
|---|---|
| 与 agent 工具无关 | 核心是独立的 Python 程序；LLM 调用只经过 agent 执行器，工具差异封装在适配器中 |
| 与被测项目无关 | 核心不含任何项目知识；项目相关的一切放在工作区，由 `project.yaml` 描述 |
| 每一步都能单独运行 | 每个流水线模块是一个纯任务：输入输出明确、可重跑、只通过存储交接 |
| 结果可靠 | 业务状态由确定性代码维护；LLM 只产出经过 schema 校验的结构化结果 |
| 安全 | 边界由核心在工具之外再检查一遍；写操作集中在少数模块，逐次确认 |
| 可观测、可改进 | 每一步都产生统一格式的事件日志；改进只以建议给出：依据出问题的真实记录，附评测对比，由用户决定是否采纳 |

## 2. 分层

```
┌──────────────────────────────────────────────────────────────┐
│ 入口层      cli(终端命令)      skills(Agent Skills，供 agent 工具调用) │
├──────────────────────────────────────────────────────────────┤
│ 编排层      orchestrator：状态表、时间表、续跑、锁                    │
├──────────────────────────────────────────────────────────────┤
│ 流水线层    collect  aggregate  triage  issue  fix  verify         │
│             release  learn  improve                               │
├──────────────────────────────────────────────────────────────┤
│ 能力层      probes   runner(执行器与适配器)   guards(边界检查)       │
│             retrieval(知识检索)   evaluation(评测)   vcs(git 与 gh)  │
│             extensions(扩展点宿主：调用技术栈扩展与项目扩展)          │
├──────────────────────────────────────────────────────────────┤
│ 基础层      domain(实体与状态机)   contracts(schema 与校验)          │
│             store(数据库与文件)   config   observability(事件日志)   │
└──────────────────────────────────────────────────────────────┘
外部：被测项目仓库、staging、agent 工具(Claude Code / Codex CLI / Antigravity CLI)、GitHub
```

**依赖规则**

1. 只能依赖同层或下层，不能依赖上层。例外：`orchestrator/policy/`(自主决定 `autonomy.py`、修复流程表 `lanes.py`)是编排拥有的关卡规则，只读配置、只依赖 `config` 与 `domain`，流水线模块可以调用。
2. 流水线模块之间不互相调用，只通过 store 中的交接文档与数据库记录衔接。
3. `domain` 不做任何 IO：实体、状态机、指纹与评分计算都是纯函数，可以不依赖任何外部环境单独测试。
4. 调用 LLM 只能经过 `runner`；执行 git 写操作只能经过 `vcs`，并且只有 `fix`(建分支与 worktree)与 `release` 两个模块可以调用写操作。`vcs` 的写操作一律先生成待确认操作，用户确认后才执行。只读 worktree 的首次创建同样是待确认操作；此后把它切换到目标 commit 由 `vcs` 的专用函数执行，只移动游离 HEAD，不建分支、不提交，编排层、`collect` 与 `triage` 可以调用。
5. 入口层只做参数解析与展示，不含业务逻辑；`skills` 只告诉 agent 该调用哪个命令、如何解读结果。

## 3. 组件

### 3.1 基础层

| 组件 | 职责 | 不负责 |
|---|---|---|
| `domain` | 实体(信号、问题、分诊结论、Issue、运行记录)、状态机(问题 8 种状态、Issue 状态)、指纹计算、规模档与处理标签的判定、状态到下一步的映射表 | 读写文件与数据库、调用外部程序 |
| `contracts` | 交接文档的统一外层与各模块 `outputs` 的 JSON schema；校验；schema 版本迁移 | 业务判断 |
| `store` | SQLite 仓储(按实体分表)；文件仓储(交接文档、人读文档、Issue markdown、中间过程文档)；对象锁；幂等键 | 业务判断 |
| `config` | 按四层(核心默认值、技术栈默认值、`project.yaml`、本机用户配置)合成并校验配置，记录每个键的来源层；模型档到具体工具、模型与推理强度的映射 | 在代码中给可调的值写默认值 |
| `observability` | 按统一格式写事件日志(trace、span、用量、决定与理由、评分)；脱敏；本机通知与去重(`notify.py`) | 统计指标(由 `learn` 计算) |

### 3.2 能力层

| 组件 | 职责 | 对外接口 |
|---|---|---|
| `runner` | agent 执行器：统一任务与结果格式；适配器 `claude`、`codex`、`agy`、`replay`(回放录制结果)；会话记录转换为统一事件格式；超时与预算 | `run(task) -> result` |
| `guards` | 工具之外的第二层边界检查：只读 worktree 设为不可写、运行后检查改动范围与受保护文件、比较运行前后的 git 状态、清除进程中的凭证、diff 规则 | `before(task)`、`after(task, result)` |
| `probes` | 各探针的具体执行：Schemathesis、Playwright、Semgrep 的通用调用，经扩展点取得接口描述、权限数据、服务端日志、技术栈工具结果与页面路由，任务外发现的读取 | 每个探针 `run(target, level) -> signals` |
| `retrieval` | 项目知识、经验库、历史问题与 Issue 的检索：索引维护、按字段过滤、全文检索，为 agent 任务组装上下文 | `search(query, filters)`、`context_for(task)` |
| `evaluation` | 评测运行器：在 `--output` 沙箱模式下对评测集运行模块，按条目表打分，比较不同版本、工具与模型 | `evaluate(module, version, runner)` |
| `vcs` | git 与 gh 的封装：只读查询(状态、日志、blame、部署记录、PR 状态)、只读 worktree 的切换，与写操作(建分支、提交、推送、提 PR)；写操作前生成说明，等待确认 | 只读函数；写操作返回「待确认操作」 |
| `extensions` | 扩展点宿主：按「项目扩展、技术栈扩展、核心默认」解析实现，以子进程调用扩展并校验 JSON 输入输出，按 commit 缓存输出，扩展缺失时给出核心默认行为 | 每个扩展点一个函数，例如 `spec_export(repo, commit)`、`local_run(worktree, mode, ports)` |

### 3.3 流水线层

每个模块是一个纯任务，内部结构一致：

```
pipeline/<模块>/
  service     入口：解析选择器、检查前置条件、逐个对象处理
  steps       模块内的各个步骤，每步一个函数或类
  prompts     需要 LLM 的步骤：组装执行器任务(不含提示正文，正文在 skills 的 references 中)
  render      由交接文档生成人读文档
```

| 模块 | 读取 | 写入 | 用到的能力 |
|---|---|---|---|
| `collect` | 目标环境、探针配置 | 信号 | probes(经 extensions 取得技术栈与项目数据)、runner(静态巡检)、retrieval(静态巡检预取)、vcs(只读) |
| `aggregate` | 信号 | 问题、关联、状态变化 | probes(只用 api_fuzz 的请求重放)、vcs(只读) |
| `triage` | 问题 | 分诊结论、发现报告 | runner、retrieval、evaluation(评分)、vcs(只读) |
| `issue` | 分诊结论 | Issue | evaluation(评分) |
| `fix` | Issue、发现报告 | worktree 中的改动、修复报告 | runner、guards、retrieval、evaluation(评分)、vcs(只建分支与 worktree) |
| `verify` | Issue、复现检查 | 验证报告 | probes(复用 api-fuzz 与 e2e 的执行部分)、extensions(`local-run`、`authz-endpoints`、`page-routes`)、runner(仅截图查看) |
| `release` | Issue、验证报告 | 提交、PR、部署跟踪 | vcs |
| `learn` | 数据库、事件日志 | 指标、周报、经验、规则库、学习建议 | retrieval、runner |
| `improve` | 出问题的来源(由 `learn` 汇总)、评测集 | 改进建议(decision 文档与补丁)、评测报告；只由 `tightrein learn improve` 运行 | runner、evaluation |

### 3.4 编排层

| 组件 | 职责 |
|---|---|
| 状态表 | 对象的当前状态到下一步模块的映射(`domain` 中定义，编排层使用) |
| 时间表 | 按 `project.yaml` 判断哪些任务到期 |
| 续跑 | 识别对象与目标，从当前状态继续到终点或下一个关口 |
| 运行记录 | 每次运行一个 run，记录开始、结束、覆盖范围与结果 |

编排层只决定「接下来调用哪个模块」，不做任何模块内部的判断。

### 3.5 入口层

| 入口 | 形式 | 用途 |
|---|---|---|
| `cli` | `tightrein <命令>` | 终端使用、launchd 定时调用、skill 调用 |
| `skills` | Agent Skills 标准格式的 `SKILL.md` 与 `references/` | 在任意 agent 工具中用自然语言使用；需要 LLM 的步骤中作为角色说明 |

### 3.6 通用与专属的划分

tightrein 要能接入不同的项目，因此每一段逻辑都按「是否通用」分到三层之一：

| 层 | 放什么 | 位置 | 例子 |
|---|---|---|---|
| 核心 | 与项目、技术栈都无关的逻辑，以及各类扩展点的接口定义 | `core/` | 聚合、分诊、状态机、执行器、边界检查、Schemathesis 与 Playwright 的调用框架 |
| 技术栈扩展 | 同一技术栈的项目都能复用的解析器与工具 | `extensions/stacks/<技术栈>/` | ASP.NET Core：导出接口描述、读取端点的权限声明、解析 .NET 控制台日志、构建告警与依赖漏洞检查 |
| 项目扩展 | 只属于某一个项目的内容 | `workspaces/<项目>/` | 角色能力矩阵的解析脚本、规范化规则、巡检用例、知识库、`project.yaml` |

**规则**

1. 核心代码中不出现任何项目名、技术栈名、框架特有的文件格式或类名。需要这些知识的地方，核心只定义扩展点，由扩展提供实现。
2. 扩展点统一为「外部命令 + JSON 输入输出」：核心以子进程启动扩展，通过标准输入传入 JSON，从标准输出读取 JSON，按 `contracts` 中该扩展点的 schema 校验。扩展因此可以用任何语言实现(例如 C# 写的权限读取工具)，也可以单独测试。
3. 只能按项目决定的部分，外部为每个扩展点提供「方法目录」：有哪些方法、每种方法的实现与参数。项目在 `project.yaml` 中只写选用哪种方法与参数；目录中没有适用的方法时，才在项目扩展中写自定义实现(10-extensions 1.4)。
4. 一段逻辑先放在项目扩展里；当第二个同技术栈的项目也需要它时，再提升为技术栈扩展。不提前抽象。
5. 设计文档与分篇中以示例项目为例的内容，一律标明「示例项目中的取值」或放在工作区章节，不作为核心行为描述。
6. 可调的值一律写在配置文件中，不写死在代码里：阈值、权重、系数、超时、轮询间隔、轮数与预算上限、保留期、输出截断长度、风险判定规则、评审触发条件、各角色的模型档与推理强度。配置分四层，后一层覆盖前一层：核心默认值 `core/tightrein/config/defaults.yaml`(全部键、默认值与注释，经 schema 校验)、技术栈默认值 `extensions/stacks/<技术栈>/defaults.yaml`、工作区的 `project.yaml`、本机用户配置 `~/.config/tightrein/config.yaml`(只允许个人键)。`tightrein config show [--key <键>]` 列出每个键的生效值与来源层。单位换算、协议与外部工具规定的值不属于可调项(01 篇 5.1)。

**扩展点清单**

| 扩展点 | 输入 | 输出 | 使用者 |
|---|---|---|---|
| `spec-export` | 仓库路径、commit | 接口描述(OpenAPI) | api-fuzz |
| `authz-endpoints` | 仓库路径、commit | 端点到所需权限的映射 | api-fuzz 的越权检查 |
| `authz-roles` | 仓库路径、commit | 角色到能力的映射 | api-fuzz 的越权检查 |
| `error-tracking` | 时间窗口 | 窗口内有新事件的错误分组(平台分组编号、堆栈、操作轨迹) | 内部错误 |
| `log-platform` | 查询语句与时间窗口 | 日志原文片段 | 内部错误、访问日志、项目探针 |
| `log-parse` | 日志原文片段 | 结构化日志条目(时间、级别、类别、异常类型、本项目堆栈帧、消息) | 内部错误、项目探针 |
| `alert-source` | 无 | 已触发、未静默的告警 | 业务告警 |
| `static-tools` | 仓库路径、commit、改动文件 | 确定性工具的发现(构建告警、依赖漏洞、规则命中) | static |
| `page-routes` | 仓库路径、commit | 前端页面路由清单 | e2e 的页面覆盖率 |
| `local-run` | worktree 路径、端口 | 本机启动与停止服务的命令、就绪与失败信号 | verify |

各扩展点的输入输出 schema 放在 `contracts/schemas/extension/points/`。调用机制、完整字段、没有扩展时的核心默认行为，以及 `aspnetcore` 技术栈扩展与示例项目的项目扩展的实现要点，见 `10-extensions.md`。

## 4. 数据流

```
staging / 仓库
    │ collect
    ▼
 信号 ──aggregate──▶ 问题 ──triage──▶ 分诊结论 ──issue──▶ Issue
                                                         │ 用户放行
                                                         ▼
                                             fix ──▶ worktree 改动
                                                         │ verify local
                                                         ▼
                                             release ──▶ PR ──用户合并──▶ 部署
                                                                          │ verify staging
                                                                          ▼
                                                                     Issue 已修复
事件日志、数据库 ──learn──▶ 指标、周报、经验与规则库、学习建议
出问题的来源、评测集 ──learn improve──▶ 改进建议 ──用户批准后自己应用──▶ skills 或模型档的修改
```

每个箭头都是一次「读取上游交接文档 → 处理 → 写入本模块交接文档」，中间不传递任何内存状态。

## 5. 目录结构

```
tightrein/
  core/                          核心程序(Python 包 tightrein)
    tightrein/
      domain/                    实体、状态机、指纹、规模档与任务复杂度、状态表；handoff/ 为交接文档的类型注册与小节操作
      contracts/
        schemas/                 JSON schema：handoff/(交接文档)、config/、extension/、runner/、data/
      store/                     SQLite 仓储、文件仓储、锁、幂等键
      config/                    四层配置的合成与校验；defaults.yaml 为全部可调键的核心默认值
      observability/             事件日志、脱敏、本机通知
      runner/                    roles.py 为分诊、采集、修复与验证共用的角色任务组装
        adapters/                claude、codex、agy、replay
      guards/                    第二层边界检查
      sources/                   采集方法：platform_errors/、access_log/、alerts/、project_probes/、api_fuzz/、static/、incidental/
      retrieval/                 知识索引与检索
      evaluation/                评测运行器
      vcs/                       git 与 gh
      extensions/                扩展点宿主：解析、调用、校验、缓存、核心默认实现
      pipeline/
        collect/  aggregate/  triage/  issue/  fix/
        verify/   release/    learn/   improve/
        checks/                  项目检查(project_checks.py)与复现检查的执行器(regressions/)，修复与验证共用
        common/                  各模块共用的命令运行记录(stage_runs.py)与取证输出的代码位置补全(locations.py)
      orchestrator/              状态表的使用、时间表、续跑、运行记录
        policy/                  自主决定(autonomy.py)与修复流程表(lanes.py)
      cli/                       命令行入口
    dev/                         开发用的脚本：按改动挑选测试、从 schema 生成契约参考
    tests/
      unit/                      纯函数与单个组件的测试
      replay/                    回放夹具：输入交接文档、录制的 agent 结果、期望输出
      integration/               需要真实外部工具的测试，工具缺失时跳过
  skills/                        Agent Skills，各工具共用
    loop/  collect/  aggregate/  triage/  issue/
    fix/   verify/   release/    learn/
  third_party/                   锁定版本的外部 skill 清单与安装记录
  local/                         本机依赖(Semgrep 的虚拟环境、第三方 skill 的下载缓存 third_party-cache/)，不进版本库
  extensions/
    stacks/                      技术栈扩展，每个技术栈一个目录(结构见 10-extensions.md 第 5 章)
      aspnetcore/                stack.yaml、各扩展点的实现、夹具测试
  workspaces/
    demo/                        示例项目工作区(结构见 01-foundation.md 4.3)
      knowledge/  e2e/  regressions/  evals/  rules/  issues/  data/
      extensions/                示例项目的项目扩展(结构见 10-extensions.md 第 6 章)
      worktrees/                 只读 worktree 与各 Issue 的修复 worktree，不纳入版本管理
  docs/                          按 Diátaxis 分类
    tutorials/  how-to/  reference/
    explanation/
      redesign/                  当前设计(与下两者不一致时以它为准)
      architecture/              架构文档：总体结构与各组件的内部设计
      design/                    业务设计：各模块的规则与流程
```

## 6. 两个典型运行过程

**定时运行(无人值守)**

1. launchd 按时间表调用 `tightrein tick --workspace workspaces/demo`：工作日固定时刻做完整运行，其余时刻只检查事件(新提交、新部署)；暂停时不运行。
2. 编排层读取运行记录与状态，判断到期任务：例如 staging 有新部署。
3. 依次调用 `collect`(浅跑)、`aggregate`、`triage`、`issue`；每个模块把结果写入 store，事件写入日志。
4. 需要用户处理的事项汇总进收件箱与每日汇总(每件附推荐做法)，发本机通知。

**交互修复**

1. 用户在 agent 工具中说「继续修 7 号」，`loop` skill 调用 `tightrein continue 7`。
2. 编排层读出 Issue 7 的状态为「待修」，调用 `fix`。
3. `fix` 经执行器运行 `fix-scout` 与 `fix-planner`，计划交用户确认；确认后运行 `fix-executor`，`guards` 在运行前后检查边界；确定性检查通过后，按风险判定由 `fix-reviewer` 做一次轻量或深度评审。
4. 通过后 Issue 进入合并前验证，编排层接着调用 `verify local`，然后停在 `release` 的第一个 git 写操作前等待确认。

## 7. 分篇设计的顺序

架构层面的分篇按依赖关系从下往上展开，每篇说明组件的内部结构、接口、文件划分与测试方式：

| 顺序 | 分篇 | 内容 |
|---|---|---|
| 1 | `01-foundation.md` | 基础层：`domain`、`contracts`、`store`、`config`、`observability` |
| 2 | `02-runner-guards-vcs.md` | 能力层：`runner`、`guards`、`vcs` |
| 3 | `03-retrieval-evaluation.md` | 能力层：`retrieval`、`evaluation` |
| 4 | `10-extensions.md` | 能力层：`extensions`(扩展点的机制与输入输出、核心默认实现)，技术栈扩展 `aspnetcore` 与示例项目的项目扩展 |
| 5 | `04-probes.md` | 能力层：`probes` |
| 6 | `05-collect-aggregate.md` | 流水线层：`collect`、`aggregate` |
| 7 | `06-triage-issue.md` | 流水线层：`triage`、`issue` |
| 8 | `07-fix-verify-release.md` | 流水线层：`fix`、`verify`、`release` |
| 9 | `08-learn-improve.md` | 流水线层：`learn` 与自我改进 |
| 10 | `09-orchestrator-cli-skills.md` | 编排层与入口层：`orchestrator`、`cli`、`skills`、`packaging` |

`extensions` 在 `probes` 之前：探针与 `verify` 都经它取得技术栈与项目专属的数据；分篇编号 10 只表示新增的先后，不代表依赖顺序。
