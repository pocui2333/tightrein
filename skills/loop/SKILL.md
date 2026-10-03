---
name: loop
description: 推进 tightrein 的缺陷闭环：查看全局状态与待用户决定的收件箱(每件附推荐做法)，按问题或 Issue 的当前状态继续到下一个关口，或从某一步重来；暂停与恢复；回答新项目接入时的问题。用户说「现在有什么要我处理的」「继续修 7 号」「那个接口 500 的问题现在怎么样了」「把昨天发现的问题都处理一下」「7 号一直做到提 PR」「从分诊重来」「先停一下」「接入还差什么」时使用。
---

# loop

进度就是状态：永远先用命令读状态再行动，下一步只按命令输出判断，不自行推断。所有命令都加 `--json`，标准输出是一个 JSON 对象，关注 `status`、`exitCode`、`result`、`stoppedAt`、`next`、`pendingOperations`、`errors`。

## 何时调用哪个命令

| 用户想要 | 命令 |
|---|---|
| 整体情况、有什么要处理 | `tightrein status --json`(收件箱 `waiting`，每件带 `recommendation`；`paused`、`onboarding`) |
| 某个对象的进度，不推进 | `tightrein next <对象> --json` |
| 推进一个或几个对象 | `tightrein continue <对象>... --json`，说了做到哪一步时加 `--until <模块>` |
| 从某一步重来 | `tightrein continue <对象> --from triage|fix|verify --json` |
| 按描述找对象 | `tightrein find <文本> [--since <日期>] [--until <日期>] --json` |
| 看或处理待确认操作 | `tightrein pending --json`；用户同意后 `tightrein confirm <操作编号> --json`，拒绝时 `tightrein reject <操作编号> --note <说明> --json` |
| 按时间表推进一轮 | `tightrein run --json`(定时由 launchd 调用 `tightrein tick`，不需要手动执行) |
| 暂停、恢复 | `tightrein pause [--workspace <工作区>] --json`、`tightrein resume [--workspace <工作区>] --json`；不带 `--workspace` 为全局 |
| 新项目接入还差什么 | `tightrein workspace check --json` |
| 回答接入问题 | 用户采用推荐时 `tightrein workspace answer <项> --recommended --json`；不接入时 `--skip`；给出值时 `--value <值>` |

- 对象：Issue 用数字编号(`7`)，问题用 `P-0042`。用户没给编号时先 `find`，把「昨天」「上周」换算成绝对日期；有多个候选时列出编号、标题、状态请用户选，不猜。
- 批量：`continue --select status:new` 等选择器，只在用户明确说了范围时使用。
- 单个模块的细节(分诊、修复、验证、发布)交给对应模块的 skill。

## 解读结果

- 汇报整体情况时按「暂停与接入状态、收件箱、异常、产出」的顺序，每项一句话；收件箱每件同时转述推荐做法(`recommendation`)，由用户决定。
- 当天的每日汇总是 `data/reports/daily-<日期>.md`：收件箱、今天各次运行的产出与卡点(暂停、预算到达、未同步到 GitHub、异常)、接入中的项目。
- 每推进一步用一句话说明做了什么；停下时说明停在哪里(`stoppedAt`)、为什么停、下一步是什么(`next`)。

| 退出码 | 含义 | 怎么做 |
|---|---|---|
| 0 | 成功 | 汇报结果 |
| 1、5、6、8 | 失败、达到上限、边界违规、输出不合格 | 转述 `errors` 中的说明与事件日志路径；不重试、不换参数绕过 |
| 2 | 用法错误 | 检查对象编号与参数，改正后再执行 |
| 3 | 前置条件不满足 | 先告诉用户输出给出的前置命令，同意后再执行 |
| 4 | 停在需要用户的关口 | 按下一节处理 |
| 7 | 锁被占用 | 告诉用户有其他运行正在进行，稍后再试 |

## 关口

退出码 4 时按 `stoppedAt` 的关口类型向用户说明，得到用户明确答复后才执行后续命令；用户的同意不能由你推断，也不能替用户同意。

