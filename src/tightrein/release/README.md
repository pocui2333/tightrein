# 发布(release)

## 是什么

代码改好之后到线上确认，一个阶段管到底：提交 → 同步 main → 推送 → 提 PR → 等 CI → 合并(合并队列) → 跟踪部署 → 验收 → 清理本地。纯程序，不调用模型。原来的验收并入发布，作为最后部分(`accept/`)。

## 流程

`release(runtime, issue) -> StepOutcome`(`release.py`)从 Issue 当前的状态接着推进，能做多少做多少，遇到要等的或要人决定的就停：

| Issue 状态 | 做什么 | 停在哪里 |
|---|---|---|
| 发布中 `releasing` | `commit.py` 提交 → `sync.py` 合并最新的 origin/main → `push.py` 推送 → `pr.py` 提 PR、发审查摘要评论 → `ci.py` 等 CI 结束 → `merge.py` 判断并合并 | 合并 main 改到同一文件或解决过冲突、要推送的改动与交付时审查过的不同(diff 哈希，比如人解决冲突后自己提交了合并)：退回实施重新自检与审查；合并 main 冲突：放弃这次合并、退回编码解决(连续退回 `release.conflictReturns` 次之后再冲突才停下等人)；高风险路径(定案时用户逐项确认过的除外)或人工合并关卡：写待决定文档等人确认，`tightrein approve` 通过后由本工具合并，选「自己在 GitHub 上合并」时等人合并(交付带用户接受的未通过项时本工具不合并，文档直接请人在 GitHub 上合并)；CI 未结束：下次接着等 |
| 验收中 `accepting` | `deploy.py` 找包含合并提交的部署 → `accept/confirm.py` 按问题来源确认 → 通过时转为完成、写交付文档(`90-issue-deliver.md`)、`cleanup.py` 清理本地；回归时 `accept/revert.py` 先提撤销 PR、清理这一次的修复目录，再退回待修(开下一次修复尝试，见 assess/issue/attempts)；Issue 正文中部署后才能确认的验收标准按指纹对应到确认结论(交接的 `criteria`)；接入清单 `release.accept` 不启用时部署完成即完成，不观察 | 还没部署、观察期未满：下次接着看；部署失败：写待决定文档 |

PR 已被人合并(含 GitHub 原生自动合并)的转为验收中；被关闭且未合并的以「修复未采纳」取消，PR 上最后一条评论或评审正文作为用户说明记下。

退回实施时 `41-release.pr` 的交接写 `backTo`(`implement.code` 或 `implement.check`)与 worktree 当前的 commit(`worktreeCommit`，作为实施的检查点)，冲突时另写要合并的 `main`、冲突文件 `conflicts`、main 一侧的相关提交 `mainCommits`、冲突报告 `report` 与第几次退回 `returns`，推送前核对不符时另写现在与交付时的 diff 哈希 `diffHash`、`deliveredDiffHash`；实施据此接着做(implement/context 的 `returned`)。

停下要人处理的(退回编码次数用完后的冲突、检查未通过、交付之后工作区变了、分支名不合规、没有交付记录)写 `90-issue-pending.md`，Issue 转为待决定，写明原因。

发布开始时合并停在进行中(交给人解决的冲突，或旧版发布冲突时留下的)，在提交之前先处理(`sync.resume`)：用户已解决(没有未合并的文件、冲突文件里没有冲突标记)就完成合并提交、退回实施重新自检与审查；没解决而退回编码的次数没用完就放弃这次合并，照常同步时再冲突即退回编码；次数用完了继续等人。待决定的 Issue 经 `tightrein approve` 回到发布中，下次运行即按这里处理。用户解决后自己提交了合并的，合并已不在进行中、main 已是祖先，由推送前核对 diff 哈希发现，同样退回实施重新自检与审查。

