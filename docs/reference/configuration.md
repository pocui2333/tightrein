# 配置与运行说明

各项功能的启用条件、配置写法与运行行为。每个键的生效值与来源层用 `tightrein project config --key <键>` 查看。文中的命令在本仓库根目录执行。

## 本机工具

Semgrep(静态巡检、静态类复现检查与 `core/semgrep` 方法使用)可以装在本工具仓库内的独立虚拟环境中(`local/` 存放本机依赖，已被 `.gitignore` 忽略)：

```
core/.venv/bin/python -m venv local/semgrep && local/semgrep/bin/pip install semgrep
```

然后在本机用户配置 `~/.config/tightrein/config.yaml` 中指定命令(相对路径相对本工具仓库根目录)：

```yaml
tools:
  semgrep: {path: local/semgrep/bin/semgrep}
```

不配置时按 `runtime.tools.semgrep`(缺省 `semgrep`，按 PATH 查找)；`tightrein project config --key runtime.tools.semgrep` 可查看生效值与各层的值。

## 采集方法

每种方法配置了才启用，运行摘要列出未启用的方法与原因；编排的 `sources` 步骤按 `sources.<方法>.every` 运行到期的平台来源与项目探针。参数与示例配置见[方法目录](methods.md)。

| 方法 | 启用条件 | 命令 |
|---|---|---|
| 内部错误 `platform-errors` | `extensions.error-tracking`(`core/sentry`)，或 `extensions.log-platform`(`core/loki`)加 `sources.platform-errors.logQuery` 与 `extensions.log-parse` | `tightrein collect --probe platform-errors` |
| 访问日志 `access-log`(可选) | `sources.access-log.query` 与 `extensions.log-platform` | `tightrein collect --probe access-log` |
| 业务告警 `alerts` | `extensions.alert-source`(`core/alertmanager`) | `tightrein collect --probe alerts` |
| 项目探针 `project-probe` | `sources.project-probes` 中有登记 | `tightrein project probe new <名称>`、`tightrein project probe test <名称>`、`tightrein collect --probe project-probe [--select name:<名称>]` |
| `api-fuzz` | `target.baseUrl` 与接口描述(`extensions.spec-export`；框架不能导出时 `tightrein project spec draft` 起草) | `tightrein collect --probe api-fuzz` |
| `static`、`incidental` | 总是可用 | `tightrein collect --probe static` |

平台的只读令牌存在本机钥匙串(`security add-generic-password -s <条目名> -a <账号> -w`)，方法的 `keychainItem` 写条目名。项目探针的写法见[如何编写项目探针](../how-to/write-project-probe.md)。api-fuzz 的检查项按 `sources.api-fuzz.checks` 分级(服务器报错、越权、状态码不符开启；响应结构不符在接口描述由框架导出时开启；响应过慢、不支持的方法关闭)；`target.environment: production` 时只测 GET 与 `sources.api-fuzz.production.allow` 列出的路由。

## 静态巡检的基线审查

工作区的第一次静态巡检(还没有上次巡检的终点)用 `full` 档时自动做基线审查：不看 diff，把已有代码按目录模块分批(小模块合批)，每批由调用点 `collect.baseline-review` 路由到的模型整份审查一次，疑点再逐条取证。低级疑点与超出取证上限的疑点进入待处理清单：超出上限的由之后的巡检继续取证，低级的在需要时以 `tightrein collect --probe static --select pending:low`(或 `pending:<编号>`)取证。也可以随时显式运行基线审查：

```
tightrein collect --probe static --level baseline --dry-run
tightrein collect --probe static --level baseline
```

之后的日常巡检仍是增量(`incremental`)。每批的文件数与总行数上限、不审查的路径(缺省为测试夹具、生成文件、锁文件、二进制与文档)、取证的主张上限在 `sources.static.baseline` 下，项目可在 `project.yaml` 中覆盖，排除路径可用 `exclude+` 追加，例如测试代码 `exclude+: [tests/]`。运行摘要列出批数与每批的耗时、费用；当天预算(`stages.collect.budgetPerDay`)用尽时其余批次不再运行。`tightrein project config --key sources.static.baseline` 查看生效值。