| 关口 | 向用户展示 | 用户同意后 |
|---|---|---|
| `pending-operation` | `pendingOperations` 中每个操作的说明原样展示：命令、分支与文件、影响、是否影响远程、能否撤销 | `tightrein confirm <操作编号> --json`，一次只确认一个 |
| `issue-approval` | `tightrein issue show <编号> --json` 的摘要：复现、原因、范围、注意事项、修复方向、需用户定夺的项 | `tightrein issue approve <编号> --json`；不修时由用户决定 `issue close` 的原因 |
| `fix-plan` | 交给 fix skill 说明计划并请用户表态 | 由 fix skill 执行 `fix confirm` |
| `interactive-fix` | 两种方式：在终端执行 `tightrein fix start <编号>`，或在当前会话用 `tightrein fix start <编号> --here` 按 fix skill 继续 | 按用户的选择执行 |
| `pr-review` | 只给出 PR 链接(Issue 待合并)；关卡 `gates.merge` 为 auto 时说明满足条件后自动合并；收件箱有 `merge-decision`(改动涉及高风险路径，转述决策简报)或 `ci-failed`(CI 必需检查未通过)时一并说明 | 用户在代码托管平台审核，不做任何操作 |
| `manual-queue` | 进入人工队列的原因(`tightrein triage queue --json`) | 用户补充信息后 `tightrein retriage <问题> --note <信息> --json` |
| `candidate-choice` | 候选的编号、标题、状态 | 用用户选的编号重新执行 |
| `awaiting-deploy` | 等待部署与部署后确认，没有可做的事 | 部署后再 `continue` |
| `breaker` | 熔断：同一对象连续失败或反复没有进展，已转待决定；转述原因 | 用户处理后 `tightrein fix start <编号> --force`，或由用户关闭 Issue |
| `onboarding`(收件箱) | 接入问题与推荐答案 | `tightrein workspace answer <项> --recommended --json` 或用户给出的值 |
| `learn-suggestion`(收件箱) | 学习建议的类型、对象与推荐；控制措施与改进建议另转述决定文档(`data/improve/<编号>.md`)的结论、推荐与理由 | 交给 learn skill 展示全文后由用户决定 `learn accept` 或 `learn reject`；接受只记录决定，配置与补丁由用户自己修改 |

## 审批关卡表

哪些事项交用户决定集中写在工作区配置的 `gates` 段(`tightrein config show --key gates --json` 查看)，模型不能在运行时改变：

| 关卡 | auto 时 |
|---|---|
| `issue-approve` | 按规则放行新建的 Issue，不满足的交用户 |
| `plan-confirm` | B 通道的修复计划按规则确认 |
| `fix-session` | `run` 以无人值守方式修复已放行的 Issue(不需要交互会话) |
| `release-writes` | 建分支、提交、推送、提 PR、PR 评论、提撤销 PR 直接执行 |
| `mirror-writes` | GitHub Issue 镜像的写入直接执行 |
| `merge` | 满足自动合并条件(修复自检通过、评审没有阻断项、PR 阶段适用的检查与 CI 必需检查全部通过)时合并 |

改动命中高风险路径的合并、删除分支或数据、修改权限与密钥配置、超出单个任务上限、待决定的 Issue 五项必须交用户。自动处理时输出写明「自动放行」「自动确认修复计划」「已直接执行」及理由，照常转述；仍出现的关口就是需要用户决定的事项，按上表处理。无人值守修复停下(计划需要确认，或 Issue 转为待决定「无人值守修复停下」「熔断」)时按输出中的原因转述。预算(`budget` 段，每次运行、每天、每周)到达时运行停下，每日汇总写明哪一层到达。

## 新项目接入

工作区接入中时只做只读的检查，不修代码、不提 PR、不建 Issue。用 `workspace check` 读出清单，把 `state` 为 `blocked` 的问题连同推荐答案(`recommendation`)一并列给用户，可以一次回答多个；用户答复后逐项执行 `workspace answer`。用户想在终端里逐项回答时，请用户自己执行 `tightrein workspace init --workspace <工作区>`(回车采用推荐)。清单全部完成、试连接都成功、检查命令在主分支上通过后自动转为运行中。

## 禁止

- 不执行任何 git 写操作；不直接编辑工作区中的文件、数据库与交接文档(接入问题的回答经 `workspace answer` 写回)。
- 不替用户确认，不跳过关口，不使用 `--ignore-state`。
- 不执行安装与定时相关的命令；用户问起时给出下面的命令，由用户在自己的终端执行。

## 安装与定时(用户自己执行)

```
tightrein third-party lock
tightrein install --dry-run
tightrein install
tightrein install --check
tightrein schedule install --workspace <工作区>
tightrein schedule show --workspace <工作区>
```
