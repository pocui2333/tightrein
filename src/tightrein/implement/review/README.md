# 审查(implement.review)

## 是什么

自检通过后由模型审查改动：轻量审查都做(调用点 `implement.review`)，高风险另加深度审查(`implement.review.deep`，盲审，与编码不同的模型)。只判断程序判定不了的部分：根因有没有修掉、有没有破坏调用方、有没有针对测试数据写死、验收标准是否达成。入口 `review.run(runtime, context) -> Handoff`。

## 流程

1. 改动没变就不重做：上次已通过且 diff 哈希相同，直接沿用(合并主干后只在主干改到同一文件时才重新审查)；
2. 上次停在「无法判断」且用户已给出判断：通过即视为通过(判断原文记入 unverified)，不通过即为局部问题；不重跑；
3. 按实际改动重新判定风险(`design/risk.apply_risk`)；
4. 轻量审查：修正轮次只给这一轮又改了的文件与上一轮的问题；
5. 轻量通过且高风险才跑深度审查(盲审：只给验收标准、用户的决定与补充及其后附的交给用户拍板的问题与推荐、合并进来的 main、最终改动与检查的实际输出)；
6. 程序过滤审查意见，汇总成交接。

两种审查都逐条核对用户的决定与补充(含确认方案时的说明)是否照做。用户的决定只记了回答，回答针对的是方案当时交给用户拍板的问题(`userDecisions`，确认方案时没另说的即照推荐)：轻量审查从方案摘要拿到它们，深度审查拿不到方案，只把这些问题与推荐附在用户的决定之后(方案的其他内容不给)。用户的决定高于方案，没照做的报 `decision-unmet`(局部问题，交回编码；照做要改方案外的文件时为方案缺口)。它不对应验收标准：判为不满足的验收标准在审查没给 `requirement-unmet`、`requirement-reduced` 时照常按方案缺口记一条，不因有 `decision-unmet` 而省掉。

发布合并 main 后退回实施(`ImplementContext.returned`)、这次交付已合并进那个 main 时，两种审查都拿到合并信息(`merged_main`)：两侧都改了的文件(编码解决过冲突的，或 main 改到修复文件的、人解决过冲突的)、main 一侧改到这些文件的提交、main 一侧对这些文件的改动(从合并前修复分支与 main 的分叉点到 main)。改动相对合并进来的 main 算，解决冲突时丢掉的 main 一侧(如两侧都在测试文件末尾追加了测试，只留了自己的)在改动里只是方案内文件的一处删除；审查据此核对 main 一侧新增或改过的内容都还在，丢掉、改回的报 `main-dropped`(局部问题)，用户的决定要求取舍的除外。人在发布阶段解决过冲突的文件另写明取了哪一侧(发布交接的 `resolutions`)：人的取舍与用户的决定一样作数，整份取了修复一侧的不再列 main 一侧的改动，两侧合并的以人的合并提交为准，那时已经没有的 main 一侧内容不报；否则审查会把人有意舍弃的 main 一侧当成丢掉的报阻断，编码又把它加回来，两套互斥实现并存。

## 输入与输出

输入：Issue 正文与验收标准、代码笔记、方案摘要(含交给用户拍板的问题)、用户的决定与补充、合并进来的 main(没有为「无」)、改动(补丁含未跟踪的新文件)、自检的实际结果与疑似写死提示。

输出(必填事实)：

| 字段 | 内容 |
|---|---|
| `base`(同 `baseCommit`)、`diffHash`、`fileStates` | 审查时的基准、改动哈希、各文件内容哈希；`baseCommit` 供采集任务外发现时取 commit |
| `modes`、`incremental`、`reviewedFiles` | 跑了哪几种审查、是否只审这一轮、审了哪些文件 |
| `acceptance` | 逐条验收标准：`criterion`、`result`(pass、fail、unknown)、`reason` |
| `blockers`、`firstCategory` | 阻断项 `{check, kind, location, summary, category, trigger}` 与最靠前的一类；status、watch 读 location、kind、summary，「没有进展就停」按(location, kind)比较 |
| `discardedFindings` | 被程序丢弃的审查意见与原因 |
| `unverified`、`awaitingUser` | 没有亲自确认的项、等用户判断的验收标准 |
| `callFailed` | 审查没有返回结果：下一轮只重跑审查，不重新编码 |
| `risk` | 按实际改动的风险判定 |
| `misjudged` | 上一轮的阻断项这一轮判为误报时 `{kind: false_block, point, detail}`，复盘据此记误判 |
| `knowledgeSuggestions` | 审查提出的建议沉淀 |
| `incidentalFindings` | 与本 Issue 无关、顺带发现的缺陷(结构照 `collect/incidental/finding.schema.json`)；不论这一轮是否通过都会被采集 |
| `skipped` | 改动没变沿用上次结论时为原因，否则为 null |

## 配置

`controls.implement.review`(模型、轮数、时限)、`controls.implement.review.deep`；`independence` 中 `["implement.review.deep", "implement.code"]` 由配置校验保证深度审查与编码用不同的模型(含条件变体)。提示在 `prompts/implement.review.md`、`implement.review.deep.md`；输出格式 `review.schema.json`(两个调用点共用)。

## 设计依据

- **审查意见的程序过滤**：类型必须在该模式允许的范围内；必须有 `文件:行号` 且位置真实存在；必须有触发条件；不合格的丢弃记入 discardedFindings，不计为不通过(旧 `pipeline/fix/steps/review.py:filter_findings`)。
- **只报两类问题，逐处做特判检查，对照根因假说**(旧 `skills/fix/references/roles/fix-reviewer.md`、`fix_reviewer.py:SPECIAL_CASE`)。
- **合并 main 的信息交给审查**：两侧都在测试文件末尾追加测试、解决冲突只留了修复一侧时，没有合并信息的审查只看到方案内文件的一处删除，按「多出来的改动本身不算问题」放过，别的 Issue 已合入的测试就被悄悄撤掉了。
- **深度审查是盲审**：不给 Issue 正文、方案与编码模型的说明，让判断独立于编码时的思路(2ef30cc)。
- **审查的输入是程序跑出的实际结果**，以 diff 为准，不通读、不全项目搜索(旧 `fix_reviewer.py:REVIEW_SCOPE`)。
- **先轻量后深度**：轻量没通过就不跑深度，省一次调用(旧 `_Apply._loop`)。
- **修正轮次只审新增改动与上一轮的问题**：省 token、提速(44 号计划「5 实施·优化」)。
- **与提交无关的 diff 哈希**：合并主干后只在改到同一文件时重新审查(#5)。
- **「无法判断」交给用户**，用户判断后不重跑编码与审查(旧 `_await_review`、`_accept_review`)。
- **审查没有返回结果时下一轮只重跑审查**(旧 `_Apply._loop` 的 `skip_execute`)。

## 不做什么

- 不报风格、命名与没有触发条件的假想风险；
- 不改代码、不运行测试(那是编码与自检的事)；
- 不决定怎样处理阻断项：按性质分派由 `implement/implement.py` 做。
