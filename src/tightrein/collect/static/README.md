# 静态巡检(collect.static)

## 是什么

不运行程序、直接检查代码找缺陷：确定性工具(Semgrep 与项目、技术栈自带的工具) → 模型审查(筛工具结果的误报，提出候选主张) → 程序筛主张 → 逐条取证 → 只有成立与有条件成立的主张转成信号(git blame 标出引入的提交)。采集阶段最贵的模块，优化的目标是先省 token，再提速度，再求准确实用(不换模型)。

三个档位：

| 档位 | 什么时候 | 确定性工具 | 模型审查 |
|---|---|---|---|
| incremental(增量) | 日常，有新提交时 | 只扫改动文件(依赖漏洞不受限) | 只看 diff 与改动所在的整个函数 |
| full(全量) | 手动 | 扫整个仓库 | 改动的 diff，加每个缺陷模式的变体扫描 |
| baseline(基线) | 接入时一次(第一次巡检自动按它) | 扫整个仓库 | 按目录分批审查现有代码，加变体扫描 |

## 流程

`source.py` 的 `collect(runtime, *, level=Level.INCREMENTAL)`：

1. 只读 worktree(`worktrees/readonly`，游离 HEAD)切到 origin/<主分支>；切不过去或 HEAD 不对即失败；
2. 范围(`scope.py`)：增量起点为上次巡检的目标 commit(state 表 `collect.static`)，没有新提交时跳过；
3. 确定性工具：Semgrep(`tools/semgrep.py`)、项目与技术栈的工具(`tools/extension.py`)、规则库(工作区 `rules/*.yaml`，命中直接成信号)；
4. 记 git 状态与工作树哈希，去掉 worktree 的写权限；
5. 审查：增量与全量由 `context.py` 准备 diff 与改动所在的函数，交给 `review.py`(collect.static.review)；只改文档、测试、配置、锁文件的跳过；内容哈希已审过的跳过(state 表 `collect.static.reviewed`)；基线由 `baseline.py` 分批(collect.static.baseline)；全量与基线另按知识库的缺陷模式做变体扫描(collect.static.variant)；
6. 筛主张(`claims.py`)：没有位置或触发条件、与未关闭问题重复、命中抑制规则的丢掉；增量审查最多 N 条，按严重度取前 N；
7. 取证(`verify.py`，collect.static.verify)：先取证上次遗留的，再按严重度取证本次高、中级的，合计不超过上限；不同文件并行；低级与超出上限的留在待取证清单(state 表 `collect.static.pending`)；
8. 恢复写权限，再比一次 git 状态，有变化整次作废；
9. 成立的主张转信号(`mapping.py`)。

## 输入与输出

| 输入 | 位置 |
|---|---|
| 规则集 | 工作区 `settings.json` 的 `overrides.controls."collect.static".semgrep.configs`(如 `["p/python"]`) |
| 项目与技术栈的工具 | `overrides.controls."collect.static".extension`：`[{name, command, timeout, secrets}]`，脚本照 `tools/extension.py` 的说明读写 JSON，输出按 `tools/extension.schema.json` |
| 规则库 | 工作区 `rules/*.yaml`(由已修缺陷生成、已用原补丁验证过的 Semgrep 规则) |
| 缺陷模式、约定与取舍 | 工作区 `knowledge/patterns/`、`knowledge/conventions/` |
| 抑制规则 | `controls."collect.dedup".suppress` 与评估判为误报时生成的规则(与去重共用) |

输出 `SourceResult`：信号(check_type `static`，location `文件:行`，symbol 为根因的「类名.方法名」，verified 与 deterministic 为真；evidence 带规则、判定、代码事实、调用点、引入的提交与完整的取证输出，评估直接复用)；state(目标 commit、已审内容哈希、待取证清单)；coverage(检查范围内的文件)。原始输出在运行目录的 `16-collect.static-raw/`。

## 配置

