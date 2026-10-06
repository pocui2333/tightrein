# 命令参考

敲 `tightrein <命令> --help` 查看每条命令的参数。不带命令时等同 `tightrein status`。所有命令都接受 `-w/--workspace`(工作区的路径或项目名，省略时取本机用户配置的 `defaultWorkspace`)与 `--json`。

## 日常

| 命令 | 说明 |
|---|---|
| `tightrein status` | 全局状态与等你处理的事(不带命令时默认执行) |
| `tightrein watch` | 实时界面：运行状态、正在进行的修复与模型调用 |
| `tightrein show` | 一个 Issue、问题或操作的状态与下一步 |
| `tightrein find` | 按描述查找问题与 Issue |
| `tightrein new` | 提一个需求，直接成为待修的 Issue |
| `tightrein continue` | 推进到下一个需要你的关口 |
| `tightrein approve` | 放行 Issue，或确认一项待确认的操作 |
| `tightrein reject` | 拒绝一项待确认的操作 |
| `tightrein run` | 按时间表与状态推进一轮 |
| `tightrein pause` | 暂停：不发起新的运行(不带 --workspace 为全局) |
| `tightrein resume` | 恢复(不带 --workspace 为全局) |

## 分组

| 命令 | 说明 |
|---|---|
| `tightrein issue` | Issue 的查看与维护 |
| `tightrein issue create` | 为去向是提 Issue 的问题创建 Issue(单步；提需求用 new) |
| `tightrein issue sync` | 同步 Issue 文件与数据库 |
| `tightrein issue list` | 列出 Issue |
| `tightrein issue show` | 查看 Issue |
| `tightrein issue edit` | 在编辑器中修改 Issue |
| `tightrein issue reindex` | 重建 Issue 索引 |
| `tightrein issue close` | 关闭 Issue |
| `tightrein issue rerender` | 按当前模板重写 Issue 正文与标题并更新 GitHub 镜像(覆盖除关联、历史外的本地编辑) |
| `tightrein issue reopen` | 重新打开 Issue |
| `tightrein problem` | 发现的问题：忽略、误报、合并、重开、重新分诊 |
| `tightrein problem ignore` | 忽略一个问题 |
| `tightrein problem false-positive` | 把问题判为误报并生成抑制规则 |
| `tightrein problem merge` | 把问题 B 并入问题 A |
| `tightrein problem reopen` | 重新打开问题 |
| `tightrein problem retriage` | 重新分诊或改判一个问题 |
| `tightrein project` | 接入与配置项目：初始化、探针、接口描述、worktree、配置、定时 |
| `tightrein project init` | 新建或继续接入：检查清单并逐项提问 |
| `tightrein project check` | 对工作区生成一次接入清单检查 |
| `tightrein project answer` | 回答一项接入问题 |
| `tightrein project probe` | 项目探针 |
| `tightrein project probe new` | 生成项目探针模板并登记 |
| `tightrein project probe test` | 单独试跑一个项目探针并校验输出 |
| `tightrein project probe logs` | 经日志平台取一段时间内的日志 |
| `tightrein project spec` | 接口描述 |
| `tightrein project spec draft` | 由 AI 读代码起草接口描述，交用户确认 |
| `tightrein project worktree` | 只读与修复 worktree |
| `tightrein project worktree init` | 申请创建只读 worktree |
| `tightrein project worktree sync` | 把只读 worktree 切换到 commit(缺省为 staging 当前部署的 commit) |
| `tightrein project worktree list` | 列出 worktree |
| `tightrein project config` | 生效的配置值与来源层；`--key <键>` 只看该键并列出各层的值，`--routes` 列出每个调用点用的模型别名、工具、模型与生效的路由行 |
| `tightrein project schedule` | launchd 定时任务 |
| `tightrein project schedule install` | 生成并加载定时任务 |
| `tightrein project schedule uninstall` | 卸载并删除定时任务 |
| `tightrein project schedule show` | 查看定时任务的配置与状态 |
| `tightrein admin` | 维护 tightrein：安装、第三方 skill、检查、评测、知识库、扩展 |
| `tightrein admin install` | 把 skills 安装到 agent 工具 |
| `tightrein admin uninstall` | 卸载本工具安装的 skills |
| `tightrein admin third-party` | 第三方 skill 的锁定与校验 |
| `tightrein admin third-party lock` | 锁定或更新第三方 skill 的 commit 与哈希 |
| `tightrein admin third-party verify` | 按锁定清单校验缓存中的第三方 skill |
| `tightrein admin skills` | skill 的一致性检查 |
| `tightrein admin skills check` | 检查 frontmatter、正文行数、参考文件与引用的命令 |
| `tightrein admin doc` | Markdown 交接文档 |
| `tightrein admin doc check` | 校验头信息、必需的小节、数据块的 schema 与小节长度 |
| `tightrein admin eval` | 模块评测 |
| `tightrein admin eval run` | 按参数组成计划并运行评测(--runner、--model 可用逗号给出多个) |
| `tightrein admin eval resume` | 续跑 |
| `tightrein admin eval report` | 显示报告 |
| `tightrein admin eval verify` | 校验用例 |
| `tightrein admin eval add` | 由用户在终端中新增用例(--commit 为用例的 commit) |
| `tightrein admin eval seal` | 用户确认后重算 manifest |
| `tightrein admin kb` | 知识检索 |
| `tightrein admin kb search` | 按关键词检索 |
| `tightrein admin kb get` | 按编号读取 |
| `tightrein admin kb related` | 关联条目 |
| `tightrein admin kb stale` | 待复核的条目 |
| `tightrein admin kb sync` | 同步索引与 INDEX.md |
| `tightrein admin kb eval` | 检索评测 |
| `tightrein admin kb queries` | 汇总检索记录 |
| `tightrein admin kb mcp` | 以 stdio 运行知识检索的 MCP 服务 |
| `tightrein admin ext` | 扩展点 |
| `tightrein admin ext list` | 各扩展点的解析结果 |
| `tightrein admin ext methods` | 方法目录中可选的方法 |
| `tightrein admin ext run` | 单独调用一次扩展点(--input 为输入 JSON 文件) |
| `tightrein admin ext test` | 以夹具测试扩展 |