## Issue 的去向：本地或 GitHub 镜像

Issue 缺省只在本地(`issues/` 下的 markdown 文件)。项目在 GitHub 私有仓库上讨论 Issue 时，在 `project.yaml` 写：

```yaml
issues:
  tracker: github              # 缺省 local
  github:
    repo: owner/name           # 可省略：取 project.repo 的 origin 远程
gates:
  mirror-writes: auto          # 本项目 Issue 镜像无需逐次确认；缺省 user(逐次确认)
```

每个未关闭的本地 Issue 在 GitHub 上有一个镜像，带一个状态标签 `tightrein:<状态>` 与一个类型标签 `tightrein:type:<任务类型>`(缺失时自动建立)与关键节点评论，PR 描述带 `Closes #<编号>`；拆分出的子任务是父 Issue 的 GitHub 子 Issue，并被前一个子任务阻塞。字段归属：开关状态以 GitHub 为准(在 GitHub 上关闭或重新打开会同步回本地)，标题、正文、标签与关系以本地为准，评论不会同步回本地。缺省只允许私有仓库(`privateOnly: true`)。镜像在 `issue create`、`approve`、`close`、`reopen`、`issue sync` 之后与每次 `run` 中对齐，失败不影响本地流程，下次重试；未同步项列在 `issue sync` 的输出与运行摘要中。`gates.mirror-writes` 只覆盖镜像的 gh 写操作，建分支、提交、推送、提 PR 的确认见下一节。gh 需已登录(`gh auth status`)。

## 输出语言、Issue 格式与严重度

```yaml
project:
  language: zh                 # 给人读的文字的语言：zh、en、ja 等，缺省 en
triage:
  severityGuide: |             # 可省略：本项目语境下的严重度说明，拼在分诊规则 P0 到 P3 之后
    个人项目……
```

`project.language` 决定分诊结论、Issue 标题与正文、修复计划与 PR 描述中的叙述、GitHub 评论的语言(代码、路径、标识符保持原样)；Issue 的小节标题与固定文字有 zh、en、ja 三套。Issue 标题为「[模块] 现象与后果」(上限 `thresholds.issue.titleMaxLength`)。Issue 文件是交接文档的 issue 类型：头信息有状态、严重度、任务类型、规模档、处理标签、来源、上游等；正文为结论、内容、需要决定、下一步、引用、历史，「内容」下依次为问题、影响、复现、原因、范围、注意事项、验收标准(复选框，固定三条在前：复现测试修复前失败修复后通过、现有测试全部通过、必须保持不变的行为)、修复方向；GitHub 镜像中代码位置是取证 commit 的永久链接，完整证据折叠。严重度由取证按 `skills/triage/references/severity.md` 与 `severityGuide` 判定并写理由。Issue 状态为待决定、待修、进行中、待合并、完成、取消六种。旧版式的 Issue 文件照常可读，用 `tightrein issue rerender [<编号>...]` 转为当前版式(不调用模型，缺的字段为「—」；完整格式先 `tightrein problem retriage <问题编号>`)。

## 审批关卡、预算与无人值守

哪些事项交用户决定集中在 `project.yaml` 的关卡表 `gates`(`tightrein project config --key gates`)，缺省全部为 `user`(逐次确认、用户放行与合并)。个人项目可以开启：

