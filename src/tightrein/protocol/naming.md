# 文件命名与排版(naming)

## 是什么

所有目录、文件名、编号、时间与时长的写法，以及 Markdown 与 JSON 的排版。程序在 `naming.py`(编号、时间、时长、控制键、文件名)，路径只在 `store/files/layout.py` 一处计算。

## 怎么做

### 目录与文件名

| 对象 | 规则 | 例子 |
|---|---|---|
| 目录(不论是否含程序) | 小写英文加下划线 | `platform_errors/`、`api_fuzz/` |
| 程序文件 | 小写英文加下划线 | `log_signals.py` |
| 方法清单 | 与程序同名 | `sentry.py`、`sentry.yaml` |
| 方法文档 | 两位序号加英文名 | `01-run-records.md` |
| 提示模板 | 控制键加 `.md` | `assess.triage.md` |

### 控制键

调用点、配置、文件名共用一套键：`阶段.模块.小步骤`，小写英文，`.` 隔开层级，`_` 只出现在模块名内部(`collect.platform_errors.log_parse`)。第一段是阶段(`collect`、`assess`、`implement`、`release`、`retro`)或 `protocol`、`agents`、`knowledge`、`onboard`。

控制字段按「小步骤 → 模块 → 阶段 → `*`」继承：`implement.design.frontend` 没写的字段取 `implement.design` 的，再取 `implement`，最后取全局缺省 `*`(`key_chain`)。

### 运行中产生的文件

一个对象(问题、Issue、运行、复盘记录)的全部文件放在它自己的目录里，只看文件名就知道是哪个阶段、哪个模块、哪个小步骤、第几轮、什么文件，`ls` 出来就是流程顺序：

```
<序号>-<阶段>.<模块>[.<小步骤>][.r<轮>]-<内容>.<扩展名>
```

| 部分 | 规则 |
|---|---|
| 序号 | 两位：十位是阶段(1 采集、2 评估、3 实施、4 发布、5 复盘)，个位是阶段内顺序；`00` 为对象共用，`90` 为给人看的文档 |
| 控制键 | 与配置同一套写法；没有小步骤的省略 |
| 轮次 | 会多轮的步骤加 `.r1`、`.r2`；第 2 轮与第 1 轮序号相同，排序时紧挨在一起 |
| 内容 | 只取词表：`handoff`、`prompt`、`raw`、`started`、`diff`、`log`、`evidence`、`notes`、`body`、`pending`、`failure`、`deliver` |
| 扩展名 | `.json` 给程序，`.md` 给人看，其余按实际格式 |

```
data/issues/0018/
  00-issue-body.md
  00-issue-notes.json
  21-assess.triage-handoff.json
  35-implement.code.r1-handoff.json
  35-implement.code.r1-diff.patch
  35-implement.code.r2-handoff.json
  37-implement.review.r1-handoff.json
  90-issue-pending.md
data/runs/R-20261007T093000Z-collect/
  12-collect.platform_errors-handoff.json
data/retro/0003-P1-implement.review-no-progress.md
```

`prompt` 与 `raw` 只在这一步失败或开了调试时保存；`started` 是模型调用开始时写下的参数，中断后排查用。

### 编号

| 编号 | 写法 | 例子 |
|---|---|---|
| Issue | 四位补零 | `0018` |
| 问题 | `P-` 加四位 | `P-0003` |
| 运行 | `R-<UTC 时间>-<阶段>`，精确到秒 | `R-20261007T093000Z-collect` |
| 复盘记录 | 四位补零 | `0003` |

问题与 Issue 编号不重叠，命令行按写法自动识别种类(`kind_of`)。同阶段同一秒已有运行时顺延一秒(`store/tables/runs.free_id`)。

### 时间与时长

- 当前时间只经注入的 `Clock` 取得，测试与 `--now` 用 `FixedClock`；
- 存储一律 ISO 8601 UTC、去微秒、`Z` 结尾，拒绝没有时区的时间；文件名中写成 `20261007T093000Z`；显示时按本地时区，「今天」按本机时区的日期；
- 时长一律写成数字加单位：`500ms`、`30s`、`20m`、`2h`、`90d`，不再混用秒、分钟、毫秒。

