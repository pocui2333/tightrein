---
name: issue
description: 查看、审阅、放行、编辑、关闭与重开 tightrein 的本地 Issue(工作区 issues/ 下的 markdown 文件)，同步 Issue 与问题的状态，以及项目启用时的 GitHub Issue 镜像。分诊给出立即修或排期修时由工具自动创建，用户审阅放行后才进入修复；用户自己的需求用 tightrein new 直接建。
---

# issue

Issue 是修复与跟踪的工作单，也是写代码的模型直接据以实现的任务说明。每个 Issue 一个 markdown 文件 `issues/<编号>-<简称>.md`，是交接文档的 issue 类型：头信息有类型、编号、状态、严重度、任务类型、规模档、处理标签、来源、上游(父 Issue 或第一个关联问题)等；正文按 `project.language`(缺省 en)书写，基础小节为结论、内容、需要决定、下一步、引用、历史，「内容」下为问题(现状与期望)、影响、复现、原因、范围(可能涉及的文件与明确不做的部分)、注意事项(不能改的文件与行为)、验收标准(复选框，固定三条在前：复现测试修复前失败修复后通过、现有测试全部通过、必须保持不变的行为)、修复方向；完整证据在「引用」中，缺少的字段显示「—」。文件是唯一的数据来源，`issues` 表只是索引。只有处理标签为立即修、排期修的问题才建 Issue，由 `tightrein issue create` 在分诊之后自动创建或追加(同一根因的问题追加到已有 Issue)，不要手写 Issue 文件。用户自己提出的需求用 `new` 建立：头信息的 `origin` 为 `manual`，不关联问题与信号，创建即为待修(免审阅)，「问题」一节是用户原文。

## 命令

```
tightrein issue list [--status <状态>] [--severity <P0-P3>]
tightrein issue show <编号>
tightrein issue edit <编号>
tightrein approve <编号>
tightrein issue close <编号> --reason <wont-fix|duplicate|not-a-bug> [--note <说明>] [--duplicate-of <编号>]
tightrein issue reopen <编号> [--note <说明>]
tightrein issue create [--select <问题编号>] [--input <triage 交接文档> --output <目录>] [--dry-run]
tightrein new --title <标题> (--body <文本> | --body-file <文件>) [--severity <P0-P3>]
tightrein issue sync
tightrein issue reindex
tightrein issue rerender [<编号>...]
```

- `list` 按处理标签(立即修在前)、严重度、创建时间排列。
- `edit` 用 `$EDITOR` 打开文件，保存后校验；状态只能用 `approve`、`close`、`reopen` 改变。
- `close` 的原因只有不修、重复、不是缺陷；已修复与修复未采纳由验证与 PR 结果自动写入。重复时必须给 `--duplicate-of`。
- `new`(顶层命令)：严重度缺省 P2，`--type` 给出任务类型(缺省 `fix.manualTaskType`，决定修复通道)；正文自带「## 验收标准」一节时沿用，否则修复计划按「需求」逐条对应。之后照常 `approve`(不改状态，只申请建修复分支)→ fix → verify → release。用户需求没有确定性的复现检查：修复第 5 步按验收标准写的测试登记为它的复现检查，合并前验证另靠项目检查、相关复现检查与浅层巡检；不能 `--from triage` 重来。
- `rerender`：用现有数据按当前版式重写 Issue 正文(省略编号时为全部未关闭的 Issue)，旧版式的文件由此转为交接文档版式；保留「历史」，其余小节被覆盖(本地编辑会丢失)。分诊 Issue 同时按分诊结论更新标题；用户需求的 Issue 把原「需求」与「验收标准」转为「问题」与「验收标准」。启用 GitHub 镜像时同时更新镜像的标题与正文(遵守关卡 `gates.mirror-writes`)。不调用模型：较早分诊的 Issue 缺少标题、复现步骤、范围等字段，显示为「—」，输出会列出；要得到完整格式先 `tightrein problem retriage <问题编号>` 再 `rerender`。
- `sync` 由编排每次运行执行：以文件为准更新索引、关联问题回归时重新打开完成或取消的 Issue、回填分诊准确率、列出放行超时的 Issue。

## GitHub 镜像

