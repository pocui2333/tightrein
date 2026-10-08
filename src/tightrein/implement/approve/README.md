# approve：定案

## 是什么

`implement.approve`：真风险或超出自动确认门槛时等用户确认方案，其余自动。

## 流程

1. 有停着等确认的同一份方案、且之后用户给了决定：approve 通过；reject 交接为没通过，implement.py 按原因重出方案；
2. 否则列出不能自动确认的全部原因(`auto_reasons`)：需要拍板的点、三类标记、高风险文件、迁移、新增依赖、删除文件、高风险判定、超出 `boundaries.autoApprove`、前端设计说明与方案冲突；
3. 没有原因且 `boundaries.gates.design` 为 auto：自动通过；否则停在关卡(交接 `gate` 供待审核文档)，同一份方案的原因只记一次事件。

## 输入与输出

- 输入：方案交接(`planHash` 与各字段)、用户的决定；
- 输出：交接事实 `planHash`、`auto`(是否自动确认，每种结局都写)、`approvedBy`(auto、user)、`reasons`、`gate`、`rejected`、`note`、`option`；`skipped`(null)与 `knowledgeSuggestions`(空列表)由 implement.py 补上；
- `record(runtime, issue, Decision)`：命令行 approve、reject 的入口，把决定(来源、时间、原文)写进 `issues.extra.decisions`；
- `confirmed(context)`：编码前核对确认的就是当前方案。

## 配置

`boundaries.autoApprove`、`boundaries.gates.design`、`boundaries.protected.highRisk`。

## 设计依据

- 确认绑定方案哈希：方案一重出，旧确认即失效(旧 `plan_gate`)；
- 自动确认的条件取自旧 `autonomy.plan_confirmation`，门槛改取 `boundaries.md`；
- 拒绝必带原因，按原因重出 1 次(`protocol/limits.md`「方案被否决后重出」)。

## 不做什么

不在这里拆分子 Issue；不调用模型。
