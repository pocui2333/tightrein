# git 规范

git 与 gh 的读写、worktree、分支提交与 PR 的格式。程序在同目录的 `git/`：`git.py`(Git)、`github.py`(GitHub)、`worktrees.py`、`format.py`。协议层定规则，settings 放取值，项目自己的约定(commitlint、PR 模板、CONTRIBUTING 等)留在项目仓库，运行时读取，不复制进 settings。

## 调用方式

**是什么**：git、gh 怎么启动、出错怎么分类。

**怎么做**：

- 以参数数组经 `process.py` 启动，不经 shell；工作目录显式传入；
- 固定加 `GIT_TERMINAL_PROMPT=0`(ssh、凭证助手不会卡在等输入)与 `LC_ALL=C`(英文输出才能按格式解析)；gh 另加 `GH_PROMPT_DISABLED=1`；
- 访问远程的 git 命令(fetch、push、pull、ls-remote)加低速断开：`-c http.lowSpeedLimit=<字节> -c http.lowSpeedTime=<秒>`，传输速度持续低于下限即断开；再加整体时限。网络卡住时 fetch 不会拖死整轮；
- 输出一律用 `-z`/`%x00` 分隔并加 `core.quotePath=false`：路径中的空格、换行与中文不需要转义；改名与二进制文件(numstat 为 `-`)单独识别；
- 错误只按退出码分类，不解析错误文字：访问远程的 git 命令退出码 128 或超时为 `NetworkError`；gh 退出码 4 为未登录(`AuthError`)；其余非零为 `CommandFailed`；可执行文件不存在为 `ProgramNotFound`；
- 错误里的命令与错误输出先经 `security.py` 的脱敏器(gh、git 的错误输出可能带 token 或带凭据的远程地址)，只留尾部若干行；
- 只读查询(fetch、gh 的查询)遇到网络错误按 `limits.retry` 退避重试；写操作(push、pr create 等)绝不自动重试：重试写操作可能重复推送或重复建 PR；
- 提交信息、PR 描述、评论、Issue 正文从标准输入传入(`git commit -F -`、`gh … --body-file -`)，不放在命令行参数里：避免转义问题，正文也不出现在进程列表与日志里；
- 所有 gh 命令都带 `--repo <owner/name>`，不依赖工作目录推断仓库；仓库从 origin 地址(https、ssh、scp 三种形式)解析(`repo_slug`)。

**在哪配置**：`limits.timeouts.git`(git 整体时限)、`limits.timeouts.gitLowSpeedBytes`、`limits.timeouts.gitLowSpeedTime`、`limits.timeouts.command`(gh)、`limits.retry`、`tools.gh.path`。

**缺省值**：

| 项 | 缺省 | 出处 |
|---|---|---|
| git 整体时限 | 10m | limits.md「超时」 |
| 低速断开 | 每秒低于 1000 字节持续 60s | limits.md「超时」；git 文档 `http.lowSpeedLimit` |
| gh 时限 | 5m | limits.md「超时」 |
| 只读重试 | 2 次，间隔 1s、4s(按 4 倍增长，不超过 20s) | limits.md「重试与退避」 |

## 写操作幂等

**是什么**：提交、合并主干、推送、建删 worktree 与分支、提 PR、合并 PR、评论、同步 GitHub Issue 只做一次。

**怎么做**：

- 每个写操作都经 `store/tables/operations.run_once`，幂等键为「对象:步骤:操作:内容哈希」(`WriteScope` 给出对象与步骤)；已完成的直接返回上次结果；
- 上次执行被中断(键处于进行中，`InProgress`)时先按实际状态对账：确认做完就补记完成，否则删键重做：

| 操作 | 内容哈希取自 | 对账：算做完了的条件 |
|---|---|---|
| 提交 | 改动的 diff_hash(相对给定基准)与文件清单 | 这些文件相对 HEAD 已没有未提交的改动 |
| 合并主干 | 分支与目标 commit | 没有进行中的合并，且目标 commit 已是 HEAD 的祖先 |
| 推送 | 分支与 HEAD | `origin/<分支>` 等于 HEAD |
| 建 worktree | 路径、分支、基准 | 目录在且分支对应(已存在且对应时直接复用，中断后重来不报错) |
| 删 worktree、删分支 | 路径或分支名 | 已不在 |
| 提 PR | 分支、目标、标题、描述 | 该分支已有打开的 PR(先查后做：有打开的 PR 就只在描述有变化时 `gh pr edit`) |
| 合并 PR | 编号、head、方式 | PR 已合并 |
| 评论 | 编号与正文 | 已有同样正文的评论 |
| 建 Issue | 本地标记 `<!-- tightrein:<项目>:<编号> -->` | 远端能按标记找到 Issue |

