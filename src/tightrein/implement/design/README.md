# design：方案

## 是什么

`implement.design`：按代码笔记拟方案(改哪些文件、每步改什么)，程序核对后交给定案；含前端文件时加一份前端设计说明(`implement.design.frontend`)；风险判定也在这里(`risk.py`)。

## 流程

1. 方案前风险判定(笔记涉及的文件 + 评估的标记)：高风险带 `high_risk` 条件选模型；
2. 调用模型，程序核对(`design.check`)：根因假说的位置真实存在且不越界；修改位置的文件都在文件清单里；要改的已有非测试文件都有修改位置；已有文件存在；禁改文件不能出现；高风险文件写进 `protectedTouches`；预估不超单个 PR 上限；验收标准逐条对应、每步至少对应一条。不合格带原因重出，模型没给结果也算一次，上限 `controls.implement.design.rounds`；
3. 根因在设计本身而用户没同意按设计层面修复：停在关卡(用户自己提的需求视为已同意)；一个 PR 放不下(`oversize`)：退回评估拆分；
4. 通过后：有前端文件时出前端设计说明(失败不阻断，原因记下)；按方案再判一次风险；记下方案哈希 `planHash`。

## 输入与输出

- 输入：Issue 正文与验收标准、代码笔记、风险判定、单个 PR 的上限与受保护文件、上一次的问题(用户否决、方案缺口)、用户的决定、知识条目；
- 输出：交接事实为方案本身(不含 `analysis`，分析放备注；含模型给出的 `knowledgeSuggestions`)加 `version`、`planHash`、`risk`、`highRiskPaths`(命中的高风险文件，status 与 watch 显示)、`frontendFiles`、`frontendDesign`、`frontendError`、`acceptance`；停在关卡时为 `reason: design_issue` 与 `gate`；没通过时为 `reason`(`oversize` 带 `splitBack`、`plan_rejected` 带 `problems`、`design_declined`)；模型输出格式 `design.schema.json`、`frontend.schema.json`。

## 配置

`controls.implement.design`(模型、`modelWhen.high_risk`、`rounds`、`riskRules`)、`controls.implement.design.frontend`(模型、`paths`)、`boundaries.*`。

## 设计依据

- 根因假说(78c3290)：一条因果链、代码证据、精确到行的修改位置，位置由程序核对；
- 精简输出字段：只留编码、定案与审查用得上的，拆分相关字段取消(44 号计划第 5 节「优化」)；
- 风险判两次：方案前作为输入与选模型条件，方案后决定是否等用户确认；自检后按实际改动再判(`apply_risk`，审查调用)决定审查深度。规则由配置给出，不写死；
- 前端设计说明复用项目既有设计变量与组件、不臆造名称，方案没覆盖的写进 `planConflicts`。

## 不做什么

不拆子任务、不写「留给后续子任务的验收标准」；不运行构建与测试。
