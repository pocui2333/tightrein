# locate：定位

## 是什么

`implement.locate`：补全代码笔记中缺的部分。评估留下的笔记有核心位置、且在修复 worktree 中仍成立时跳过，不调用模型。

## 流程

1. `brief.sufficient` 判断笔记够不够用；不够时把已有笔记交给模型，只补缺的(已不成立的核心位置写进需要处理的问题)；
2. 程序先补全位置(`brief.complete`，与评估共用 `assess/checks.complete`：只写了文件名或缺前几级目录的，按 worktree 中唯一的路径后缀补全；只去掉开头的 `./`)，再逐个核对(`brief.location_problem`，与评估共用 `assess/checks.Snapshot.problem`：`文件:行号[-止]` 或 `{file, line}`，在 worktree 之内，行号不越界)，不合格带原因重做，上限 `controls.implement.locate.rounds`(缺省继承 `*` 的 1)；
3. 通过后程序截取原文与签名补进 `00-issue-notes.json`(`assess/notes.py`：核心截原文前后 2 行、最多 40 行，相关只记定义行，同一位置只记一次、核心优先)。

## 输入与输出

- 输入：Issue 正文、已有代码笔记、知识条目、用户的决定；
- 输出：交接事实 `skipped`(跳过的原因，没跳过为 null)、`added`、`files`、`trigger`、`missing`、`affectedEndpoints`、`affectedPages`、`incidentalFindings`、`baseCommit`、`knowledgeSuggestions`；没通过时为 `problems`；模型输出格式 `locate.schema.json`。
- `incidentalFindings`：任务外发现，每条结构照 `collect/incidental/finding.schema.json`(schema 中复制同样的结构，类别限 defect、security、performance、data)；`baseCommit` 是定位时的基准 commit，采集任务外发现时据此记发现的版本(`controls."collect.incidental".points`)。

## 配置

`controls.implement.locate`(模型、时限、轮数)；笔记涉及前端文件时带 `frontend` 条件选模型。

## 设计依据

- 先搜索后阅读、有着落即停、错误的「已有实现」比「无」危害大(#5、#8：曾单次 340 万 token、同一文件反复读 5 次)，写在 `prompts/implement.locate.md` 与 `prompts/common/notes.md`；
- 位置由程序核对、原文由程序截取，不来自模型(#5)；位置核对与补全只有一份实现，评估的证据、定位、方案的根因假说共用(旧 `evaluation/scorers/code.py:location_problem`、`pipeline/common/locations.py:complete`)。

## 不做什么

不给修复代码，不评代码风格；重出方案时不再回到定位。