```yaml
gates:
  issue-approve: auto     # 简单的 Issue 按 autonomy.approve 自动放行；用户需求创建即放行
  plan-confirm: auto      # B 通道的简单计划按规则自动确认(C 通道一律交用户)
  fix-session: auto       # run 以非交互任务修复已放行的 Issue
  release-writes: auto    # 建修复分支、本地提交、合并主干、推送、创建或更新 PR、发评审评论、提撤销 PR 直接执行
  mirror-writes: auto     # GitHub Issue 镜像的写入直接执行
  merge: auto             # 满足自动合并条件的 PR 自动合并
release:
  mergeMethod: squash     # 或 merge
  autoMergeBlockPaths+: [deploy/]   # 可选：在核心缺省(CI 工作流、依赖清单、迁移、权限认证、密钥配置)之外追加
budget:
  perRunUsd: 5            # 每次运行；null 为不限
  perDayUsd: 20
  perWeekPercent: 50      # 每周上限写为订阅额度的百分比……
  subscriptionWeekUsd: 200  # ……与订阅额度每周相当的金额(换算值)；也可直接写 perWeekUsd
loop:
  maxActiveFixes: 1       # 同时处于修复阶段的 Issue 上限
```

- 必须交用户的五项不能改为 `auto`(配置校验拒绝)：改动命中 `release.autoMergeBlockPaths` 的合并(写决策简报 `data/fixes/<编号>/merge-decision.md`)、删除分支或数据、计划改动 `credentialFiles` 或 `review.riskRules.authz` 命中的文件、超出单个任务上限、待决定的 Issue。
- 直接执行的操作照常经待确认操作表与执行器，日志、事件、幂等与失败处理与用户确认后执行相同；冲突解决、放弃合并仍需用户确认，本工具从不 force push、`reset --hard`、rebase、amend，也不向主分支直接提交或推送。
- 自动合并的条件：修复自检通过、评审没有阻断项、PR 阶段适用的检查全部通过、CI 必需检查没有失败、PR 不是草稿、没有人请求修改、PR 头部是本工具推送的 commit。主分支有分支保护或规则集要求必需检查时开启 GitHub 原生自动合并(`gh pr merge --auto`)；否则另要求 PR 无冲突，由本工具合并。不满足时照常等待，原因列在每日汇总中。AI 评审结论以 PR 评论写入(`release.reviewComment`)，只作说明。
- 撤销：部署后确认发现回归时自动提一个撤销该合并提交的 PR(不合并，交用户决定)，Issue 回到待修；也可以手动 `tightrein release revert <编号> --reason <回归现象>`。
- 预算到达时运行在当前步骤后停下(不再执行调用模型的步骤)，执行器也不再启动新任务；熔断：无人值守推进中同一对象连续失败 `thresholds.loop.breakerFailures` 次或同一步反复没有进展 `breakerRepeats` 次(缺省都为 3)时转为待决定。
- 实时查看：在另一个终端运行 `tightrein watch`，固定 26 行原地刷新(间隔 `loop.watchIntervalSeconds`，缺省 2 秒，`--interval` 覆盖)：运行状态与进程号、当前模型、今日 token 用量、最新一份交接文档(路径、时间与结论)、流程脉络(跳过与等待的步骤折叠)、上一步/当前步/下一步的明细(状态、耗时、token 用量、模型、备注)与最近 3 条事件(带表头)，底栏给出建议的命令。只读数据库与工作区文件，不调用模型。按 q 退出、r 立即刷新；`--once` 只输出一帧。
- 待用户决定的事项集中在收件箱：`tightrein status` 列出每件与推荐做法；每天一份汇总 `data/reports/daily-<日期>.md`(运行摘要与收件箱合为一份)，每次运行后发一条 macOS 通知；GitHub 镜像中对应 `needs-decision` 标签。
- 触发：`tightrein project schedule install` 装的 launchd 任务每 15 分钟(`schedule.tick`)唤醒 `tightrein tick`：工作日 `schedule.runAt`(缺省 09:00)做完整运行，其余时刻只检查事件(主分支有新提交时增量巡检，有新部署时部署后确认)。无人值守推进先修立即修、再修排期修。
- 暂停：`tightrein pause`(全局)或 `tightrein pause --workspace <工作区>`，不再发起新的运行，进行中的步骤完成后停下；`tightrein resume` 恢复。

## 接入新项目

```
tightrein project init --workspace workspaces/<项目> --repo <仓库路径>
```

