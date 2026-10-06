# 9. 打包结构：工具无关的核心与 skill

## 9.1 总体形式

整套工作流不绑定某一个 agent 工具。Claude Code、Codex CLI、Antigravity CLI(agy)等都可以用来执行其中的 LLM 环节，各环节用哪个工具、哪个模型，在项目配置中指定。

- **核心是一个命令行程序 `tightrein`**：用 Python 编写，负责编排、状态机、数据库、schema 校验、探针与聚合等确定性脚本、评测运行器和定时入口。核心不依赖任何 agent 工具。
- **LLM 环节通过「agent 执行器」调用**：核心把任务交给执行器，执行器再调用配置指定的 agent 工具，并把结果转换成统一格式交回(9.4)。
- **skill 采用 Agent Skills 开放标准**：每个模块一个 `SKILL.md`(YAML frontmatter 加 markdown 正文)。这个标准由 Anthropic 发布，Claude Code、Codex CLI、Antigravity CLI、GitHub Copilot、Cursor 等三十多个工具都支持同一份文件。frontmatter 只使用标准字段(`name`、`description`)，不依赖某个工具的扩展字段。
- **项目规则用 AGENTS.md**：它同样是跨工具的约定。
- **模块之间只通过数据交互**：skill 之间不互相传递对话内容，只通过数据库和文件中的中间产物(信号、问题、分诊结论、Issue、验证报告)衔接。任何一个模块都可以单独重跑，也可以换一个工具重跑。

## 9.2 目录结构

```
~/Projects/tightrein/
  core/                                核心程序(Python 包 tightrein)，内部分层与目录见 docs/explanation/architecture/00-overview.md
  skills/                              Agent Skills 标准格式，各工具共用
    loop/SKILL.md                      编排
    collect/SKILL.md                   第 1 章 信号采集
      references/                      各采集方法的说明(见 docs/reference/methods.md 与 how-to)
    aggregate/SKILL.md                 第 2 章 聚合去噪
    triage/SKILL.md                    第 3 章 分诊取证
    issue/SKILL.md                     第 4 章 提 Issue
    fix/SKILL.md                       第 5 章 生成修复
    verify/SKILL.md                    第 6 章 验证
    release/SKILL.md                   第 7 章 合并发布
    learn/SKILL.md                     第 8 章 观测学习(含第 14 章的改进建议)
  third_party/                         锁定版本的外部 skill 清单
  extensions/
    stacks/                            技术栈扩展：同一技术栈的项目都能复用的扩展点实现
      aspnetcore/                      ASP.NET Core：接口描述导出、端点权限声明、控制台日志解析、构建告警与依赖漏洞、dotnet run
        stack.yaml                     提供的扩展点、命令、默认参数与外部依赖
        defaults.yaml                  该技术栈的配置默认值，例如风险判定规则(architecture/01 5.1 的第二层)
  workspaces/
    demo/                              示例项目的工作区
      project.yaml                     项目适配配置，见 9.7
      normalize.yaml                   项目特有的规范化规则
      suppressions.yaml                抑制规则
      extensions/                      示例项目的项目扩展：角色能力矩阵解析、日志目录读取、本项目帧判定、前端构建告警与 npm audit、前端路由提取、前端启动
      knowledge/                       项目知识库，每类一个目录，每个条目一个文件
        defect-pattern/                缺陷模式
        tradeoff/                      已接受的取舍
        triage-lesson/                 分诊经验
        fix-lesson/                    修复经验
        contract/                      字段契约等项目约定
        reference/                     项目参考资料
        INDEX.md                       自动生成的总索引
      e2e/                             巡检用例
      regressions/                     复现检查
      evals/                           评测集(只读)
      rules/                           规则库：缺陷变规则验证通过的 Semgrep 规则(8.6)
      issues/                          本地 Issue
      worktrees/                       只读 worktree(readonly/)与各 Issue 的修复 worktree(fix-<Issue 编号>/)
      data/                            数据库、交接文档、中间过程文档、日志、周报
  docs/                                tutorials、how-to、reference、explanation(redesign、architecture、design)
```

