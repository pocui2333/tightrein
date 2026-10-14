# GitHub 上已有的 Issue(collect/github_issues)

## 是什么

把目标项目 GitHub 上别人提的、打开着的 Issue 导入流水线：每个转成一条信号，走正常的去重与评估。评估之后按结论在
GitHub 上回应(重复的、不成立的、不修的关掉，成立的由本地 Issue 接手原编号，修复的 PR 用 `Closes #<编号>` 关掉)。
不调用模型。

## 什么时候跑

不是接入清单里的模块(加进清单会让已有的 `setup.json` 通不过校验)：跟着 GitHub Issue 同步走，接入清单的
`release.github_issues` 为 enabled 且项目的 origin 是 GitHub 仓库时，由 `collect.sources` 加在来源最后；
间隔 `schedule.every."collect.github_issues"`(缺省 1h)。

## 流程

`source.collect(runtime)`：`gh issue list --state open`(`GitHub.open_issues`) → 去掉编号已是某个本地 Issue 镜像的
(自己建的、已接手的)与已处理过的 → 每个转成一条信号 → 记下处理过的编号。

## 输入与输出

信号：

| 字段 | 取值 |
|---|---|
| check_type | 带 `featureLabels` 中任一标签为 `request`(需求)，其余为 `report`(缺陷报告) |
| location | 正文中第一个 `目录/文件.扩展名:行号`(反引号里的或裸写的；网址里的不算)；没有为 null |
| message | 标题 |
| occurred_at | Issue 的创建时间 |
| commit | 创建时间之前最近一次成功部署的 commit |
| group_key(指纹) | `github-issue:<编号>`：一个 GitHub Issue 一个问题 |
| deterministic | 两种都为真 |
| evidence | `number`、`url`、`labels`、`body`(先脱敏再截到 `bodyChars`) |

已处理记录：state 表 `collect.github_issues:seen`(编号列表)。覆盖范围永远为空。

评估中(`assess/assess.py`、`assess/issue/remote.py`)：需求查重之后不取证，直接写成待修的用户需求；缺陷报告照常取证。
写成 Issue 时镜像直接记成原编号(`extra.github`)，同步不再新建。事务提交之后按去向在 GitHub 上回应：

| 去向 | 回应 |
|---|---|
| 写成 Issue 并接手 | 不动，等修复的 PR 关闭 |
| 追加到已有的 Issue、并入别的问题 | 「和 #N 是同一个问题，在那边一起处理。」，以 not planned 关闭 |
| 不成立 | 「看了下代码，这里没有问题：<取证给的现象来源，第一句>」，以 not planned 关闭 |
| 不修、命中取舍 | 「这个先不改：<一句理由>」，以 not planned 关闭 |
| 主干上已修 | 「已经在 <commit 前 7 位> 修好了。」，以 completed 关闭 |
| 证据不足、观察、转人工 | 不动，留着打开 |

回应的结果(或 GitHub 的错误)记进问题的 `extra.assess.github`；出错不让评估失败。

## 配置

`controls."collect.github_issues"`：`featureLabels`(算需求的标签，缺省 enhancement、feature，不分大小写)、
`bodyChars`(正文摘录的字数，缺省 4000)、`sourceTimeout`(缺省 10m)。

## 设计依据

- 不进接入清单：清单要求每个模块都明确列出，新增一项会让所有已接入项目的 `setup.json` 校验失败；导入本来就只在
  同步 GitHub Issue 时才有意义，跟着那个开关最省事。
- 都算确定性来源：人报的 Issue 不会「再出现一次」，按偶发先观察就永远到不了评估。
- 一个 GitHub Issue 一个问题，不按「同一处」并进别的来源的问题(`collect/dedup/group.py` 的 `STANDALONE_SOURCES`)：
  并进去就没人回应这个 Issue 了；是否同一根因交给评估的查重，并入时在 GitHub 上关成重复。
- 接手原编号而不是另建：同一件事在 GitHub 上只有一个 Issue，提的人能在原处看到进展，PR 合并时一并关掉。
- 写到 GitHub 的话像开发者自己写的：一两句、按项目语言，不提工具与模型，不写本地编号。

## 不做什么

- 不导入已关闭的 Issue，不读评论；
- 不改别人 Issue 的标题、正文与标签；
- GitHub 回应失败不自动重试(Issue 留着打开，错误记在问题里)。