新工作区先处于接入中：只做只读的事(识别技术栈、试连接已配置的平台与扩展、在主分支上自检检查命令)，不修代码、不提 PR、不建 Issue。清单写在工作区的 `onboarding.md`，每项注明自动完成、需要回答(附推荐答案)或失败待处理；`init` 在终端逐项提问，回车采用推荐。也可以 `tightrein project answer <项> --recommended|--skip|--value <值>`、通过 loop skill 用自然语言回答，或直接改 `project.yaml`(在 `onboarding.md` 的数据块中把某项改为 done 也算回答)，下次检查采纳。`tightrein status` 显示「接入中，还差 N 项需要回答」，每日汇总有「接入中的项目」。清单全部完成、试连接都成功、检查命令通过后转为运行中。已有工作区视为运行中，可用 `tightrein project check` 生成一次清单检查。

## 分支、提交、PR 的格式与部署跟踪

分支、提交与 PR 先按项目约定，没有时用通用格式。优先级：`project.yaml` 的 `git.conventions`(格式串，占位符见 `core/tightrein/domain/release_format.py`) > 项目中写明的约定(PR 模板、commitlint、`CONTRIBUTING.md`、`AGENTS.md`、`CLAUDE.md` 中带占位符的模板；拿不准时不采用) > 通用格式。通用格式：分支 `<类型>/<Issue 编号>-<简称>`(类型由任务类型映射为 `feature`、`bugfix`、`hotfix`、`chore`，`git.branchTypes`)；提交 `<类型>(<范围>): <一句话>`，空一行后写原因(Conventional Commits，`git.commitTypes`)；PR 标题一句祈使语气的完整话，描述为问题、为什么这样做、局限，之后是关联，最后是程序生成的验证结果。需要个人前缀的项目写 `git.personalPrefix: true`，前缀取本机用户配置的 `branchPrefix`。

```yaml
git:
  conventions:
    branch: "{prefix}{type}-{slug}"     # 可选：项目自己的分支格式
    commit: "{type}: {summary}"         # 可选：项目自己的提交格式
  personalPrefix: true
  branchTypes: {default: fix, feature: feat}
extensions:
  deploy-source:
    use: core/vercel                    # 或 core/github-actions、core/github-deployments
    options: {projectId: prj_xxx, keychainItem: tightrein.<项目>.vercel}
```

部署信息由 `extensions.deploy-source` 选用的只读方法读取：`core/github-actions`(部署工作流的运行，`options.workflow`)、`core/github-deployments`(GitHub Deployments，可选 `options.environment`)、`core/vercel`(Vercel API，令牌放在钥匙串条目 `options.keychainItem` 中，`security add-generic-password -s <条目名> -a vercel -w`)。都没有配置时，合并后经过 `release.deploy.observationHours`(缺省 24)视为已部署，再做部署后确认。

## 分诊的处理标签与修复通道

分诊一次取证同时给出判定、严重度、价值判断、任务类型(缺陷、安全、数据、前端、功能、重构、依赖、文档配置)、预估改动(文件与行数，不含测试)与修复方向；证伪复核只在高风险时运行(`triage.refute`)。处理标签由决策树 `triage.treatment.rules` 给出：立即修、排期修(建 Issue)、观察(不建 Issue；再出现或严重度升级时重新分诊，仍判为观察时升为排期修)、不修(直接关闭)。规模档按 `thresholds.tiers`：微(1 个文件、30 行以内)、小(3、100)、中(10、500)、大(30、3000)，超过为超限。

修复按「类型 × 档」的流程表 `fix.lanes` 分三条通道(项目可在 `project.yaml` 中覆盖)：

| 类型 | 微 | 小 | 中 | 大 |
|---|---|---|---|---|
| 缺陷、前端、功能、重构、文档配置 | A | A | B | C |
| 安全、数据、依赖 | B | B | B | C |

