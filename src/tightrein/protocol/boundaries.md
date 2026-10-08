# 边界与关卡

各阶段与各角色能读写什么、命令白名单、改动量上限、两级受保护文件，以及哪些关卡必须人工确认。程序在同名的 `boundaries.py`：只看改动的文件，不调用模型。

规则的取值都在 settings 的 `boundaries` 段；项目只能在受保护文件两级上追加(`forbidden+`、`highRisk+`)，写原键即报错，缺省项删不掉(见 `settings/load.py`)。

## 读写边界

**是什么**：每个阶段与小步骤允许做的事。

**怎么做**：

| 阶段或步骤 | 能做什么 |
|---|---|
| 采集、评估 | 只读：项目代码只开只读 worktree(游离 HEAD、去掉写权限，见 `git.md`)，外部平台只查不写 |
| 实施·编码 | 只能写自己的 worktree，其余只读；只写测试的任务只能改测试文件 |
| 实施的其他小步骤 | 只读 |
| 发布 | 提交、推送、提 PR、合并都由程序经 `protocol/git/` 执行，agent 不碰 |
| 复盘 | 只写 tightrein 自己的复盘记录 |

所有对外的写操作(git、GitHub、评论)只由程序做，agent 一律没有对外写的权限；agent 只给出提交的范围、一句话、原因与 PR 标题等文字，由程序按项目约定拼进提交信息与 PR。

只读步骤前后的快照比对、可写步骤对 worktree 之外位置的比对见 `security.md`「只读副本与快照比对」。

## 每轮改动检查(check_round)

**是什么**：每一轮可写的步骤结束后，程序对本轮改动(`Change`：路径、增删行数、状态)做的检查。

**怎么做**：

- 改动集合包括未跟踪的新文件(全部行记为新增，`Git.numstat` 给出)，不只看 `git diff`；
- 越界(`outside`)：路径解析符号链接后不在 worktree 之内(agent 建一个指向外面的链接也算)；只写测试的任务改了测试以外的文件；
- 禁改(`forbidden`)：命中禁改级受保护路径；
- 超量(`over_cap`)：计入的文件数或增删行数超过上限；
- 第一次越界：撤回本轮改动，带原因交回重做一次；再越界：停下，生成「出问题」文档。

另几类越界由别处产生：`read_only_changed`(只读步骤前后快照不同，`security.md`)、可写步骤的 `outside`(主仓库或 worktree 之外不可写的路径有变化，`security.md`)、`credential`(启动前工作目录有凭据)、`command`(命令不在白名单)、`hidden_read`(见下)。

`hidden_reads`：会话记录中的工具调用(各适配器 `parse_line` 给出的工具名与参数)读了隐藏目录或凭据文件才算越界。参数中的字符串切成片段，含 `/` 或 `.` 的当作路径(相对工作目录，`~/` 按家目录展开，解析符号链接)：落在隐藏位置(工作区的 data/、家目录下的 .ssh、.aws、.gnupg 等，工作目录与额外可读目录除外)即越界；文件名匹配凭据文件模式的只在路径确实存在时算读取(代码片段中的 `it.key` 不是文件)；只提交结果的工具(`StructuredOutput`)不检查。

## 路径模式

**是什么**：受保护路径、不计数路径、测试路径共用的写法，取 gitignore 的常用子集；路径一律相对 worktree、以 `/` 分隔。

**怎么做**：

- 以 `/` 结尾的只配目录，目录下的全部文件都算命中：`migrations/`、`.github/workflows/`；
- 不含 `/`(结尾的除外)的在任意层级按名称匹配：`*.pem`、`.env`、`package.json`；
- 含 `/` 的从根目录起匹配：`src/*/appsettings*.json`；开头的 `/` 表示根目录；
- `*`、`?`、`[...]` 按 fnmatch 解释，区分大小写。

## 命令白名单

**是什么**：agent 能执行的 shell 命令(`command_allowed`)。

**怎么做**：

- 命令按 shell 记号拆开，开头与白名单某一条的记号完全相同才放行(`git grep` 放行 `git grep -n x`，不放行 `git -C /x grep`)；
- 含串联、管道、重定向、子命令(`;`、`&&`、`|`、`>`、`$(…)`、反引号)的一律拒绝；引号不成对的拒绝；
- 先由工具自带的权限设置挡住，程序再检查一遍兜底。