- 前置条件：做决定时用 `Git.state`(分支、HEAD、相对基准的 diffHash、origin/<主分支>)或 `GitHub.pull_state`(该分支打开的 PR)记下观察到的状态，作为 `expected` 传给 commit、merge、revert、push、create_pr；真正执行前以同样的项重新观察，任何一项不同即抛 `Stale`、不执行(键删去)。复核放在查键之后：已完成与对账补记的直接返回，不复核；
- 程序永远不直接写主分支：提交、合并、推送作用于主分支或游离 HEAD 时一律拒绝(`MainBranchRefused`)；
- fetch 只更新远程跟踪分支，按只读处理，不记幂等键；需要 fetch 的操作在判断时 fetch，执行时不再 fetch。
- 命令输出按步落盘：`WriteScope.raw` 给出这一步的原始输出目录时(`Runtime.scope` 对登记了文件序号的步骤自动给出)，一次写操作期间(含复核与对账)的每条 git、gh 命令按顺序存为 `vcs/<对象>/<操作>/step-N.log`(命令、退出码或终止原因、标准输出、错误输出尾部，均已脱敏)；编号接着已有的，重做不覆盖上次的；任何一条失败即停，最后一个文件就是失败的那一步。

## 提交、同步主干与推送

- 改动哈希(`diff_hash`)逐文件记「路径 + 基准中的模式与 blob + 工作目录中的模式与 blob」，未跟踪文件用 `hash-object` 计入：与改动是否已提交无关(复现测试是新文件，提交后从未跟踪变已跟踪，哈希不变)，所以既能作提交的幂等键，也能判断评审过的改动是否变了；
- 比较基准(`review_base`)取 HEAD 与主干的最近公共祖先：合并过主干后它就是最近一次合并进来的主干版本。主干只改了别的文件时改动哈希与评审时相同，只需重新验证；改到同一文件或解决过冲突时才重新审查；
- 同步主干一律 `merge --no-ff`，不用 rebase、`push --force`、`reset --hard`、`commit --amend`：已推送的历史不改写，PR 上的评审不失效；
- 冲突时合并停在进行中，抛 `MergeConflict` 列出冲突文件，不自行取舍；
- 推送结果按 `git push --porcelain` 的引用标记判断(`!` 为被拒，`PushRejected`)，不看错误文字；被拒时先同步主干，绝不强推；
- 合并 PR 用 `gh pr merge --match-head-commit <判断时的 head> --delete-branch`：判断后有人再推送就合并失败而不是合进未审查的提交；带 `--repo` 时 gh 只删远程分支，本地分支与 worktree 由清理负责；
- 必需检查按 `gh pr checks --required --json` 每项的 bucket 判断，退出码不反映结果；免费账号的私有仓库查规则集返回 403，视为没有规则集。

## worktree

**怎么做**：

- 修复 worktree：先 fetch，从 `origin/<主分支>` 用 `worktree add -b` 新建，不动用户的主工作区；已存在且分支对应时直接复用；项目要求的被忽略目录(如 `.claude/`、`node_modules`)从主工作区软链接进去，否则测试与 agent 跑不起来；
- 只读 worktree：游离 HEAD，不建分支；切换时目标 commit 本地已有就不 fetch，fetch 失败写明原因；被忽略的构建产物不算不干净；不干净说明有东西写入，停下不清理，保留现场；
- 只读锁定：agent 运行期间去掉只读 worktree 全部目录与文件的写权限(不跟随符号链接)，先写标记文件(原权限、进程号、主机)再改权限，中途崩溃也能按标记恢复；恢复失败保留标记下次重试；启动时只恢复本机上持有进程已不在的标记。这是工具只读权限与快照比对之外的又一层；
- 清理：先确认 worktree 干净，再 `worktree remove`(不加 `--force`)、`branch -d`(只删已合并的，从不用 `-D`)、`fetch --prune`；目录与本地分支都已不在的算完成。

**在哪配置**：主分支取工作区 `project.mainBranch`；worktree 与标记文件的路径由 `store/files/layout.py` 给出；要链接的目录由调用方按项目设置传入。