- A 快速：Issue 即计划，写复现测试(在未修改的代码上必须失败)、写代码(同一会话的第二轮)、收集结果，轻量评审(微档且检查都通过时跳过)；实际改动超出当前档时转 B，测试保留。
- B 标准：需要时勘察，fix-planner 出计划，程序重评规模档、超出单个 PR 上限时拆分；确认计划后同 A，安全、数据类的复现测试由另一会话的 repro-writer 写；轻量评审，高风险另加深度评审。
- C 大任务：先出整体方案并拆成子任务，一律交用户确认，确认后其余子任务生成为排队的子 Issue。超限的 Issue 不接，转待决定并建议拆分需求。

单个 PR 的改动量上限缺省 5 个文件、200 行，都不含测试文件(`thresholds.change.maxFiles`、`maxLines`)；实际改动超出时交回收敛一次，仍超出时停下交用户，不放宽上限。修改轮数上限 `thresholds.fix.reviewRounds`(缺省 3)。每一步的进度与产出在 `data/fixes/<编号>/`(`progress.md`、`plan.md`、`result.md`、`review-<轮>-<模式>.md` 等)。

## 复现测试与用户的决定

修复的第 5 步写一个表达验收标准的复现测试：一个项目测试文件与运行它的命令(开头须为 `checks.commands` 中某条命令的允许前缀，例如 `<python> -m pytest -q tests/test_x.py::test_y`)。测试文件须匹配 `project.yaml` 顶层的 `testPaths`(没有配置时写不了复现测试，Issue 转为待决定)。本工具在未修改的代码上运行它：缺陷与新功能必须失败，重构的表征测试必须通过；缺陷类测试在未修改的代码上就通过时问题不成立，Issue 退回分诊。合格的测试登记为本 Issue 的测试类复现检查，修复后须通过，并随修复提交进 PR。退出码在 `regressions.testFailureExitCodes`(缺省 `[1]`)中为测试失败，其他非零(收集错误、找不到用例)不算。测试失败时按 `regressions.testFailurePatterns` 归因：环境问题(连接被拒、超时)就地重试 `regressions.envRetries` 次，仍失败则停下报环境问题；基准版本上测试本身有问题(语法、导入、夹具)的交回重写；测试登记了预期异常签名(`expectedSignature`)时，基准版本上的输出须包含该签名才算复现。文档与配置类不写复现测试(`fix.repro.skipTypes`)。建修复分支后本工具先在基准版本上运行一次项目检查，不通过说明配置或环境有问题，Issue 转为待决定。

```yaml
testPaths: ["tests/"]
checks:
  commands:
    - {name: pytest, cwd: ., command: "python -m pytest -q -p no:cacheprovider"}
```

`fix plan --note`、`fix confirm --reject --note` 与 `--accept-design` 记录在 `data/fixes/<编号>/decisions.json`，之后每次出计划与实施都交给全部修复角色。写不出合格的复现测试时 Issue 转为待决定：补充线索后 `fix start <编号> --force`、`fix apply <编号>`。

## GitHub 网络失败的换路重试

访问 GitHub 的 git 远程命令(push、fetch、pull、ls-remote)、gh 与第三方 skill 下载遇到网络类错误(超时、连接重置、TLS 握手失败、无法解析主机等，模式在 `runtime.network.errorPatterns`)时，自动换另一条路重试一次：按 `network.noProxy` 直连的改为经代理，经代理的改为直连(需要本机配置了 `network.proxy` 或环境中有代理变量)。认证失败、推送被拒等不重试。每次换路写事件，运行摘要「网络换路」列出；`admin install`、`third-party` 等命令把换路写到错误输出。

## 本机用户配置：模型别名、路由表与网络代理

选模型只有两张表，写在 `~/.config/tightrein/config.yaml` 的顶层一次，所有工作区生效；项目需要不同的模型时在 `project.yaml` 中写同样的两段，按项覆盖(同名别名、同一路由键整体替换用户配置的那一项，architecture/01 5.1、5.3)：

