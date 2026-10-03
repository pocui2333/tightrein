# 交接文档

<!-- 本文件由 core/dev/contract_reference.py 生成，不要手改；改 schema 或类型注册后重新生成。 -->

格式与规则见 [交接文档](../explanation/redesign/00-handoff-documents.md)；新增类型见[如何新增一种交接文档类型](../how-to/add-handoff-document-type.md)。校验：`tightrein doc check <文件>`。

## 头信息

给程序路由、判断状态与串联链路；由核心写入并校验，状态只看这里。

| 名称 | 类型 | 必填 | 说明 | 示例 |
|---|---|---|---|---|
| `kind` | 字符串(`^[a-z][a-z0-9-]*$`) | 是 | 文档类型，决定「内容」部分有哪些固定小节；须已在 domain/handoff/types.py 登记 | `"plan"` |
| `id` | 字符串(`^[A-Za-z0-9][A-Za-z0-9._-]*$`) | 是 | 唯一编号 | `"TR-20261002-0003"` |
| `status` | `pending` \| `in-progress` \| `blocked` \| `done` \| `failed` | 是 | 文档所说的工作的状态 | `"done"` |
| `from` | 字符串 | 是 | 写入者：模块或「模块/角色」 | `"triage/claim-verifier"` |
| `to` | 字符串 | 是 | 交给谁：模块、「模块/角色」或 user | `"fix"` |
| `subject` | 字符串 | 是 | 所说的对象(问题、Issue、运行等)的编号；一份文档只说一个对象 | `"P-0043"` |
| `parent` | 字符串 或 null | 否 | 上游文档的编号，串起整条链路；没有上游时为 null | `"SC-20261001-0042"` |
| `created` | 字符串(`^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$`) | 是 | 创建时间(UTC) | `"2026-10-02T02:15:00Z"` |
| `updated` | 字符串(`^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$`) | 是 | 最后一次写入的时间(UTC) | `"2026-10-02T02:21:00Z"` |
| `next` | 字符串 或 null | 否 | 下一步的简述 | `"fix plan 0003"` |

## 基础小节

正文按以下顺序使用二级标题；标题按 `project.language` 写入，读取时三种语言都识别。

| 键 | zh | en | ja |
|---|---|---|---|
| `conclusion` | 结论 | Conclusion | 結論 |
| `content` | 内容 | Content | 内容 |
| `decisions` | 需要决定 | Decisions needed | 要判断事項 |
| `next` | 下一步 | Next steps | 次のステップ |
| `references` | 引用 | References | 参照 |
| `history` | 历史 | History | 履歴 |

## 文档类型

「内容」下按顺序使用三级标题，全部必需。数据块是信息串为 `yaml data:<标签>` 的代码块，放在所属小节的末尾。

### task

上游向下游下达任务。数据块的 schema：`handoff/types/task.schema.json`。

| 小节键 | zh | en | ja | 数据块 |
|---|---|---|---|---|
| `goal` | 目标 | Goal | 目的 |  |
| `inputs` | 输入 | Inputs | 入力 |  |
| `constraints` | 约束 | Constraints | 制約 |  |
| `acceptance` | 验收标准 | Acceptance criteria | 受け入れ基準 | `acceptance` |
| `deliverables` | 产出要求 | Deliverables | 成果物の要件 |  |

#### 数据块 `acceptance`

「验收标准」小节中逐条可对照的验收项

类型：数组

| 名称 | 类型 | 必填 | 说明 | 示例 |
|---|---|---|---|---|
| `[].id` | 字符串(`^A\d+$`) | 是 | 验收项编号，结果与计划按它对照 | `"A1"` |
| `[].text` | 字符串 | 是 | 可观察、可验证的验收条件 | `"复现测试修复前失败、修复后通过"` |

### result

下游回报执行结果。数据块的 schema：`handoff/types/result.schema.json`。

| 小节键 | zh | en | ja | 数据块 |
|---|---|---|---|---|
| `done` | 做了什么 | What was done | 実施内容 |  |
| `outputs` | 产出 | Outputs | 成果物 |  |
| `evidence` | 证据 | Evidence | 証拠 | `checks` |
| `deviations` | 偏离与原因 | Deviations and reasons | 計画との差異と理由 |  |

#### 数据块 `checks`

「证据」小节中每项检查的结论

类型：数组

| 名称 | 类型 | 必填 | 说明 | 示例 |
|---|---|---|---|---|
| `[].name` | 字符串 | 是 | 检查名称或命令 | `"pytest tests/unit"` |
| `[].verdict` | `passed` \| `failed` \| `skipped` | 是 | 检查结论 | `"passed"` |
| `[].summary` | 字符串 | 否 | 输出摘要；完整输出放在附件，由「引用」给出路径 | `"2590 passed"` |

### finding

采集产出的问题。数据块的 schema：`handoff/types/finding.schema.json`。

| 小节键 | zh | en | ja | 数据块 |
|---|---|---|---|---|
| `symptom` | 现象 | Symptom | 現象 |  |
| `location` | 位置 | Location | 箇所 | `locations`(必需) |
| `evidence` | 证据 | Evidence | 証拠 |  |
| `severityHint` | 严重度提示 | Severity hint | 重大度の目安 |  |