提交、同步、推送、提 PR 在做决定时记下观察到的状态(`Git.state` 的分支、HEAD、改动哈希、origin/main，`GitHub.pull_state` 的打开的 PR)，作为 `expected` 交给写操作；执行前重新观察有不同(protocol/git 的 `Stale`)就不执行，记一条事件，Issue 留在发布中，下次运行重新观察、重新决定。

推送被拒分三种(protocol/git)：非快进时先同步 main，转为待决定；其余远程拒绝转为待决定，原因写明是分支保护、仓库规则或权限不允许，还是认不出的原因(附错误输出，不说成权限问题)；远程服务器或网络的临时错误(如 GitHub 的 `[remote rejected] (Internal Server Error)`、`(failure)` 加 `fatal error in commit_refs`、`(cannot lock ref …)`)由 `Git.push` 退避重试，次数用完仍失败也不交人，记一条事件，Issue 留在发布中，下次运行再推送。验收中回归时推送撤销分支遇到同样的临时错误：撤销 PR 还没提，记一条事件，Issue 留在验收中，这一步记为失败(一直推不上去时由对象熔断交给人)，下次运行再提。
一次推进可能要几分钟(同步、推送、等 CI)，合并关卡上的 Issue 每次运行都会接着推进：记下 PR 与判断合并之前重新读一次 Issue，期间用户的决定(`tightrein approve` 记下决定、清掉 gate)照常生效，不会被开头读入的旧记录整份覆盖；期间被接管或取消的不再往下做。

`queue(runtime) -> list[StepOutcome]`(`queue.py`)：发布中的 Issue 按严重度、再按放行时间排队，依次调用 `release`；合并一个接着下一个，后面的由 `release` 先合并最新的 main；转为待决定的移出队列，继续后面的；遇到人工合并关卡、或到达时间上限即停。仓库开了 GitHub 自带的合并队列(或主分支要求必需检查)时，`release` 对每个 PR 只做到开启 GitHub 原生自动合并就返回，由 GitHub 排队。

## 输入与输出

输入：
- 实施·交付的 handoff(`38-implement.deliver-handoff.json`)的必填事实，键为小驼峰：`branch`、`worktree`、`commit`、`diffHash`、`changedFiles`(路径或 `{path, added, deleted}`)、`checks`(`[{name, passed, detail}]`)、`acceptedFindings`(用户接受的未通过项)、`review`(`{round, conclusions}`)、`highRiskPaths`、`release`(编码时 agent 给出的文字：`title`、`scope`、`summary`、`why`、`problem`、`approach`、`limitations`)；
- Issue 记录：`kind`、`severity`、`extra.problems`(关联问题)、`extra.github`(镜像 Issue 编号)、`extra.approvedAt`(放行时间，排队用)；
- store 的 problems 与 occurrences(验收)；部署来源(接入清单 `release.deploy`，method 为 `deploy_source/` 下的方法名)；是否验收(接入清单 `release.accept`)；
- 凭据：部署来源方法清单里写的条目名(Vercel 为 `vercel.token`)，取自 secrets.json。

输出：
- 每一步一份 handoff：`41-release.pr`(提交、同步、推送、PR)、`42-release.ci`、`43-release.merge`、`44-release.deploy`、`45-release.accept`、`46-release.cleanup`；冲突报告 `41-release.pr-evidence.md`；
- Issue 记录的 `branch`、`pr`、`merge_commit`、`deploy`、`extra.acceptUntil`(验收观察期截止，status、watch 显示)与 `extra.release`(发布进度：最近一次推送的 commit、合并进来的 main、未合并的原因、已开启自动合并的 head、部署与验收)；
- state 表 `release.deploy.deployments`(采集据此给信号定 commit)；
- 给人看的 `90-issue-pending.md`、`90-issue-failure.md`、`90-issue-deliver.md`(protocol/documents)。

## 配置

`settings/defaults.json` 的 controls：