- 核心不含任何项目名、技术栈名与框架特有的知识：需要这些知识的环节经扩展点调用技术栈扩展(`extensions/stacks/`)或项目扩展(工作区 `extensions/`)，扩展点的定义、查找顺序与两类扩展的目录见 `docs/explanation/architecture/10-extensions.md`。
- skills 只有一份，由 `packaging/` 中的安装脚本链接或复制到各工具读取 skill 的位置；不为某个工具单独维护一份。各角色任务用到的第三方 skill(锁定清单 `third_party/skills.lock.yaml` 中的条目)同样由安装脚本一并安装；没有安装时角色任务不引用它们，已锁定的副本哈希核对不通过时停止并报出文件。
- Python 虚拟环境与 Playwright 等依赖由核心程序管理，放在工作区之外的缓存目录。
- `~/Projects/tightrein/` 整体是个人 git 仓库；`workspaces/*/data/` 与 `workspaces/*/worktrees/` 不纳入版本管理。
- 知识条目的文件名为 `<编号>-<简称>.md`，编号带类型前缀(例如 `DP-0012`)；目录与总索引由脚本生成，命中次数只记录在数据库中。工作区的完整布局见 `docs/explanation/architecture/01-foundation.md` 4.3。

## 9.3 各 skill 的职责与运行方式

| skill | 对应章节 | 做什么 | 主要依赖 | 运行方式 |
|---|---|---|---|---|
| `loop` | 全部 | 读取当前状态与时间表，决定本次运行哪些模块，汇总运行摘要 | 核心的状态机 | 定时无人值守；也可以手动运行 |
| `collect` | 1 | 运行指定的探针，产出信号 | 探针脚本、Schemathesis、Playwright；静态巡检经 agent 执行器 | 无人值守 |
| `aggregate` | 2 | 规范化、指纹、归并、复现确认、状态更新 | 聚合脚本 | 无人值守 |
| `triage` | 3 | 把问题转成主张，取证，出结论与去向 | agent 执行器 | 无人值守 |
| `issue` | 4 | 创建、查看、放行、关闭本地 Issue | Issue 脚本 | 创建无人值守；放行、关闭由用户手动 |
| `fix` | 5 | 建修复工作区，按 5.2 的流程修复 | agent 执行器(交互模式) | 交互会话 |
| `verify` | 6 | PR 阶段的本机检查、部署后确认 | 验证脚本、启动脚本 | 合并前手动触发；部署后无人值守 |
| `release` | 7 | 提交、同步主干、推送、提 PR、跟踪 PR 与部署、收尾清理 | git、gh | git 写操作逐次确认；跟踪无人值守 |
| `learn` | 8、14 | 长期跟踪、指标、周报、经验、缺陷变规则、学习建议、链路健康检查；`learn improve` 归纳改进建议并评测对比 | 统计脚本、agent 执行器、评测运行器 | 无人值守；`learn improve` 只由用户运行，建议由用户批准后自己应用 |

每个 skill 背后都有一个对应的核心子命令(`tightrein collect`、`tightrein triage` 等)，每个子命令都可以单独运行，见第 15 章。skill 负责告诉 agent 这一步做什么、怎么解读结果；实际的执行、校验和写入都由核心子命令完成，所以不经过任何 agent 工具也可以直接在终端运行。

## 9.4 agent 执行器

**统一接口**：核心交给执行器的任务，与执行器交回的结果，格式固定，与所用工具无关。

| 任务字段 | 说明 |
|---|---|
| `instructions` | 本次任务的说明，以及要加载的 skill |
| `workdir` | 工作目录：只读 worktree、修复 worktree 或工作区 |
| `outputSchema` | 期望输出的 JSON schema |
| `access` | `read-only`(只读)或 `workspace-write`(只能写工作目录) |
| `allowedCommands` | 允许执行的命令，例如只读 git 命令、项目检查命令 |
| `limits` | 最大轮数、最长时间、费用上限 |
| `interactive` | 是否交互运行：修复为交互，其余为无人值守 |