`settings/defaults.json` 的 `controls."collect.static"`：`sourceTimeout`(整个来源的时限，与模型调用的 `timeout` 分开)、`semgrep`(`configs`；整体时限取 `limits.timeouts.semgrep`)、`extension`、`nonCode`(不交给模型审查的路径模式，另加项目的测试路径模式)、`maxVerify`(每次取证上限，含遗留的)、`pendingLimit`、`duplicateLines`(与未关闭问题算重复的行距)、`contextLines`、`functionLines`、`changesChars`(交给审查的改动文本上限)、`knowledge`(`entries`、`tokens`)、`baseline`(`maxClaims`、`batchFiles`、`batchLines`、`exclude`)。`controls."collect.static.review".maxClaims` 为增量审查的主张条数上限。四个调用点的模型与上限在 `controls."collect.static.review|verify|baseline|variant"`。

外部 skill：审查用 differential-review、sharp-edges，基线用 sharp-edges，变体扫描用 variant-analysis；调用前逐文件核对 `vendor/lock.json` 的哈希，不符即整次失败。

## 设计依据

| 做法 | 为什么 | 出处 |
|---|---|---|
| 程序准备 diff 与改动所在的整个函数，审查不再自己通读、搜索 | 审查读整个范围是最大的 token 开销；同实施的代码笔记 | 44 号计划 1.6 第一批 |
| 取证前程序筛主张(无位置、无触发条件、重复、命中抑制) | 每条主张单独一次取证，挡掉一条就省一次调用 | 44 号计划 1.6 |
| 只改文档、测试、配置、锁文件的不审；内容哈希审过的不审 | 不审非代码改动，不重复审同一份内容 | 44 号计划 1.6 |
| 取证按文件并发 | 不同文件的取证互不依赖 | 44 号计划 1.6 |
| 增量最多 N 条，按严重度取前 N | 迫使只报最重要的 | 44 号计划 1.6 |
| 增量范围只含 HEAD 中仍存在的改动文件；base 与 HEAD 按前缀比较 | 删除的文件不用审；记下的 commit 可能是缩写 | 旧 scope.py |
| 起点只在审查成功时前进 | 失败的运行没审到的改动下次还要审(审过的按内容哈希跳过) | 旧 run_probe.base_commit |
| 第一次全量按基线分批；基线按目录合批、超大文件独占一批；工具结果分到所在的批，不在任何批的放第一批 | 一次读不下整个仓库；依赖清单的漏洞不能丢 | 旧 baseline.py、probe.assign_findings |
| 基线中断后从没审到的文件接着审 | 预算用尽时其余批次不跑，下次按内容哈希跳过已审的 | 旧 probe._baseline(改进) |
| Semgrep 只有退出码 0 算正常；--metrics=off 且关闭版本检查 | 不加 --error 时有结果也返回 0；不联网 | 旧 tools/semgrep.py |
| 增量档只留改动文件里的工具结果，依赖漏洞除外；某个工具失败只丢它的结果、记 partial | 依赖清单常常不在改动里 | 旧 tools/extension.py |
| 同一处(文件、行、规则)只取证一次；超出上限与预算用尽没取证的留到下次 | 不重复花钱，也不丢主张 | 旧 probe._verify |
| 环境级越界(只读被改等)作废整次，其余越界只作废那一次；前后比 git 状态；chmod 去写权限 | 环境不可信时任何结论都不能用；三层只读保护 | 旧 probe.run、reviewer.environment_violated |
| 只有成立、有条件成立的产出信号；location 优先取根因；依赖漏洞写「清单:包名」；git blame 失败不影响信号 | 评估复用取证结论，不再取证一次 | 旧 mapping.py |
| 规则库命中直接成信号，失败记 partial | 规则已用原补丁验证过 | 旧 probe.run(library) |

## 不做什么

- 不运行被检查的程序，不改 worktree，不写 git；
- 同一文件的主张合并在一次调用中取证(第二批)尚未做；
- 不判断问题值不值得修(交给评估)；不给修复代码。
