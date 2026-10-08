# knowledge：知识库

## 是什么

一个项目长期共用的记忆，供评估与实施按需取用。程序在这里，条目在各项目的工作区 `workspaces/<项目>/knowledge/<类>/`。

| 类 | 写什么 | 用在 |
|---|---|---|
| conventions(约定与取舍) | 项目约定、参考资料、已接受的取舍 | 评估时命中已接受的取舍就不再报；实施时遵守约定 |
| patterns(缺陷模式) | 这个项目反复出现的问题写法 | 评估与审查时对照 |
| lessons(经验) | 评估与修复的经验 | 评估与实施 |

| 文件 | 内容 |
|---|---|
| `entries.py` | `Entry`；条目的读写、头信息、格式校验；格式坏了的文件沿用上一次读好的内容 |
| `match.py` | `match(layout, paths, *, limit_entries, limit_tokens)`：按本次要动的位置匹配；`render`：拼进提示的文本 |
| `stale.py` | `check`：涉及的文件删除或大改时标为待确认 |
| `propose.py` | 收集各步骤的「建议沉淀」、去重，用户确认后经模型判断写入 |
| `curate.schema.json` | 调用点 `knowledge.curate` 的输出格式；模板在 `prompts/knowledge.curate.md` |

## 流程

### 条目的格式

`knowledge/<类>/<编号>-<英文短名>.md`，编号前缀 CON、PAT、LES 与类一一对应：

```markdown
---
id: PAT-0003
kind: patterns
summary: 列表接口按编号查询时漏了公司过滤
status: active            # active、stale(待确认)、superseded(已取代)
updated: '2026-10-08'
locations:                # 涉及的位置；为空表示对整个项目都适用
- path:src/services/       # 文件，或以 / 结尾的目录前缀
- route:GET /api/orders   # 「方法 空格 路由模板」
- page:/orders            # 以 / 开头的页面路由
commit: 3f2a…             # 写入时项目的 commit，过期比对的基准
sources: ['0018']         # 提出它的对象
supersededBy: null        # 只有已取代的条目写
staleReason: null
---

# 标题

正文。
```

校验(`problems`)一次列出全部问题，每条带文件、字段与原因：必填项、编号前缀与类一致、文件名以编号开头、所在目录与 kind 一致、位置格式、只有已取代的写 supersededBy、正文有一级标题。

### 写入

1. 各步骤只在交接的必填事实 `knowledgeSuggestions`(字符串列表)中提「建议沉淀」；`suggestions_in(handoff)` 取出；
2. `propose(runtime, subject, suggestions)`：按规整后的文字(去空白与标点、转小写)去重，列进待确认清单 `knowledge/proposals.json`；同一条再被提出只追加对象；被拒绝过的，只有拒绝时没有的新对象再提出才重新列为待确认；
3. 用户确认(`accept`)：调用一次模型(`knowledge.curate`)，给它建议原文与候选条目(按提出它的对象的代码笔记中的文件匹配，没有笔记时取最近更新的)，判断新增、更新、合并或不必写入并写出完整条目；程序校验判断(只能引用候选编号；update 恰好 1 条；merge、noop 至少 1 条；只有 add 可写 supersedes；条目字段与位置合格)，不合格带逐条原因再判一次，仍不合格就不写；
4. 写文件：新增与合并分配新编号，被推翻、被合并的旧条目标为已取代并指向新条目；更新改写原条目；条目从不删除文件，写入先写临时文件再改名；
5. 用户拒绝(`reject`)：记下原因与当时的对象。

### 读取

`match` 把调用方给的位置分成三类再匹配：「方法 路由」、以 `/` 开头的页面、其余取 `文件:行号`、`文件:符号` 中的文件。`path:` 前缀匹配(按路径段，`src/a` 不匹配 `src/ab.py`)，`route:`、`page:` 精确匹配；匹配到的标签越长越靠前，同样长按更新日期从新到旧，没有位置的条目排在最后；只取有效条目，最多 `limit_entries` 条、按摘要列出不超过 `limit_tokens`。`render` 在全部正文不超过上限时放全文，超过就只列编号与摘要。调用方给不出位置时，把问题信号的 location 原样传进来即可。

### 过期

`check(layout, git, *, change_ratio, today)`：对每条有效条目，比对写入时的 commit 到 HEAD：文件被删除，或增删行数之和与写入时的行数之比达到 `staleChangeRatio`，标为待确认并写明原因；目录前缀下已没有文件时同样标记。同一个 commit 只取一次 numstat；只比对文件，不调用模型。

## 输入与输出

- `match` → `list[Entry]`(实施上下文 `ImplementContext.knowledge`、评估的提示)；
- `propose` → 待确认清单；`accept` → 写入的条目编号；
- `check` → `StaleReport`(标记的条目与原因、警告)。

## 配置

`settings/defaults.json` 的 `controls`：

| 键 | 缺省 | 含义 |
|---|---|---|
| `knowledge.matchEntries`、`knowledge.matchTokens` | 8、3000 | 注入提示的条数与 token 上限(调用方读出后传给 `match`、`render`) |
| `knowledge.staleChangeRatio` | 0.5 | 增删行数与写入时行数之比达到即算大改 |
| `knowledge.curate` | opus-mid，1 轮，3m；candidates 5，candidateTokens 6000 | 确认写入时判断的模型、候选条数与 token 上限 |

## 设计依据

- **按路径匹配就够用**：条目少，砍掉旧 `retrieval/` 的 SQLite 索引、全文检索、RRF、MCP、命中计数与检索评测(约 2260 行)；匹配规则沿用旧 `retrieval/context.py:_match`(前缀与精确匹配、越长越靠前)与 `retrieval/locations.py:classify`。
- **不让模型自己搜索**：程序按本次要动的位置把命中的条目放进提示，有条数与 token 上限，省 token(44 号计划「读取(省 token)」)。
- **正文小就放全文**：已接受的取舍等条目总量小时直接放全文，评估要靠它判断是否命中取舍；超过上限只列摘要(旧 `retrieval/context.py:build` 的 inline_documents)。
- **token 估算**：中日韩字符每字算 1，其余非空白字符每 4 个算 1，向上取整，只用于上限判断(旧 `retrieval/text.py:estimate_tokens`)。
- **写入要用户确认**：与复盘「不自动写知识库」一致，自动写入会悄悄改变后面的判断。
- **去重判断的约束**：只能引用候选编号、不合格带原因重试一次、拿不准按新增(误合并会丢信息)、update 与 merge 写完整条目(旧 `retrieval/dedup.py:check_decision`、`skills/learn/references/knowledge-curator.md`)。
- **坏文件不中断**：一个条目文件写坏了，照常沿用它上一次读好的内容并给出警告，不让评估与实施停下(旧 `retrieval/service.py` 的 index_warnings)；上一次读好的内容缓存在 `data/cache/knowledge-entries.json`。
- **条目从不删除**：被推翻的标为已取代并指向新条目，保留历史；写文件先写临时文件再改名(旧 `domain/knowledge.py`、`retrieval/writer.py`)。
- **过期只比对文件**：按写入时的 commit 取 numstat，不调用模型；条目没有 commit 的不检查。

## 不做什么

- 不建索引、不做全文检索与关键词补足、不提供 MCP、不计命中；
- 不自动写入：各步骤只提建议；
- 与代码笔记(`00-issue-notes.json`)不同：笔记只跟着一个 Issue，用完归档；知识库是这个项目长期共用的。