| 键 | 缺省 | 说明 |
|---|---|---|
| `release.mergeMethod` | `squash` | `gh pr merge` 的方式；`merge` 时撤销加 `-m 1` 由合并提交的父提交数判断 |
| `release.reviewComment` | `false` | 是否把最后一轮审查结论发成 PR 评论 |
| `release.conflictReturns` | `2` | 合并 main 冲突时连续退回编码解决的次数上限；用完后再冲突就停下等人解决；同步没有冲突、推送成功后重新计 |
| `release.ci.watchInterval` | `30s` | `gh pr checks --watch` 的刷新间隔；等待上限为 `limits.timeouts.ci` |
| `release.queue.timeLimit` | `1h` | 合并队列一次运行的时间上限 |
| `release.deploy.assumeDeployedAfter` | `1h` | 没有部署来源时，合并后多久视为已部署 |
| `release.deploy.manualPaths` | `[]` | 需要手动部署的路径模式，命中时提示联系负责人 |
| `release.deploy.<方法>` | 见文件 | 部署来源各方法的参数，按 `deploy_source/<方法>.yaml` 的 optionsSchema 校验 |
| `release.accept.windows` | 运行时来源 `24h`，静态巡检与任务外发现 `0s` | 各来源的观察期，`default` 为没列出的来源 |

关卡：`boundaries.gates.merge`(缺省 `auto_ci_passed`)；高风险路径取 `boundaries.protected.highRisk`，命中即固定人工(`high_risk_merge`)，除非每个命中的路径都在用户通过的那份方案的 `protectedTouches` 里(定案关卡逐条列给用户确认过)。两个关卡上用户通过后，本工具照常检查其余条件后合并；关卡上的通过不改变「没有用户接受的未通过项」这一条，交付带这类项时本工具不合并，待决定文档直接请人在 GitHub 上合并；合并的交接 `43-release.merge` 的 `approvals` 记下用了哪些确认(`point`、`at`、`note`、`option`，定案的另有 `planHash`、`paths`)。

## 设计依据