- `models`(模型别名)：一处定义工具(`tool`，必填)、模型(`model`，省略时用工具自己的缺省模型)、推理强度(`effort`，可省略)与每百万 token 的价格(`inputUsdPerMTok`、`outputUsdPerMTok`，美元，两项同时写或都不写；只在工具不返回费用时用于估算)。同一工具的同一模型在不同别名中价格不同时报配置错误；
- `routes`(路由表)：调用点 → 别名。没写的调用点用 `default`；核心不给任何别名与路由，`routes.default` 与对应调用点都没写时，运行到该调用点报出调用点名，提示写 `routes.default` 或 `routes.<调用点>`。

推荐的写法(Claude 做主力，agy 做只读判断)：

```yaml
models:                     # 模型别名：一处定义工具、模型、推理强度与价格(价格只在工具不返回费用时用于估算)
  flash:      {tool: agy,    model: gemini-3.8-flash-low,  inputUsdPerMTok: 0.5, outputUsdPerMTok: 3}
  flash-high: {tool: agy,    model: gemini-3.8-flash-high, inputUsdPerMTok: 0.5, outputUsdPerMTok: 3}
  opus:       {tool: claude, model: opus, effort: high,    inputUsdPerMTok: 4,   outputUsdPerMTok: 20}
  opus-mid:   {tool: claude, model: opus, effort: medium,  inputUsdPerMTok: 4,   outputUsdPerMTok: 20}
  fable:      {tool: claude, model: fable}
routes:                     # 调用点 → 别名；没写的调用点用 default
  default: opus
  fix.scout: flash-high
  fix.planner: opus
  fix.planner.high-risk: fable
  fix.review.deep: flash-high
  triage.refuter: flash-high
network:
  proxy: http://127.0.0.1:8118
  noProxy: [github.com, api.github.com, codeload.github.com, objects.githubusercontent.com, raw.githubusercontent.com]
```

调用点是固定的清单，每个对应一处真实的模型调用(定义在 `core/tightrein/config/routes.py` 的 `CALL_POINTS`)；名称为 `<环节>.<角色>`，角色名以环节名开头时去掉这一段(`fix-scout` → `fix.scout`)。写了清单以外的键或不认识的条件时，加载配置即报出完整键名：

| 调用点 | 说明 | 可用条件 |
|---|---|---|
| `collect.static-review` | 静态巡检的增量审查 | |
| `collect.baseline-review` | 静态巡检的基线审查，每批一次 | |
| `collect.variant-scan` | 以一个缺陷模式为种子的全量扫描 | |
| `collect.claim-verifier` | 静态巡检候选主张的取证 | |
| `collect.spec-drafter` | 没有自动导出时起草接口描述(`project spec draft`) | |
| `triage.claim-verifier` | 分诊取证 | |
| `triage.refuter` | 证伪复核；须与 `triage.claim-verifier` 使用不同的工具或模型 | |
| `triage.dedup` | 分诊查重 | |
| `fix.scout` | 勘察根因位置、联动方与可复用实现 | `frontend` |
| `fix.planner` | 出修复计划 | `high-risk`、`large` |
| `fix.frontend-designer` | 计划含前端文件时的前端设计说明 | |
| `fix.executor` | 写复现测试与写代码(同一会话的两轮) | `high-risk` |
| `fix.repro-writer` | 安全、数据类的复现测试(与写代码不同的会话) | |
| `fix.review.light` | 轻量评审 | |
| `fix.review.deep` | 深度评审(盲审)；须与 `fix.executor` 使用不同的工具或模型 | |
| `fix.session` | 修复的终端交互会话 | |
| `verify.screenshot-review` | 合并前验证的截图查看 | |
| `learn.lesson-writer` | 经验撰写与同类条目的矛盾比对 | |
| `learn.rule-writer` | 缺陷变规则 | |
| `learn.improvement-writer` | 改进建议 | |
| `learn.knowledge-curator` | 知识写入的去重判断(由写入知识的环节发起) | |
| `eval.judge` | 评测的模型评审 | |

条件只有三种，写成调用点的后缀，只能用在上表列出该条件的调用点上：