`project.yaml` 的 `issues.tracker` 为 `github` 时(缺省 `local`，只在本地)，每个未关闭的本地 Issue 在 GitHub 上有一个镜像 Issue，frontmatter 的 `github` 记着编号与链接。本地仍是唯一数据源：

- 镜像正文与本地正文格式不同(拟人化展示)：本地正文保留交接文档的完整 WRAP 结构(供流水线与写代码模型使用)；GitHub 镜像正文由 tightrein 自动转换为真人开发者工单结构(原因、问题、方法，代码位置是指向取证 commit 的永久链接，完整证据折叠)，去除内部控制命令、流水线测试复选框与水印声明，使线上 Issue 保持自然、专业、无 AI 机器感。
- 镜像由工具建立与更新，不要在 GitHub 上手动建对应的 Issue；镜像仅使用常规类型标签(如 `bug`、`enhancement` 等，不添加 `tightrein:` 前缀和内部状态标签)，关键节点(PR 创建、PR 合并、关闭带原因)由工具同步，彻底屏蔽内部决策结论与流水线评论(留在本地与终端)，PR 描述带 `Closes #<编号>`。拆分出的子任务是父 Issue 的 GitHub 子 Issue，并被前一个子任务阻塞(`--parent`、`--blocked-by`)。
- 字段归属：开关状态以 GitHub 为准——在 GitHub 上关闭镜像，本地按「用户关闭」(不修)取消；重新打开，本地回到待修。标题、正文、标签与子 Issue 关系以本地为准，在 GitHub 上的编辑会在下次同步时被覆盖，评论不会同步回本地；要改 Issue 内容用 `edit`。
- 只允许私有仓库(`issues.github.privateOnly`)；公开仓库时不同步并说明原因。
- 镜像写入缺省逐次确认：待确认的镜像操作出现在运行摘要与 `tightrein status --pending` 中，用 `tightrein approve <操作编号>` 执行；关卡 `gates.mirror-writes` 为 auto 时直接执行。
- GitHub 调用失败不影响本地流程；`issue sync` 的输出与运行摘要的「未同步到 GitHub」列出没对齐的项，下一次运行自动重试。

## 状态与下一步

| 状态 | 含义 | 下一步 |
|---|---|---|
| `needs-decision` 待决定 | 新建等放行，或修复、验证中遇到需要用户决定的事(头信息 `hold` 写明原因) | 新建的审阅后 `approve` 或 `close`；带 `hold` 的按原因处理后 `fix start <编号> --force` |
| `todo` 待修 | 已放行；用户需求创建即为此状态 | 修复环节开始工作；还没有修复分支的用户需求先 `approve` |
| `in-progress` 进行中 | 修复、合并前验证或提交在进行(细分记在头信息 `phase`) | 按 `tightrein show <编号>` 继续 |
| `pending-merge` 待合并 | PR 已创建 | 用户在代码托管平台审核；满足条件时可自动合并 |
| `done` 完成 | PR 已合并(`phase` 为 `deploy-check` 时还在等部署后确认) | 部署后确认；回归时重新打开 |
| `cancelled` 取消 | 修复未采纳、不修、重复或不是缺陷 | 需要时 `reopen` |

## 放行前检查

1. 「复现」是否可信，能否据此重现；「问题」是否说清了现状与期望。
2. 「原因」与「引用」中的完整证据是否带 `文件路径:行号` 且说得通；「影响」中的严重度与理由是否合理。
3. 「范围」与「修复方向」是否合理，改动范围是否与问题相称；「注意事项」写着「需要先与代码作者讨论」时先讨论。
4. 「注意事项」中「需用户定夺」的项(根因在设计本身、要动数据结构或存量数据、会改变公共实现或接口契约)是否已经有了决定，「不能改」的项是否完整。
5. 「验收标准」是否都是可以验证的条件，必须保持不变的行为是否列全。

可以先用 `edit` 补充复现步骤、范围或注意事项，再 `approve`。放行后会当场询问是否建立修复分支与 worktree，拒绝时 Issue 仍为待修，之后用 `tightrein fix prepare <编号>` 再次申请。

## 关闭的连带影响

- 不修：关联问题转为已忽略，严重度升级或出现在新版本中时恢复。
- 不是缺陷：关联问题判为误报并生成抑制规则，分诊结论记为「误判为成立」。
- 重复：关联问题并入被重复的 Issue。
