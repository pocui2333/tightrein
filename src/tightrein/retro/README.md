# retro：复盘

## 是什么

tightrein 自身问题的记录簿。每次运行结束都检查一遍，把 tightrein 自己运行中的问题(不是被管理项目的缺陷)记下来，附简单的解决思路与评级；用户想改进 tightrein 时统一翻看、挑着改，改时遵守 [improve.md](improve.md)。复盘不自己改任何东西，也不写知识库。

| 文件 | 内容 |
|---|---|
| `retro.py` | 入口 `retro(runtime) -> RetroOutcome`：检测 → 按指纹合并或新建 → 评级 → 新记录写解决思路 → 落盘交接 |
| `detect.py` | 从本次运行的 runs、events.jsonl、各对象目录的 `handoff.json` 与 `started.json` 找出失败、浪费、误判、打扰 |
| `records.py` | 记录簿：读写、指纹、编号、状态、列出 |
| `rating.py` | 评级(影响 × 次数) |
| `idea.py` | 新记录的解决思路(调用点 `retro.idea`，模板 `prompts/retro.idea.md`，输出 `idea.schema.json`) |
| `retro.schema.json` | 复盘交接的必填事实 |
| `improve.md` | 改进规范：拿记录去改 tightrein 时的规则 |

## 流程

1. 读出已有记录，建「指纹 → 记录」的字典；
2. `gather`：本次运行碰过的对象 = events.jsonl 中的对象 ∪ 运行目录 ∪ issues、problems 表中本次开始后更新过的；只读这些对象目录中 `run` 等于本次运行的交接与调用标记；
3. `detect`：五项检测各自独立，某一项出错只记进 errors，其余照常算：

   | 检测 | 类型 | 现象(英文短名) | 影响 |
   |---|---|---|---|
   | 运行状态为 failed、interrupted | 失败 | `run-failed`、`run-interrupted` | severe |
   | 模型调用没有结束 | 失败 | `call-unfinished` | major |
   | 模型调用不是 ok | 失败 | `call-<状态>` | 认证、额度、工具不可用为 severe；越界为 major；其余 minor |
   | 单次调用耗时超过 `callDuration` | 浪费 | `call-slow` | minor |
   | 交接状态 failed | 失败 | `step-failed` | severe(停下要用户处理) |
   | 交接状态 pending | 打扰 | `waiting-user` | trivial |
   | 单步 token 超过 `stepTokens` | 浪费 | `step-tokens` | 达到 `bigTokens` 为 major，否则 minor |
   | 单步耗时超过 `stepDuration` | 浪费 | `step-slow` | minor |
   | 单步轮数超过 `rounds` | 浪费 | `many-rounds` | minor |
   | 单步读代码超过 `linesRead` 行 | 浪费 | `read-much` | minor |
   | 单个 Issue 累计 token 在本次越过 `issueTokens` | 浪费 | `issue-tokens` | 本次就用掉 `bigTokens` 以上为 major |
   | 交接必填事实中的 `misjudged` | 误判 | `false-confirm`、`false-refute`、`false-block` | major |

4. 同一次运行中指纹相同的发现合成一次出现；指纹已有记录的追加这次出现并重新评级，没有的新建并调用一次模型写解决思路；
5. 落盘 `data/runs/<运行>/51-retro.detect-handoff.json`：新建与追加的记录、各自评级、errors。

## 输入与输出

- 输入：`Runtime`；不读模型输出原文，只读程序写下的交接与标记；
- 输出：`RetroOutcome`(新建、追加的记录编号，评级，errors，量化数据)；记录文件 `data/retro/<编号>-<评级>-<位置>-<英文短名>.md`；
- 其他阶段要配合的(写进各自交接的必填事实)：
  - 误判：`facts.misjudged = {"kind": "false_confirm" | "false_refute" | "false_block", "point": "<当初判断的调用点>", "detail": "<一句话>"}`；修复前复现不了、用户以「不是缺陷」关闭记 `false_confirm`，用户把「不成立」改判为成立记 `false_refute`，审查报的阻断项被证明是误报记 `false_block`。

### 每条记录

头信息(YAML)存全部数据，正文按「结论 → 必填事实 → 每次出现」渲染给人看：

