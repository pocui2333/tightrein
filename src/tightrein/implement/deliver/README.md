# 交付(implement.deliver)

## 是什么

实施的最后一步：把在当前这份改动上通过了自检与审查的结果交给发布。纯程序，不调用模型。入口 `deliver.run(runtime, context) -> Handoff`。

## 流程

1. 再核对一次 diff 哈希：自检不在当前改动上通过就回到自检；审查后工作区又有改动就只回到审查(`facts.backTo`)，不重新编码；
2. 生成补丁(含未跟踪的新文件)，落盘 `38-implement.deliver-diff.patch`；
3. 汇总必填事实并按 `deliver.schema.json` 校验；
4. Issue 经 `assess/issue/transitions.apply_event` 以 DELIVER 转为 releasing，历史一并记下(actor 为 tightrein)。

## 输入与输出

输入：上一次自检与审查的交接、worktree。

输出(必填事实，44c 规定发布只读这些；键名照 `release/record.parse_delivery`)：

| 字段 | 内容 |
|---|---|
| `branch`、`worktree`、`commit` | 修复分支、修复目录、worktree 当前 HEAD |
| `base`、`diffHash` | 比较基准与与提交无关的改动哈希(发布据此判断合并主干后是否要重新审查) |
| `changedFiles` | 每个文件的路径、增删行数、状态(`?` 为未跟踪的新文件) |
| `checks` | `{name, passed, detail}`：每条项目检查与本机运行检查；弱证据与未验证不阻断，但结论写在名字里，PR 描述照样看得到 |
| `acceptedFindings` | 用户接受的未通过项(实施中没有这种流程，恒为空) |
| `review` | `{round, conclusions}`：审查轮次与结论(含逐条验收标准) |
| `highRisk`、`highRiskPaths` | 是否命中高风险路径(合并须人工确认)及命中的文件 |
| `release` | 编码给出的提交与 PR 文字(`implement.code` 的 release，换成 title、scope、summary、why、problem、approach、limitations) |
| `patch` | 补丁文件路径 |
| `unverified` | 弱证据与未验证的项、审查没有亲自确认的项，写进报告不阻断 |
| `rounds` | 全部轮次的汇总：`{round, steps: {<编码、自检、审查>: {status, summary, blockers}}}`，读各轮落盘的交接；阻断项写成 `[性质/类型] 位置：说明` |
| `knowledgeSuggestions`、`skipped` | 程序步骤，恒为空、null |

## 配置

无模块参数；高风险路径取 `boundaries.protected.highRisk`。

## 设计依据

- **交付前再核对哈希，审查后又改了只回到审查**(旧 `FixService.done`、`ResumePoint.REVIEW`)。
- **补丁附上未跟踪的新文件**：审查与交付看到的是完整改动(旧 `pipeline/fix/steps/report.py:patch_text`，实现在 `check/changes.py` 与审查共用)。
- **弱证据与未验证照写进报告**：写成通过等同伪造(`skills/verify/references/local-run.md`)。
- **状态转换只经状态机**：转换规则与历史只在 `assess/issue/transitions` 一处(44c)。

## 不做什么

- 不提交、不推送、不提 PR(那是发布)；
- 不生成「交付」文档：`90-issue-deliver.md` 在验收通过后由 `protocol/documents.deliver` 汇总。
