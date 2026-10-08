# assess/issue：写成 Issue

## 是什么

评估的最后一步(原立项阶段)，纯程序，不调用模型：要修的问题写成 Issue、放行关卡、Issue 状态机，以及可选的 GitHub Issues 镜像。用户自己提的需求(`tightrein new`)也在这里建。

## 流程

1. **核对位置**(`create.from_problem`)：正文要引用的代码位置按只读 worktree 补全(只补全证据、根因、报告、评估)，补不全或不存在就不建，交接为 failed 并提示重新评估。
2. **同一根因追加**：键是根因的「文件#方法」集合(没有方法名时用行号)，与任一未关闭 Issue 有交集就追加：加入问题、写历史，新问题严重度更高时提升并写明。
3. **新建**：编号取 `sequences` 的 issue 序列与 `naming.issue_id`；粗规模档为「大」且根因分布在几个模块的，按模块拆成几个 Issue。
4. **排版**(`body.py`)：正文直接用取证报告，去掉空小节，不再加三条固定的通用验收项；验收标准是取证给的条件加按来源能由采集复查的条件。观察类来源的「部署后的观察期内不再出现指纹为…的问题」只能在部署后确认，是验收阶段的标准(`body.post_deploy`)：方案不对应、审查不判断，由发布的验收按指纹确认。
5. **放行**：缺省待决定(写 `90-issue-pending.md`)；`boundaries.gates.issue` 为 `auto…` 且低风险(没有安全、数据、权限类标记且规模为小)时直接待修。
6. **检查**：建好后查必需小节、正文中的位置真实、日期为绝对日期；不过就保留文件、交接为 failed。
7. **GitHub**(`github.py`)：只在新建、放行、关闭、重开时同步；是否启用只看接入清单 `release.github_issues`(enabled，且 origin 是 GitHub 仓库)。

## 输入与输出

- 文件(Issue 目录 `data/issues/<编号>/`)：`00-issue-record.json`(记录，issues 表是它的索引，可重建)、`00-issue-body.md`(正文，用户可改)、`00-issue-notes.json`(代码笔记)、`22-assess.issue-handoff.json`。
- issues 表：status、title、severity、kind(bug、feature)、origin(problem、user)、gate、stage、step、round 等；其余在 `extra`：slug、problems、rootCauses、rootKey、triageCommit、treatment、size、taskType、labels、history、closeReason、hold、github 等。
- 正文的小节：结论、问题、影响、复现、原因、范围、注意事项、验收标准、修复方向、引用、历史；标题按项目语言(zh、en、ja)，读取时三种语言的标题都认。

### 修复尝试(`attempts.py`)

回归、重开退回待修且上一次已动过手(有分支、PR、合并提交或发布进度)时开下一次尝试：`extra.attempt` 加一，上一次的分支、PR、合并提交、部署与发布进度(`extra.release`)挪进 `extra.attempts` 留档后清空，发布不复用上一次已合并的 PR，验收的观察期从这一次的部署算起；开始实施前(待修 → 实施中)把上一次实施与发布的文件挪进 `attempt_<次>/`(共用文件与采集、评估的文件留在原处)，恢复只看对象目录这一层，各步不沿用上一次的「已完成」。

### 状态机(`transitions.py`，44c)

状态：`needs_decision`、`todo`、`implementing`、`releasing`、`accepting`、`done`、`cancelled`、`held`。事件：`IssueEvent`。`transition(issue, event, *, reason, clock)` 是纯函数，规则写成数据表 `TRANSITIONS`(状态 + 事件 + 条件 → 新状态、副作用、关闭原因)，不在表中的组合抛 `InvalidTransition`(写明当前状态与可用的事件)。`apply_event` 是唯一的写入口：执行副作用、写历史、记录文件与索引在一个事务中，失败恢复文件。