**在哪配置**：`boundaries.readCommands`；可写步骤另加项目自己的测试、lint、构建命令(工作区 `project.commands`)。

**缺省值**：`git grep`、`git ls-files`、`git log`、`git show`、`git diff`、`git blame`、`grep`、`ls`、`cat`、`head`、`tail`、`wc`。

## 改动量上限(counted)

**是什么**：单个 PR 的文件数与增删行数上限，以及自动确认方案的门槛。

**怎么做**：计数不算测试文件(项目的 `testPatterns`)、锁文件与生成的文件(快照、编译产物，`boundaries.uncounted`)。

**在哪配置**：`boundaries.changeCap`、`boundaries.autoApprove`、`boundaries.uncounted`；自动门槛不能大于上限(配置校验)。

**缺省值**：

| 项 | 缺省 | 出处 |
|---|---|---|
| 单个 PR | 10 个文件、400 行 | SmartBear/Cisco 评审研究：200 到 400 行评审效果最好；Google「Small CLs」约 100 行合适、1000 行通常太大；文件数为推断 |
| 自动确认方案的门槛 | 3 个文件、100 行，没有命中高风险路径，测试通过 | 推断：依 Google 约 100 行合适，自动确认从严 |

原 5 个文件、200 行偏少：#18 一个小功能因此被拆成 3 个子任务。

## 受保护文件(两级，forbidden、high_risk)

| 级 | 缺省路径 | 规则 |
|---|---|---|
| 禁改 | `.env`、`.env.*`、`*.pem`、`*.key`、`*.p12`、`*.pfx`、`secrets*.json` | agent 不能改，碰到就停下交人 |
| 高风险 | CI 配置、依赖清单与锁文件、数据库迁移与 `*.sql`、权限认证目录、带 secret 或 permission 字样的文件 | 可以改，合并必须人工确认；同一组命中只写一次决策文档 |

**在哪配置**：`boundaries.protected.forbidden`、`boundaries.protected.highRisk`，项目用 `forbidden+`、`highRisk+` 追加。

## 关卡

| 类 | 关卡 | 缺省 |
|---|---|---|
| 固定人工(`MANDATORY_GATES`，写死) | `forbidden_changed` 改了禁改文件；`high_risk_merge` 合并的改动命中高风险路径；`over_cap` 超出改动量上限；`needs_decision` agent 给出需要拍板的点 | 人工 |
| 可配置 | `issue` Issue 放行 | `auto_low_risk`：低风险自动，其余人工 |
| | `design` 方案确认 | `auto_within_threshold`：在自动门槛内自动 |
| | `merge` 合并 | `auto_ci_passed`：CI 通过且没有命中高风险路径时自动 |

`gate_is_auto`：可配置关卡的取值以 `auto` 开头即按条件自动(条件由发起方判断)，写 `manual` 即人工；固定人工的关卡即使配置里写成自动也不生效。

提交、推送、提 PR、同步 GitHub 带幂等键直接自动执行(`recovery.md`)；验收通过后的本地清理自动做；程序永远不直接写主分支。

## 设计依据

- 越界只查改动的文件、不调用模型：便宜且确定，每轮都能跑；
- 符号链接解析后再判断是否在 worktree 内：只比较字符串路径时，链接可以把写入带到 worktree 之外；
- 路径模式取 gitignore 子集：项目维护者熟悉这种写法，不需要另学正则；
- 受保护文件只许追加：缺省项是安全底线，项目配置写错一次就可能放开凭据文件；
- 命令白名单按记号比较并拒绝 shell 运算符：前缀字符串比较会被 `cat a; rm b` 绕过；
- 参考：[AI agent guardrails checklist](https://dev.to/brennhill/ai-agent-guardrails-a-practical-checklist-42be)(审批与急停)、[Google: Small CLs](https://google.github.io/eng-practices/review/developer/small-cls.html)、[SmartBear: Best practices for code review](https://smartbear.com/learn/code-review/best-practices-for-peer-code-review/)、[gitignore 模式格式](https://git-scm.com/docs/gitignore#_pattern_format)。