| 结果字段 | 说明 |
|---|---|
| `output` | 结构化结果，已由核心按 `outputSchema` 校验 |
| `usage` | 输入、输出 token 与费用；工具不提供的字段留空 |
| `durationMs` | 耗时 |
| `transcriptPath` | 会话记录，已转换为本工具的事件格式(10.4) |
| `status` | `ok`、`failed`、`limit-reached`(达到轮数、时间或费用上限)、`schema-invalid`(输出重试后仍不合 schema)、`guard-violation`(核心的边界检查判定违规) |

**适配器**：每个工具一个适配器，把统一接口翻译成该工具的调用方式。

| 能力 | Claude Code | Codex CLI | Antigravity CLI(agy) |
|---|---|---|---|
| 无人值守调用 | `claude -p` | `codex exec` | `agy -p` |
| 结构化输出 | `--output-format json --json-schema`，结果在 `structured_output` | `--output-schema`，最终消息符合 schema | `--json-schema`，结果在 `structured_output` |
| 只读与可写 | `--allowedTools` 白名单与只读的权限模式 | `--sandbox read-only` 或 `workspace-write`，配合审批策略 | 无工具与命令白名单；只读为缺省权限模式加沙箱，可写为 `--mode accept-edits`，需要确认的写入与命令被自动拒绝 |
| 会话记录 | `--output-format stream-json` | 执行事件输出 | `--output-format stream-json`(init、step_update、result 事件) |
| 用量 | 返回值中的 usage 与 `total_cost_usd` | 返回值中的 usage | 各步骤的 token 用量，没有费用 |

适配器只负责调用和格式转换，不做任何业务判断。新增一个工具，只需要新增一个适配器。

## 9.5 能力差异由核心兜底

各工具的约束能力不一样，关键的约束不依赖工具自身，由核心统一保证：

| 约束 | 核心的做法 |
|---|---|
| 输出格式 | 不论工具是否支持 schema 约束，核心都按 `outputSchema` 校验；不合格重试一次，仍不合格记为 `schema-invalid` 并停止 |
| 轮数与时间 | 工具支持轮数上限的按上限设置；另由核心按 `limits` 计时，超时终止进程 |
| 只读 | 只读 worktree 的文件在 agent 运行期间设为不可写，从文件系统层面保证只读 |
| 可写范围与受保护文件 | agent 运行结束后，核心检查 worktree 的改动：出现工作目录以外的改动、受保护文件的改动(11.2)、测试与复现检查的改动时，判为违规并停止 |
| git 写操作 | 运行前后比较 git 状态：agent 新建了提交、分支，或改动了远程配置，判为违规并停止 |
| 凭证 | agent 进程的环境变量中不包含任何凭证；只读 worktree 与修复 worktree 中不放置含凭证的本地配置 |

工具自身的限制(白名单、沙箱、hook)照常启用，作为第一层；核心的检查是第二层，不论换成哪个工具都生效。

## 9.6 各环节选用工具与模型

模型按角色分为三档。每一档对应各工具的具体模型与推理强度，写在配置的 `capabilities.<档>.<工具>` 中(`model`、`effort`)；每个角色默认用哪一档写在 `roleCapabilities.<角色>` 中，某个环节需要不同档位时用 `stages.<环节>.roles.<角色>.capability` 覆盖。下表为核心默认值；档对应的模型与各环节用哪个工具是个人偏好，写在本机用户配置的 `agents` 段，项目需要时在 `project.yaml` 中覆盖(architecture/01 5.1、5.3)。

| 档 | 模型与推理强度 | 角色与任务 |
|---|---|---|
| 轻量档 `light` | 小模型，低推理强度 | 修复勘察(`fix-scout`)；静态巡检的初筛(`static-review`)；分诊查重；知识写入去重判断(`knowledge-curator`) |
| 标准档 `standard` | 中等模型，默认推理强度 | 修复的轻量评审与截图评审；`lesson-writer`；评测的模型评审 |
| 强档 `strong` | 强模型，高推理强度 | 分诊取证(`claim-verifier`、`refuter`，含静态巡检中的取证)；修复计划、前端设计、复现测试与实施(`fix-planner`、`frontend-designer`、`fix-executor`、`repro-writer`)；静态巡检的基线审查(`baseline-review`，一次性、影响大)；修复的深度评审；缺陷变规则(`rule-writer`)与改进建议(`improvement-writer`) |