| 条件 | 含义 |
|---|---|
| `high-risk` | 修复的风险判定为高风险(`review.riskRules` 命中，或分诊与计划标记)；`fix.executor` 按已确认计划中的判定 |
| `frontend` | 改动涉及前端文件(按 `stages.fix.roles.frontend-designer.paths` 判定)；勘察时看 Issue 的根因位置，没有已知位置时不带 |
| `large` | 大任务(C 通道) |

带条件的调用依次取 `<调用点>.<条件1>`、`<调用点>.<条件2>`…、`<调用点>`、`default` 中第一条写了的路由；`fix.planner` 的条件次序为 `high-risk`、`large`。证伪复核与深度评审按解析后的工具与模型检查独立性：`triage.refuter` 须与 `triage.claim-verifier` 不同，`fix.review.deep` 须与 `fix.executor` 及其写了路由的条件变体(例如 `fix.executor.high-risk`)不同，不满足时报出要改的 `routes.` 行。命令行的 `--runner` 改写所有调用点的工具(工具与别名的不同时不沿用别名的模型与推理强度)，`--model` 改写模型。

`tightrein project config --routes` 列出每个调用点(及写了路由的条件变体)解析出的别名、工具、模型、推理强度与生效的路由行，例如 `fix.planner.high-risk → fable  claude/fable  [routes.fix.planner.high-risk (用户配置)]`；`tightrein project config --key routes`、`--key models` 列出各层中的值。

旧的写法(`agents` 段、`capabilities`、`roleCapabilities`、`defaultTool`、`evaluation.judge`，以及 `stages.<环节>` 中的 `tool`、`model`、`capability`、`refuter`、`session`)已经删去，配置中还有时加载即报出该键与新写法。`stages.<环节>.roles|tasks.<角色>.limits`、`review.light|deep.limits`、`screenshotReview.limits` 等上限与 `paths` 照常生效，它们不是选模型的设置。

Antigravity CLI(`agy`，Gemini CLI 的继任者，`agy models` 列出可用模型)在无人值守模式下按它自己的命令白名单放行命令，`tightrein admin install` 为它补上只读的搜索命令；只读任务在沙箱中不能写文件，白名单以外的命令被拒绝，检查由核心之后执行；它也不支持交互会话，`routes.default` 指向 agy 的别名时把 `routes.fix.session` 指向 claude 或 codex 的别名。详见 architecture/02 2.5。

修复计划预估改动的文件中有前端文件(`stages.fix.roles.frontend-designer.paths`，核心缺省为常见前端扩展名与目录，项目可写 `paths` 覆盖或 `paths+` 追加)时，`frontend-designer` 在实施前给出前端设计说明，`fix-executor` 按它实现。

agy 做勘察时用白名单中的 `git grep`、`git log` 等只读命令搜索代码与提交历史；没有执行 `tightrein admin install` 时只能逐个打开文件，又慢又费 token。

价格以各产品当时的价目为准。配置了 `network.proxy` 后，tightrein 启动的子进程(agent 工具、git、gh、Schemathesis、Playwright、Semgrep、扩展、本机服务)与核心自己的 HTTP 请求都走代理，`noProxy` 与本机回环地址直连；没有配置时沿用当前进程环境中的代理变量。`tightrein project config --key network`、`--key stages.fix` 可查看生效值与来源层。

第三方 skill 也可以只放在本工具仓库内(下载缓存在 `local/third_party-cache/`，链接在 `skills/<名称>`，都已被 `.gitignore` 忽略)，不写任何 agent 工具的目录；`lock` 与下载需要访问 GitHub(gh 已登录)：

```
(cd core && .venv/bin/pip install -e .)
core/.venv/bin/tightrein admin third-party lock
core/.venv/bin/tightrein admin install --repo-only --dry-run
core/.venv/bin/tightrein admin install --repo-only
core/.venv/bin/tightrein admin install --repo-only --check
```

撤销用 `tightrein admin uninstall --repo-only`。
