# 自检(implement.check)

## 是什么

编码之后由程序检查改动：项目检查(测试、lint、类型检查、构建)、改动量与规则检查、本机运行检查(`runtime/`)。除截图审查外不调用模型。入口 `check.run(runtime, context) -> Handoff`，由 `implement/implement.py` 落盘与推进。

## 流程

1. 改动没变就不重做：上次已通过且 diff 哈希(与提交无关，`protocol.git.diff_hash`)相同，直接沿用上次结果。
2. 改动量与规则检查(`rules.py`)：越界、禁改、超量、方案外文件、改已有测试、跳过标记、残留模式；疑似写死只提示。
3. 项目检查(`commands.py`、`affected.py`)：第一轮全量；修正轮次先只跑受影响的(快速失败)，都过了再补跑其余的；相互独立的命令并行(并发数取 `resources.concurrency.tests`)。检查命令执行后工作区被改了算局部问题。
4. 前面都过了才做本机运行检查(`runtime/session.py`)。
5. 交出 handoff：不通过项带性质(`findings.py`)与(位置, 类型)。

| 结论 | 条件 |
|---|---|
| passed | 没有不通过项，没有等用户的事 |
| failed | 有不通过项；`facts.firstCategory` 为最靠前的一类，implement.py 只处理这一类 |
| pending | 改到迁移文件等用户确认，或截图审查给不出结论等用户查看(已写 `90-issue-pending.md`) |

## 输入与输出

输入：`ImplementContext`(worktree、基准 commit、轮次、方案与上一次自检的交接)；`settings.json` 的 `project.commands` 与 `project.testPatterns`。

输出(必填事实)：

| 字段 | 内容 |
|---|---|
| `base`、`diffHash` | 比较基准与改动的哈希 |
| `scope` | `full`(全量通过或第一轮)、`affected`(修正轮次只跑了受影响的就有失败) |
| `changedFiles`、`changedThisRound`、`fileStates` | 改动的文件、这一轮又改了的、各文件的内容哈希(下一轮据此算「这一轮」) |
| `counted`、`highRiskPaths` | 计入上限的文件数与行数、命中高风险路径的文件 |
| `commands` | 每条命令：命令、结果(passed、failed、not_run、invalid)、退出码、日志路径、精简后的失败输出 |
| `runtime`、`runtimeNotes` | 本机运行检查的各项(passed、weak、unverified、failed)与说明 |
| `hardcodeHints` | 疑似写死的提示，交给审查 |
| `blockers`、`firstCategory`、`overCapSeen` | 不通过项、最靠前的一类、本次实施是否已超量过一次 |
| `migration`、`awaitingScreenshots` | 等用户确认的迁移、等用户看的截图 |
| `skipped`、`knowledgeSuggestions` | 改动没变沿用上次结果时为原因(否则 null)；程序步骤不提建议，恒为空 |

日志与运行产物在 `36-implement.check.r<轮>-raw/` 下。

## 配置

`settings/defaults.json` 的 `controls`：

| 键 | 含义 |
|---|---|
| `implement.check.environmentPatterns`、`environmentRetries` | 测试环境问题的输出特征(正则)与就地重试次数 |
| `implement.check.testFailureExitCodes` | 测试命令的这些退出码算测试失败，其他非零为无效(收集错误、用法错误、没选中测试) |
| `implement.check.skipMarkers`、`residuePatterns`、`minLiteralLength` | 跳过标记、残留模式、疑似写死比较的最短字面量 |
| `implement.check.when` | 命令名 → 路径模式：非全量的轮次只在相对基准的改动中有文件匹配时跑该命令；全量时不过滤；缺省为空(都跑) |
| `implement.check.protectedMarkers` | 受保护标记(如许可证头、生成代码标记)：增删的行含它们即为需要用户，方案中已确认的受保护文件除外；缺省为空 |
| `implement.check.outputChars`、`logTailBytes` | 交给模型的失败输出上限、从日志末尾读取的字节数 |
| `limits.timeouts.tests` | 每条命令的时限 |
| `boundaries.*` | 改动量上限、不计数的文件、受保护文件两级 |

测试命令写 `{tests}` 占位才能只跑受影响的测试(如 `pytest -q {tests}`)；全量时去掉占位。

## 设计依据

- **未运行不交回编码**：检查没跑起来时让模型改代码只会越改越乱；记为需要用户，停下报告原因(旧 `pipeline/fix/steps/checks.py:_project`)。
- **环境问题就地重试 1 次，断言失败不重试**：limits.md「按失败类型处理」；不靠重试掩盖测试本身的不稳定(旧 `repo_test_check.py:classify_failure`)。
- **失败输出精简**：只留失败段落、报错行与末尾统计，通过行只认 `xxx::yyy PASSED`(73f1058：写成 `.*PASSED` 会把失败详情里含 PASSED 的行当成通过)；完整输出只给日志路径(旧 `output_trim.py`)。
- **改动量含未跟踪文件，不计测试与生成文件；超量先交回收敛一次，再超即交人**：不能以放宽上限处理(旧 `diff_rules.py:size_violations`、`SIZE_GUIDANCE`)。
- **不通过项按性质只处理最靠前的一类**：设计问题 > 需要用户 > 方案缺口 > 局部问题(旧 `triage_blockers.py`)。
- **修正轮次只跑受影响的，通过时补跑全量**：提速，又保证交给审查与交付的「通过」都是全量通过(44 号计划「5 实施·优化」)。
- **疑似写死只提示**：字面量相同不等于特判，交给审查读代码判断；短于 4 个字符的不比(旧 `diff_rules.py:hardcode_violations`)。

## 不做什么

- 不写复现测试、不在基准版本上验证测试先失败(随设计取消)；
- 不改代码：格式化、自动修复要在编码时做；
- 不跑其他 Issue 的回归检查清单(新设计没有回归检查登记，部署后的确认在 `release/accept/`)。