- 只有取证、修复计划与实施、基线审查、深度评审、写规则与改进建议需要强档；其余任务的输入范围明确、判断简单，用轻量档或标准档，token 与耗时更低。
- 证伪复核与深度评审使用与生成者不同的工具或模型，减少模型评审偏好自己生成内容的偏差(12.5)；配置校验时按合并后的生效值检查这一点，相同时报出键名。只用一种工具时，用不同的模型档区分生成者与评审者(例如证伪复核用标准档)；用两种工具时，评审可用另一种工具。
- 工具按环节指定(`stages.<环节>.tool`)，没有指定时用 `defaultTool`，任选已接入的工具；核心不给缺省工具。个别角色或任务可以单独指定工具与模型(`stages.<环节>.roles|tasks.<名称>.tool`、`model`)，优先于环节的工具，例如主力用 Claude Code，前端设计、勘察、查重交给 agy；证伪复核、评审与截图评审仍用各自的设置。各工具都由核心的适配器以子进程调用，一种产品的会话不直接调用另一种产品。
- 评测运行器(architecture/03 第 2 章)可以在同一评测集上比较不同工具、模型与档位的得分、耗时与费用，据此调整各角色的档位。

分诊与修复的各个角色都以本工具 skill 中的说明文件存在，经执行器调用，与所用工具无关。它们由项目原有的 `auto-probe` 与 `auto-dev` 拆分重组而来，见 9.10。

## 9.7 项目适配配置

对接一个新项目时，只需要在工作区中提供 `project.yaml` 与所需的项目扩展，不改核心代码；只能按项目决定的部分，从方法目录中选用方法并填写参数；目录中没有适用的方法时才写项目扩展(architecture/10 1.4)：

| 配置项 | 示例项目中的取值 |
|---|---|
| 项目仓库路径、主分支 | 项目主工作区路径，`main` |
| staging 地址与健康检查接口 | staging 地址，`GET /api/Health` |
| 部署来源(`extensions.deploy-source`) | `core/github-actions`，工作流 `deploy.yml` |
| 角色与测试账号 | 五个角色，账号密码在 macOS 钥匙串中的条目名 |
| 用到的技术栈(`stacks`) | `[aspnetcore, vue, node]` |
| 各扩展点选用的方法(`extensions.<扩展点>.use`) | 见 architecture/10 1.6 中示例项目的取值；只有 `authz-roles` 为自定义实现 |
| 接口描述导出(`spec-export`) | 技术栈扩展，参数为启动项目与文档名 `V1` |
| 端点权限(`authz-endpoints`) | 技术栈扩展，读取构建产物中的 `[Authorize(Policy)]` 等声明 |
| 角色能力(`authz-roles`) | 示例项目的项目扩展，解析 `PermissionMatrix.cs` |
| 模糊测试排除范围与运行档位 | 1.6.1 中的排除接口与浅跑、深跑参数 |
| 服务端日志(`log-source`、`log-parse`) | 日志来源为示例项目的项目扩展(NSSM 日志目录)；解析为技术栈扩展(`simple` 单行格式)，本项目帧由示例项目的项目扩展按命名空间 `Demo` 判定 |
| 确定性工具(`static-tools`) | 技术栈扩展的后端构建告警与依赖漏洞，示例项目的项目扩展追加前端构建告警与 `npm audit` |
| 页面路由(`page-routes`) | 示例项目的项目扩展，解析前端路由文件 |
| 项目检查命令 | 项目约定的构建与测试命令 |
| 本机启动(`local-run` 与 `localRun.ports`) | 6.2 中的后端与前端启动方式、端口、迁移文件 |
| 受保护文件 | 11.2 中的清单 |
| 风险判定规则(`review.riskRules`) | 5.8 中示例项目补充的路径 |
| 受影响测试的映射(`checks.commands[].affected`) | 6.3 中前端 `*.test.js` 与 Python 单元测试的对应方式 |
| 各环节的工具与模型 | 9.6，写在本机用户配置的 `agents` 段，示例项目沿用核心默认的档位 |
| 项目知识库 | 工作区 `knowledge/`：缺陷模式、已接受的取舍、字段契约等 |
| Git 规则 | 分支、提交与 PR 的约定(工作区 `git.conventions` 与 `git.personalPrefix`，或从项目文档识别，design 7.2)、拆分阈值 |
| 运行时间表 | 各探针与各模块的运行时间，例如静态巡检工作日每天 1 到 2 次 |
| 预算 | 各环节每天的费用上限 |