- 写操作都经 protocol/git(幂等键、状态不明先对账、不重试写操作、永远不直接写主分支)，发布这里不再自己判断「做过没有」：中断后重跑整条流程，做过的直接返回上次结果(protocol/git.md)。
- 同步用 `merge --no-ff`，不 rebase：已推送的历史不改写，PR 上的评审不失效。合并 main 后以合并进来的 main 为基准算改动哈希，main 只改了别的文件时不必重新审查(#5)；交集从 merge-base 算，否则修复自己的文件总被算进去。
- 冲突发布自己不取舍，退回编码解决：并行实施时几个 Issue 常在同一文件末尾各自加测试、各改页面的一处，合并时冲突但两侧并不矛盾，交给编码保留两侧的意图解决，之后照常自检、审查，不必等人。合并与合并提交由程序做(agent 不做 git 写操作)。两侧可能是同一功能的互斥实现，这时编码中止、交给人选；同一 Issue 反复冲突(到 `release.conflictReturns`)也交给人。交给人时冲突文件清单另存，因为用户 `git add` 后它们不再显示为冲突。退回次数按连续计：每一段冲突各自有上限，不因之前解决过的冲突累计到上限。
- 停在进行中的合并要在提交之前处理：提交按「工作区相对 merge-base 的改动」核对交付清单，合并进行中时 main 的改动也在工作区里，要么被当成交付之后新增的改动停下，要么连同冲突标记一起以修复的提交信息提交成合并提交，之后 main 已是祖先、同步什么都不做，不经审查就推送。
- 提交的文件清单取工作区实际改动并与交付核对：防止把交付之后出现的无关文件或临时文件一起提交。
- 推送前核对要推送的改动(相对合并进来的 main 的 diff 哈希)与交付时审查过的相同：分支上的提交不全是本工具做的，人在修复目录解决冲突后顺手 `git commit` 完成合并、或交付之后另有提交时，提交看到工作区干净，同步看到 main 已是祖先，都发现不了；main 只改了别的文件时哈希不变，不会误退。
- 提 PR 先查后做、只在描述变化时更新；审查结论同一 PR 同一轮只发一次：每次跟踪都会走一遍，否则 PR 上被同一段话刷屏。
- 自动合并要求 PR 头部等于本工具最近一次推送的 commit，合并带 `--match-head-commit`：判断与合并之间有人推送时宁可失败，不合进没审查过的提交([gh pr merge](https://cli.github.com/manual/gh_pr_merge))。评审只看每人最后一次结论：先请求修改后批准的不算阻断。
- 主分支要求必需检查或开了合并队列时交给 GitHub 原生自动合并，同一 head 只开一次：平台自己会等检查，重复开启没有意义([GitHub：Automatically merging a pull request](https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/incorporating-changes-from-a-pull-request/automatically-merging-a-pull-request)、[merge queue](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/managing-a-merge-queue))。
- 等 CI 用 `gh pr checks --watch`，结束即判断合并，而不是每次运行轮询一次：工作区暂停时 PR 原本会一直停在待合并(#7)。`--json` 时退出码不反映结果，按每项的 bucket 判断。
- 高风险路径(CI、依赖、迁移、权限认证、密钥配置)一律要人确认：这些改动影响构建、数据或权限，合并前的检查覆盖不到。确认在定案时给过的不再问第二次：定案关卡逐条列出要改的高风险文件，用户看过并通过，合并时再停一次只是重复；交付多碰了没列过的文件、或重新出方案后没再确认的，照旧停下。关卡上通过后由本工具合并：与自动合并同一方式(`--match-head-commit`、删除远程分支)，不让人再去 GitHub 上点一次。
- 验收只做实施做不到的：线上环境、数据与部署本身可能不同，上线后在最初报出问题的地方确认一次。按来源设观察期：确定性来源部署后即确认，运行时来源看一段时间是否再出现。观察期从发现部署成功的时刻算起：平台记的是部署开始的时间，开始到上线之间旧代码报的错不算回归。
- 部署来源是方法库(`deploy_source/`，每种来源一个程序加同名清单)，与采集的平台方法同一写法(collect/common/methods)：接新平台只加一对文件，参数、凭据条目名与限制写在清单里一眼看全，必填参数没写时按清单报缺少，不把 null 传给 gh 或平台。
- 写操作带做决定时的前置条件(protocol/git.md)：判断与执行之间仓库或 PR 被别人动过时，执行旧决定可能合进或推上没判断过的状态；过期不是要人处理的事，下次重新观察即可。
- 验收可以关掉(`release.accept` 不启用)：有的项目没有能回读的运行时来源，观察期只会白等；关掉后合并并部署即完成。
- 找包含合并提交的部署：先找同一 commit 且没被跳过的，再找以它为祖先的最早一次(等待窗口内多个提交由一次部署统一上线)；祖先关系查不到时只 fetch 一次；同一次运行内部署记录只读一次，多个 Issue 一起确认。没有部署来源时按合并时间加一段时间视为已部署，不无限等待。
- 回归时先提撤销 PR 再退回待修：线上先恢复，修复再重新来；撤销在单独的临时 worktree 上做，不碰修复目录；撤销 PR 不自动合并，交人决定。
- 清理只用 `worktree remove`(不加 --force)与 `branch -d`(从不用 -D)：有未提交内容或 git 认不出已合并时保留现场并说明。

## 不做什么

- 不调用模型；PR 描述、提交信息全部由程序拼出，不带任何 AI 署名；
- 不 rebase、不强推、不 `reset --hard`、不 `commit --amend`，不直接写主分支；
- 不自行解决冲突，不在没有用户确认时合并高风险路径的改动，不自动合并撤销 PR；
- 不另写平台对接：验收读 store 的问题与出现记录，平台由采集按自己的调度读取；
- 不做本机运行检查(启动服务、打接口、截图)：已并入实施的自检(`implement/check/runtime/`)。