| 字段 | 内容 |
|---|---|
| id、rating、status | 四位编号；P0 到 P3；open(待看)、adopted(已采纳)、done(已处理)、wontfix(不处理) |
| kind、point、callPoint、phenomenon | 类型；阶段与小步骤；调用点(去掉末尾序号)；现象的英文短名。四者合成指纹 |
| fact | 现象：只写事实，不含某一次的数字 |
| firstSeen、lastSeen、count | 首次与最近发现时间、累计出现次数 |
| idea、settle | 解决思路；建议沉淀为经验或规则(用户采纳后才做) |
| occurrences | 每次运行一段：运行编号、时间、次数、对象、逐条细节、影响(多花的 token、时间、返工轮数) |

### 评级

| 评级 | 条件 |
|---|---|
| P0 | severe(整轮失败、卡死、需要用户救场)且出现次数达到 `repeatCount` |
| P1 | 只出现一次的 severe；major(大量浪费、误判)；minor 出现次数达到 `frequentCount`(频繁返工) |
| P2 | 偶发的浪费或失败(minor) |
| P3 | 小问题、体验、按设计停下的关卡(trivial) |

### 查看与关闭(命令在 cli，程序在 records.py)

- `tightrein retro list`：`listing`，按评级、次数列出待看的；
- `tightrein retro show <编号>`：`get`，详情与每次出现的细节；
- `tightrein retro close <编号> --done | --wontfix`：`set_status`。

记录存在本机工作区，不进 git(有项目名与运行细节)。

## 配置

`settings/defaults.json` 的 `controls`：

| 键 | 缺省 | 含义 |
|---|---|---|
| `retro.stepTokens` | 300000 | 单步 token 阈值(缓存读取按 `resources.cacheReadWeight` 计) |
| `retro.bigTokens` | 1000000 | 达到即为大量浪费 |
| `retro.issueTokens` | 1000000 | 单个 Issue 累计 token 阈值 |
| `retro.stepDuration`、`retro.callDuration` | 20m、10m | 单步、单次调用的耗时阈值 |
| `retro.rounds` | 2 | 同一步来回的轮数阈值 |
| `retro.linesRead` | 5000 | 单步读代码的行数阈值 |
| `retro.repeatCount`、`retro.frequentCount` | 2、3 | P0 的反复次数、minor 算频繁的次数 |
| `retro.idea` | opus-mid，1 轮，2m | 写解决思路的模型与上限 |

## 设计依据

- **只记录，不自己改**：旧学习阶段自动写经验、自动生成规则，会悄悄改变后面的行为；改为记下问题与思路，用户采纳后才改(44 号计划「8 复盘」「不再做的」)。
- **发现、合并、评级都由程序做**：只有新建记录时调一次模型，复盘本身几乎不花 token。
- **按指纹合并**：同一个问题每次运行都会出现，按「类型 + 阶段与小步骤 + 调用点 + 现象」合并成一条、累计次数，评级才能反映「反复出现」；现象只用英文短名，不含数字，否则每次都会是新记录。
- **调用点归组**：角色名末尾的序号与缺陷模式编号去掉后再算指纹，同一调用点的多次调用并到一起(旧 `pipeline/learn/steps/yields.py:role_group`)。
- **改判由上一条负责**：改判记录的上一条已判为误判时不重复计数(旧 `pipeline/learn/steps/troubles.py:misjudged`)。
- **一项出错不影响其余**：复盘在运行末尾，不能因为一个读不懂的文件或一项检测的异常让整次复盘落空(旧 `pipeline/learn/steps/metrics.py:compute`)。
- **不处理的只追加不再提醒**：用户拒绝过的不反复打扰(旧 `pipeline/learn/steps/suggestions.py:store`)；已处理的再出现说明没修好，重新列为待看。
- **Issue 用量只在越过阈值的那次运行记**：按本次之前与之后的累计判断，越过之后的运行不再重复记。

## 不做什么

- 不记被管理项目的缺陷；
- 不自动写知识库、不自动生成规则、不评测改进建议、不出周报；
- 不判断「判为误报是否判对」(需要之后的采集覆盖情况，由去重回填到 `problems.extra.assess.outcome = correct`，见 collect/dedup/README.md)；复盘只记已被证明的误判。