配置按四层合成：核心默认值、技术栈默认值、`project.yaml`、本机用户配置，后一层覆盖前一层(architecture/01 5.1)。所有可调的值都在这四层中，代码中不写死；`tightrein project config [--key <键>]` 列出每个键的生效值及其来源层。个人前缀这类因人而异的值，放在本机的用户配置文件中，不写进工作区。

## 9.8 编排的运行逻辑

`tightrein run` 每次执行时：

1. **读取状态**：从数据库读取运行记录、问题、Issue 与 PR 的状态，以及待用户确认的事项。
2. **判断到期任务**：按 `project.yaml` 的时间表和触发条件判断本次该做什么，例如：
   - staging 有新部署：`collect` 浅跑，接着 `aggregate`。
   - 到了静态巡检时间：`collect` 静态巡检，接着 `aggregate`。
   - 有新的「新发现」或「回归」问题：`triage`，判为值得修的交给 `issue` 创建。
   - 有待合并的 PR，或 PR 已合并而等待部署后确认：`release` 跟踪；已部署的交给 `verify` 做部署后确认。
   - 每次运行：`learn` 在出问题时写经验，为修好的缺陷生成并验证规则。
   - 每周第一个工作日：`learn` 生成周报。
3. **依次执行**：每个模块独立运行，一个模块失败只记录并通知，不影响其他模块。
4. **停在需要用户的地方**：放行 Issue、确认修复计划、确认 git 写操作、审核 PR，这些都不在无人值守运行中进行，只汇总进运行摘要，等用户处理。
5. **输出运行摘要**：本次执行了什么、产生了什么、有哪些事项等待用户，通过本机通知发出。

**定时触发**：launchd 按时间表执行 `tightrein tick --workspace <工作区>`：工作日固定时刻完整运行，其余时刻只检查新提交与新部署。定时入口是核心程序，不是某个 agent 工具。

**手动使用**：

- 终端中：`tightrein status` 查看全局状态与待处理事项；`tightrein <模块>` 单独运行某个模块。
- agent 工具中：在任何支持 Agent Skills 的工具里调用 `loop` skill，由它调用核心子命令推进一轮，遇到需要确认的事项当场确认。

## 9.10 拆解 auto-probe 与 auto-dev

项目 `.claude/` 中原有的 `auto-probe`(分析评估)与 `auto-dev`(开发流水线)是为本工作流先行设计的，现拆解后重组进各模块，拆解完成并验证后删除这两个 skill。

**拆解去向**

| 原有部分 | 拆解到 |
|---|---|
| `claim-verifier` | `skills/triage/references/roles/`(3.2) |
| `auto-probe` 的容量分析与价值评估角色 | 并入 `claim-verifier` 的一次取证：响应过慢由它取证，价值判断写进取证的评估(3.2) |
| `auto-probe` 的归类、证据质量检查、三类单独标出 | 核心的分诊编排(3.2)；排序由处理标签取代(3.5)，跨条对齐不保留 |
| `auto-probe` 的发现报告模板与取证标准 | `skills/triage/references/`，取证标准同时并入 12.2 分诊的验收标准 |
| `code-scout` | `fix-scout`(5.2) |
| `solution-architect` | `fix-planner`，只保留修复计划部分(5.2) |
| `auto-dev` 的联网风险检索角色 | 不保留：高风险修复标出风险并另加深度评审(5.8) |
| `plan-executor` | `fix-executor`(5.2) |
| `delivery-auditor` 的自查 | 能由代码判定的部分并入修复的程序检查(5.2 第 6、7 步)；需要判断的部分并入 `fix-reviewer` 的一次评审(5.2、5.8) |
| `finding-fixer` 与审核修复循环、阻断项分类 | 修复流程第 6 步(5.2) |
| `refuter` | 分诊的 `refuter`(3.2) |
| `browser-verify.md` | `skills/verify/references/local-run.md`(6.2) |
| `closeout.md` 中提交前自查的部分 | `release` 的提交前置条件(7.2) |
| `work-summary.md`(给用户汇报用的工作总结) | `release` 在 PR 合并后可选生成工作总结 |
| `frontend-ui.md` 等 项目专属的参考资料 | 工作区 `knowledge/` |
| `config.json` 中的模型能力分档 | 核心默认配置中的三档与角色对应(9.6)，项目在 `project.yaml` 中覆盖 |
| `.claude/knowledge/` | 工作区 `knowledge/` |
| `auto-dev/plans/`、`auto-dev/runs/`、`auto-probe/findings/` 中的历史产出 | 归档到工作区 `data/archive/`，其中的任务外发现一次性导入为信号(1.6.5) |