| 事件 | 从 | 到 | 说明 |
|---|---|---|---|
| APPROVE | needs_decision | accepting / releasing / implementing / todo | 按停下时记下的状态与阶段(hold.status、hold.stage)：验收中停下的回到 accepting，发布阶段停下的回到 releasing，实施中停下的回到 implementing(保留步骤与轮次，从停下处接着做)，其余(立项放行)为 todo；只在第一次放行时写 `extra.approvedAt`(合并队列排序，再放行不改) |
| APPROVE | todo(用户需求) | 不变 | 照常申请建修复分支 |
| REJECT | needs_decision | cancelled | reason 为 wont_fix、duplicate、not_a_bug |
| START | todo、implementing、releasing | implementing | releasing：合并主干后改到同一文件或解决过冲突，退回实施重新审查 |
| DELIVER | implementing | releasing | |
| MERGE | releasing | accepting | |
| ACCEPT | accepting | done | 关闭原因 fixed；评估结论回填「判对」 |
| REGRESS | accepting / done、cancelled | todo | 部署确认失败 / 关联问题回归重开；上一次已动过手时开下一次修复尝试(`attempts.py`，REOPEN 同) |
| FAIL | implementing(not_reproduced) | needs_decision | 回填「误判为成立」，关联问题请求重新评估 |
| FAIL | releasing(fix_rejected) | cancelled | 修复未采纳 |
| FAIL | todo、implementing、releasing、accepting | needs_decision | 停下等用户，hold 记原因、时间、停下前的阶段与状态 |
| CANCEL | 未关闭 | cancelled | 只接受 wont_fix、duplicate、not_a_bug |
| REOPEN | done、cancelled | todo | |
| TAKE / GIVE | 未关闭 / held | held / 接管前的状态 | held_by 记接管者 |
| SPLIT_BACK | needs_decision、todo、implementing | cancelled | 关闭原因 split，`create.split_back` 建新的(第一个继承关联问题、根因键与放行时间，关联问题改挂过去)，出错时新建的文件一并删掉 |

状态到推进阶段的映射(`stages.py`)只定义一处：todo、implementing 由实施推进(实施中的优先)，releasing、accepting 由发布推进，其余不自动推进；调度与实施入口都读它，不各自判断。

关闭时连带问题：不修与修复未采纳的问题忽略到严重度升级或出现在新版本；不是缺陷的问题关闭并生成抑制规则、回填「误判为成立」；重复的问题改挂到另一个 Issue。对问题的改动经 `assess/persist.py` 写库并写问题目录的 `00-problem-assess.json`(重建用)，与 Issue 文件一起在出错时恢复。

## 配置

| 键 | 缺省 | 含义 |
|---|---|---|
| `controls.assess.issue.titleMaxLength` | 80 | 标题字数上限，超长截断加省略号 |
| `controls.assess.issue.slugMaxLength` | 40 | 英文简称(也用于分支名)的长度上限 |
| `controls.assess.issue.newAcceptanceMax` | 5 | `new` 的验收标准超过几条时提示拆分 |
| `controls.assess.issue.github` | 标签前缀 `tightrein:` | GitHub 镜像的参数；开关在接入清单 `release.github_issues` |
| `boundaries.gates.issue` | `auto_low_risk` | 放行关卡 |

## 设计依据

- **文件为准**：记录与正文都是文件，issues 表只是索引，坏了可以 `admin rebuild`；正文建好后程序只在末尾追加历史，用户的修改不会被覆盖(旧 `store/files/issue_files.py`)。
- **一个 Issue 只做一件事**：大的按模块拆、`new` 的验收标准多了提示拆，少了计划拆分与评审误判这类返工(Issue 0018 跑了 12 轮的原因)。
- **去掉空小节与固定验收项**：正文会拼进实施每个模型的提示，少读一些；模型不再把通用项当成特有要求。
- **状态机写成数据**：规则一目了然，非法组合统一报错，副作用只返回不执行(旧 `domain/state_machine.py`、`domain/issue.py`)。
- **关闭原因的限制**：已修复只由验收写入、修复未采纳只由发布写入，用户关闭只能是不修、重复、不是缺陷，误判统计才可信。
- **GitHub 只在关键节点同步、开关状态以 GitHub 为准**：少调用 API；公开仓库不建镜像；建 Issue 用幂等键与正文标记找回，不重复建(旧 `pipeline/issue/steps/github.py`)。

## 不做什么

- 不调用模型写正文：取证时已经写好；
- 不发本机通知(P0 也不发，协议层定为不发)，待放行的写进 `90-issue-pending.md`；
- 不在 GitHub 上镜像评论与历史。