#### 数据块 `locations`

「位置」小节中问题出现的代码位置

类型：数组

| 名称 | 类型 | 必填 | 说明 | 示例 |
|---|---|---|---|---|
| `[].path` | 字符串 | 是 | 相对仓库根目录的文件路径 | `"src/api/orders.py"` |
| `[].line` | 整数 | 否 | 行号 | `42` |
| `[].symbol` | 字符串 | 否 | 函数、类或接口名 | `"OrderService.create"` |

### decision

请求用户或上级拍板。数据块的 schema：`handoff/types/decision.schema.json`。

| 小节键 | zh | en | ja | 数据块 |
|---|---|---|---|---|
| `background` | 背景 | Background | 背景 |  |
| `options` | 选项及利弊 | Options and trade-offs | 選択肢と得失 | `options`(必需) |
| `recommendation` | 推荐与理由 | Recommendation and reasons | 推奨案と理由 |  |

#### 数据块 `options`

「选项及利弊」小节中可供选择的选项

类型：数组

| 名称 | 类型 | 必填 | 说明 | 示例 |
|---|---|---|---|---|
| `[].id` | 字符串(`^[a-z0-9-]+$`) | 是 | 选项编号，用户的选择按它记录 | `"split"` |
| `[].summary` | 字符串 | 是 | 一句话说明这个选项 | `"拆成两个子 Issue 依次修复"` |
| `[].recommended` | 布尔 | 是 | 是否为推荐选项；最多一个为 true | `true` |

### plan

修复或实现计划。数据块的 schema：`handoff/types/plan.schema.json`。

| 小节键 | zh | en | ja | 数据块 |
|---|---|---|---|---|
| `approach` | 方案 | Approach | 方針 |  |
| `steps` | 文件与步骤 | Files and steps | ファイルと手順 | `files`(必需) |
| `acceptanceMapping` | 验收对照 | Acceptance mapping | 受け入れ基準との対応 |  |
| `outOfScope` | 不做什么 | Out of scope | 対象外 |  |

#### 数据块 `files`

「文件与步骤」小节中计划改动的文件清单

类型：数组

| 名称 | 类型 | 必填 | 说明 | 示例 |
|---|---|---|---|---|
| `[].path` | 字符串 | 是 | 相对仓库根目录的文件路径 | `"src/api/orders.py"` |
| `[].change` | `add` \| `modify` \| `delete` | 是 | 改动方式 | `"modify"` |
| `[].reason` | 字符串 | 否 | 为什么要改这个文件 | `"补上数据归属校验"` |

### review

评审意见。数据块的 schema：`handoff/types/review.schema.json`。

| 小节键 | zh | en | ja | 数据块 |
|---|---|---|---|---|
| `verdict` | 结论 | Verdict | 結論 |  |
| `findings` | 问题清单 | Findings | 指摘一覧 | `issues`(必需) |
| `basis` | 依据 | Basis | 根拠 |  |

#### 数据块 `issues`

「问题清单」小节中的问题；没有问题时为空列表

类型：数组

| 名称 | 类型 | 必填 | 说明 | 示例 |
|---|---|---|---|---|
| `[].level` | `blocking` \| `suggestion` | 是 | 阻断项须修改后才能继续；建议项不阻断 | `"blocking"` |
| `[].text` | 字符串 | 是 | 问题与修改建议 | `"缺少对 owner 的校验"` |
| `[].location` | 字符串(`^[^:\s][^:]*:\d+(-\d+)?$`) | 否 | 代码位置 <路径>:<行> | `"src/api/orders.py:42"` |

### progress

长任务的进展。数据块的 schema：`handoff/types/progress.schema.json`。

| 小节键 | zh | en | ja | 数据块 |
|---|---|---|---|---|
| `checklist` | 检查清单 | Checklist | チェックリスト | `checklist`(必需) |
| `completed` | 已完成 | Completed | 完了済み |  |
| `blockers` | 卡点 | Blockers | 障害 |  |

#### 数据块 `checklist`

「检查清单」小节中的各项工作

类型：数组

| 名称 | 类型 | 必填 | 说明 | 示例 |
|---|---|---|---|---|
| `[].item` | 字符串 | 是 | 一项工作 | `"建修复分支与 worktree"` |
| `[].state` | `pending` \| `done` \| `blocked` \| `failed` | 是 | 这一项的状态 | `"done"` |
| `[].owner` | `system` \| `user` | 否 | 由系统自动完成还是需要用户处理 | `"system"` |

### issue

本地 Issue。数据块的 schema：`handoff/types/issue.schema.json`。

| 小节键 | zh | en | ja | 数据块 |
|---|---|---|---|---|
| `problem` | 问题 | Problem | 問題 |  |
| `impact` | 影响 | Impact | 影響 |  |
| `reproduce` | 复现 | Steps to reproduce | 再現手順 |  |
| `cause` | 原因 | Cause | 原因 |  |
| `scope` | 范围 | Scope | 範囲 |  |
| `notes` | 注意事项 | Cautions | 注意事項 |  |
| `acceptance` | 验收标准 | Acceptance criteria | 受け入れ基準 |  |
| `direction` | 修复方向 | Fix direction | 修正方針 |  |