## 单步执行(高级)

| 命令 | 说明 |
|---|---|
| `tightrein collect` | 运行一种采集方法，或只做部署检测 |
| `tightrein aggregate` | 把采集的信号归并成问题 |
| `tightrein triage` | 分诊新发现与回归的问题 |
| `tightrein triage queue` | 人工队列中的问题 |
| `tightrein fix` | 修复 |
| `tightrein fix prepare` | 申请建修复分支与 worktree |
| `tightrein fix start` | 进入修复会话 |
| `tightrein fix plan` | 出修复计划 |
| `tightrein fix confirm` | 确认或退回修复计划 |
| `tightrein fix apply` | 实施修复与检查 |
| `tightrein fix done` | 修复完成，交给验证 |
| `tightrein fix abandon` | 放弃修复 |
| `tightrein fix cleanup` | 清理修复分支与 worktree |
| `tightrein verify` | 验证 |
| `tightrein verify local` | PR 阶段的本机检查 |
| `tightrein verify staging` | 部署后确认 |
| `tightrein verify screenshots` | 截图查看的结论 |
| `tightrein release` | 提交、推送、提 PR 与跟踪 |
| `tightrein learn` | 指标、周报、学习建议与链路健康 |
| `tightrein learn report` | 生成周报 |
| `tightrein learn metrics` | 计算指标，不写快照 |
| `tightrein learn health` | 链路健康检查 |
| `tightrein learn lessons` | 出问题时写经验，修好的缺陷生成并验证 Semgrep 规则 |
| `tightrein learn curate` | 经验清理与知识库定期复核 |
| `tightrein learn improve` | 从近期失败归纳改进建议并评测(只建议，不修改) |
| `tightrein learn suggestions` | 列出学习建议 |
| `tightrein learn accept` | 接受一条学习建议 |
| `tightrein learn reject` | 拒绝一条学习建议 |

## 定时任务

`tightrein tick` 由 launchd 定时调用(`tightrein project schedule install` 安装)，不需要手动执行。