`auto-dev` 中服务于功能开发的部分不纳入：需求拆解与提问、按功能拆模块、跨模块对齐、多模块并行实施、方案设计与评分、串行区管理，以及依赖 Claude Code 编排能力的 workflow 脚本。

**拆解与删除的顺序**

1. 按上表在本工具中建立各角色与参考资料，并为 `triage`、`fix` 各准备至少 3 个评测用例(architecture/03 2.3)，用例取自原有的历史产出。
2. 在评测集上运行新的分诊与修复，结果不低于用原有 skill 处理同样用例的结果。
3. 列出将要删除与移动的完整清单(`auto-probe`、`auto-dev` 目录与 `agents/auto-probe`、`agents/auto-dev` 下的全部文件，以及 `.claude/knowledge/` 的移动)，经用户确认后执行。
4. 删除后，功能开发不再有 `auto-dev` 流水线可用；需要时另行设计。

## 9.11 第三方 skill 的采用门槛

引入任何第三方 skill 之前，必须同时满足：

| 条件 | 衡量方式 |
|---|---|
| 使用人数 5000 以上 | 来源仓库的 GitHub 星标数，或 skill 注册表公布的安装量，取其一 |
| 有持续维护记录 | 来源仓库最近 90 天内有提交，且没有归档 |

不满足的部分由本工具自己实现。已采用的第三方 skill 在 `third_party/skills.lock.yaml` 中记录来源、锁定的 commit、核实日期与核实时的星标数和最近提交日期；`learn` 每月重新核实一次，不再满足条件的在周报中列出，由用户决定替换或自行实现。

| skill | 来源 | 核实结果(2026-09-30) |
|---|---|---|
| `differential-review`、`variant-analysis` | `trailofbits/skills` | 7296 星，最近提交 2026-09-28，未归档 |
| `fp-check`、`sharp-edges`、`semgrep-rule-variant-creator` | `trailofbits/skills` | 同一来源仓库，同上；锁定由用户执行 `tightrein admin third-party lock` |

## 9.9 参考

- Agent Skills 开放标准：`SKILL.md` 由 YAML frontmatter 与 markdown 正文组成，Claude Code、Codex CLI、Antigravity CLI、GitHub Copilot、Cursor 等三十多个工具支持同一份文件；启动时每个 skill 只加载名称与描述，任务匹配时才加载全文。
- Codex CLI 的非交互模式：`codex exec` 运行一次任务后退出，`--output-schema` 要求最终回复符合 JSON schema，`--sandbox read-only` 保持只读，默认即为只读沙箱。
- Gemini CLI 已于 2026-06-18 停用，继任者为 Antigravity CLI(`agy`)。`agy -p` 配合 `--output-format json` 或 `stream-json`，后者输出 init、step_update、result 等事件；无人值守模式按 `~/.gemini/antigravity-cli/settings.json` 的 `permissions.allow` 放行命令(1.2.17 实测；更早的版本不读，见上游 issue google-antigravity/antigravity-cli#548)，其余需要确认的动作被自动拒绝。`tightrein admin install` 为 agy 补上只读命令(`git grep`、`git ls-files`、`git log`、`git show`、`git diff`、`grep`、`ls`、`cat`、`head`、`tail`、`wc`)，只补缺的并记进安装记录，卸载时只移除这些。
- Claude Code 的 headless 模式：`claude -p` 配合 `--output-format json --json-schema`，结果在 `structured_output` 中。