## 分支、提交与 PR 的格式

**是什么**：分支名、提交信息、PR 标题与描述怎么写(`format.py`)。

**怎么做**：

- 格式怎么确定(每项记下来源，供 PR 与运行摘要说明)：
  1. settings 中缺省层之上写明的 `git.branch`、`git.commit`、`git.prTemplate`；接入时从历史推断并经确认的结果也写在这里；
  2. 项目仓库写明的约定：PR 模板文件；commitlint 继承 config-conventional 时提交采用 Conventional Commits；CONTRIBUTING、AGENTS、CLAUDE.md 中同一行写了分支(或提交)并在反引号中给出带占位符的模板；占位符全部认得出、全部文档只有一个不同模板时才采用，拿不准就不采用；
  3. 通用格式(`settings/defaults.json` 的 `git` 段)；
- 从历史推断(`infer`)：去掉 Merge 提交与主分支名，达到最小样本数与比例才给出结果；只交接入时确认，不自动采用；
- 格式串只允许已知占位符，写错立即报错：分支 `{prefix}`、`{type}`、`{issue}`、`{slug}`；提交 `{type}`、`{scope}`、`{summary}`；PR 标题 `{type}`、`{summary}`；Issue 编号不补零，简称转小写短横线；立即修的 P0 分支类型为 `hotfix`；
- 一个提交只做一件事：提交的文件清单取工作区的实际改动，第一行按格式串，空一行后写为什么这样改；
- 不带 AI 署名：提交信息与 PR 描述完全由程序拼出，不加 `Co-Authored-By`、`Generated with` 之类的行；`Git.commit` 发现署名行即拒绝；个人前缀不能是 AI 或工具名(ai、claude、codex、agy、copilot 等)，项目要求个人前缀而没有配置时停下并说明；
- PR 描述不调用模型，不分固定小节：问题、做法、局限三段，之后是关联(`Closes #<编号>`)，最后是程序生成的验证结果；项目有 PR 模板时按模板标题(中英日关键词)把同样的内容放进对应小节，模板原有的复选框保留，模板没有对应标题的内容附在后面不丢，一个标题都认不出时在模板后附上通用描述。

**在哪配置**：`git.branch`、`git.commit`、`git.pr`、`git.types`(Issue 种类 → 分支与提交的类型)、`git.branchPrefix`(个人前缀，本机配置)、`git.prTemplate`(可选，仓库内的模板路径)。

**缺省值**：

| 项 | 缺省 |
|---|---|
| 分支 | `{prefix}{type}/{issue}-{slug}` |
| 提交 | `{type}: {summary}` |
| PR 标题 | `{summary}` |
| 类型 | bug → fix，feature → feat |
| 个人前缀 | 无 |
| 历史推断 | 读最近 50 条提交，少于 10 个样本不推断，统一比例达到 0.8 才给出 |

## 设计依据

- 参数数组不经 shell、正文走标准输入：路径与提交信息里的空格、引号不会被解释成命令，正文不进进程列表；
- 只按退出码分类：错误文字随语言与版本变化，退出码稳定(gh 退出码见 [gh 文档：Exit codes](https://cli.github.com/manual/gh_help_exit-codes))；
- 低速断开代替只给 fetch 一个短时限：大仓库首次 fetch 本来就慢，按速度判断卡住比按总时长准([git-config：http.lowSpeedLimit](https://git-scm.com/docs/git-config#Documentation/git-config.txt-httplowSpeedLimit))；
- 写操作不重试、带幂等键、状态不明先对账：重试写操作可能重复推送或重复建 PR([Stripe：Idempotent requests](https://docs.stripe.com/api/idempotent_requests)、[timeouts, retries and idempotency](https://aipromptgear.com/agent-systems/tool-timeouts-retries-and-idempotency-for-ai-agents/))；
- 改动哈希与提交无关、以合并进来的主干为基准：合并主干后不必重复评审(#5)；
- `--match-head-commit`：判断与合并之间有人推送时宁可失败([gh pr merge](https://cli.github.com/manual/gh_pr_merge))；
- 约定的优先级与「拿不准不采用」：项目写明的约定最可靠，猜错一次提交就要人改；[Conventional Commits](https://www.conventionalcommits.org/)、[commitlint config-conventional](https://github.com/conventional-changelog/commitlint/tree/master/%40commitlint/config-conventional)。