### 路径段

编号、角色、简称等外部来的片段拼进路径前先过 `segment`：含 `/`、`\`、`\0` 或为 `.`、`..` 即拒绝。

### 排版

- Markdown：头信息之后按「结论 → 必填事实 → 量化数据 → 备注」排列；必填事实与量化数据用表格；
- 给人读的文字按项目语言(中、英、日)；字段名、路径、命令、代码保持原样；
- JSON：UTF-8、两个空格缩进、字段名小驼峰；不适用的字段写 null，不省略；
- 文件一律 UTF-8、换行 LF、末尾留一个空行；写文件一律经原子写(`store/files/atomic.py`)。

## 在哪配置

不可配置：命名是各模块之间的约定，改了就对不上。文件序号登记在 `naming.py` 的 `STEP_SEQUENCE`，内容词表在 `CONTENT_WORDS`。

## 缺省值

无。

## 设计依据

- **序号 + 控制键 + 轮次**：人不打开文件也能看出流程位置；序号表示「在流程中的位置」，轮次表示「第几次」(44 号计划「文件命名与排版」)。
- **运行编号精确到秒并顺延**：同一秒的两次同类运行会互相覆盖记录与目录(旧 `store/repos/runs.py:free_id`、`orchestrator/runlog.py:Pacer`)。
- **时间只经注入的 Clock**：测试可重复，`--now` 可回放(旧 `domain/clock.py`)。
- **路径段校验**：防止外部输入把文件写到预期目录之外(旧 `store/files/layout.py:segment`)。
- **目录一律下划线**：Python 包名不能有连字符，只放文档的目录也照此，保持一种写法。
- 时间格式：ISO 8601(https://www.iso.org/iso-8601-date-and-time-format.html)。

## 中文与英文(给人看的输出)

原则：给人读的说明用项目的输出语言(`settings.json` 的 `project.language`：`zh` 或 `en`)；能复制、能搜索、要与文件或代码对得上的标识保持英文原样。命令行的文字都放在 `cli/text/<语言>.json`，程序按语言取，不在代码里写死；两版的布局、行数、颜色、英文标识相同，只换说明文字。

| 随语言变化 | 始终保持英文原样 |
|---|---|
| 区块标题(等你处理、进行中、各阶段) | 命令(`tightrein approve 0019`) |
| 阶段与小步骤名(中文版写「实施·编码」「评估」) | 编号(`0019`、`P-0003`、`R-20261007T013000Z-implement`) |
| 状态(就绪、待审核、出问题、不通过) | 文件路径与代码位置(`src/notes.ts:88`) |
| 判定与原因(成立、证据不足、被安全分类拒绝) | 工具与模型名(`claude`、`agy`、`opus`) |
| 标签(已等、累计、交回、留量) | 控制键(`implement.code`、`platform_errors`) |
| 采集来源名(中文版写「平台错误」「访问日志」) | 通用缩写：PR、CI、P0 到 P3、token、diff、lint、worktree |
| 时间说明(后、前、剩) | 单位与数量写法：`42m`、`1h18m`、`3d`、`1.2M`、`120k` |
| | commit 哈希；Issue 标题按原文，不翻译 |

- 英文版中阶段、小步骤、来源直接用控制键原名(`implement.code`、`platform_errors`)，不另起英文名，与配置和文件名一致；
- 中文与英文、数字相邻时加一个空格：「已等 48m」「4 文件」「新 Issue 4」；中文标点与英文、数字之间不加空格：「（41m 后）」；
- 时长与数量只用 `naming.py` 的 `format_duration`(`12s`、`42m`、`1h18m`、`3d`)与 `format_count`(`999`、`120k`、`1.2M`)，两种语言写法相同，不写「42 分钟」「1.2 百万」；
- 加一种语言(如日文)只加一份文案表；换语言时只换上表左列，右列始终不变。
