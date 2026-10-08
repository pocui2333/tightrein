# assess：评估

## 是什么

原来的分诊与立项合成一个阶段：判断采集交来的问题是否存在 → 存在的定严重度(P0 到 P3)、处理方式(立即修、排期修、观察、不修)与粗规模档 → 要修的写成 Issue(`issue/`)并等放行。采集只发现与记录，判断只在这里做一次。

## 流程

`assess_pending(runtime)`(调度调用)与 `assess(runtime, problem)`(指定一个问题)：

1. **选题**(`select.py`)：每个问题只评估一次——状态为 new、regressed，或用户请求了重新评估；转人工的带 manual 标记不再自动评估。按五档排序：预估 P0(越权)→ 服务端报错 → 回归 → 其他运行时问题 → 静态、任务外发现与响应过慢，同档按末次出现倒序。
2. **只读 worktree**(`assess.readonly`)：切到 origin/主干最新 commit，切换失败整次停下(`AssessBlocked`)；评估期间去掉写权限，结束后恢复。
3. **并行**：位置或文件相同的问题归一组(`select.related_groups`)，组与组之间并行(并行数 `resources.concurrency.modelCalls`，每组自己的数据库连接)。每个问题先拿对象锁，锁被占就跳过；额度到了给用户留的余量就不再开始新的问题。
4. **主张**(`claims.py`)：只放观察到的事实，事实带序号；信号样本按(角色, 位置)去重；附「主干差异」(`main_diff.py`)。
5. **分情况**(`cases.py`)：已取证(复用采集时的取证)、能复现(只找出错位置)、完整取证、轻量推测(只推测触发条件)。
6. **查重**(`dedup.py`)：程序先比(位置、文件、标题相似度)，明显重复的直接并入、明显不相关的不算候选，拿不准才调 `assess.dedup`；模型的判断由程序核对。
7. **取证**(`evidence.py`)：调 `assess.triage`，输出先补全位置，再做证据检查(`checks.py`，全部程序判定)，不过就带逐项原因重做，仍不过判证据不足并转人工。
8. **证伪复核**(`refute.py`)：只在 P0、P1 或安全、数据类(配置)时调 `assess.refute`，盲审、模型与取证不同；两次结论按合并表定。
9. **归因**(`attribution.py`)与**取舍核对**(`tradeoff.py`)。
10. **评级**(`rating.py`)：严重度以取证报告为准；粗规模档取模型的档与按文件数算的档中较大的。
11. **去向**(`disposition.py`)：决策树给出处理方式，按顺序定去向。
12. **落库**(`persist.py`)：一个事务内写问题状态、评估记录、抑制规则、并入、写成 Issue；失败全部回滚。
13. 每个问题写 `21-assess.triage-handoff.json`(问题目录)，建 Issue 时另写 `22-assess.issue-handoff.json`(Issue 目录)。

另有：`retriage(runtime, problem, note)`(用户补充信息后重新评估，补充跨次累积)、`override(runtime, problem, verdict, reason)`(用户改判，不调用模型)。

## 输入与输出

- 输入：problems、occurrences 表(采集的去重写入)；知识库(`knowledge.match`)；只读 worktree。
- 输出：`AssessOutcome`(44c：problem、verdict、severity、disposition、issue、status，另加 destination、merged_into、issues、reason)。
- 问题状态的去向：要修 → `ongoing`(带 Issue)；证据不足 → `watching`(再出现时由采集提升为 new，带新证据重新评估)，连续 `insufficientToManual` 次仍不足 → 转人工(状态不变、带 manual 标记、写 `90-problem-pending.md`)；观察 → `muted`(再出现 N 次或严重度升级时恢复)；不成立、不修、取舍、并入 → `closed`(不成立另生成抑制规则)；主干上已修 → `ongoing` 等部署。
- 文件(问题目录)：`00-problem-notes.json`(代码笔记)、`21-assess.triage-evidence.json`(主张与取证、复核的完整输出)、`21-assess.triage-handoff.json`；误判回填写 `21-assess.triage.r<第几次>-handoff.json`(必填事实 `misjudged`)；`00-problem-assess.json`(评估对问题的最后一次改动的整行快照，带运行编号，供重建)。
- problems.extra：`assess`(评估记录：第几次、判定、去向、根因、上一次的摘要、实际结果等)，另把 `verdict`、`severity` 写在顶层(status、watch 读取)。
- 评估之后对问题的改动(写成 Issue 时关联、Issue 关闭时连带关闭或忽略、并入、回填误判、请求重新评估)都经 `persist.update`，同时写库与 `00-problem-assess.json`；事务回滚时这些文件一并恢复(`persist.restoring`)。
- 交接必填事实(`handoff.schema.json`)：problem、verdict、severity、disposition、destination、issue、issues、mergedInto、`knowledgeSuggestions`(建议沉淀，字符串列表；取证输出给的)、`misjudged`(误判 `{kind: false_confirm | false_refute | false_block, point, detail}`，没有为 null；复盘读它)，另有 case、commit、rootCauses、missingInfo、`incidentalFindings`(采集的任务外发现读它)等。
- 误判的来源：Issue 以「不是缺陷」关闭、修复前复现不了记 `false_confirm`(回填到当次评估，另写 `.r<第几次>` 交接)；用户把「不成立」改判为成立记 `false_refute`(写在改判的交接)；当次结论已有实际结果的不再回填(由更早的一条负责)。

### 从文件重建(`admin rebuild`)

problems 先由采集重放去重的变更日志(`collect/dedup/output.py` 登记的重建函数)；issues 从各 Issue 目录的 `00-issue-record.json` 重建，随后 `persist.replay` 把比该问题最后一份采集快照更新的评估快照整行覆盖上去(store/rebuild.py 的 `rebuild_issues`)。两边都是整行快照，按运行编号的时间取新的那份，所以合起来就是最后的状态。

### 代码笔记(`notes.py`)

评估读了代码就留下：相关位置、一句实际实现、推测的触发条件。格式与实施的代码摘要相同(44c 的 `CodeNotes`、`NoteEntry`)：核心位置由程序截取原文(前后 2 行，最多 40 行)，相关位置只记签名。实施复用 `build_entries`、`CodeNotes.add`(只补缺的)、`load`、`save`；写成 Issue 时复制为 `00-issue-notes.json`。

### 调用点与输出格式

| 调用点 | 提示 | 输出格式 |
|---|---|---|
| `assess.triage` | `prompts/assess.triage.md` | `assess.triage.schema.json` |
| `assess.refute` | `prompts/assess.refute.md` | 同上(盲审，输出与取证相同) |
| `assess.dedup` | `prompts/assess.dedup.md` | `assess.dedup.schema.json` |

`prompts/`(本文件夹内)只给变量的值：`common.py`(拼提示、取参数、调用、累计量化数据)、`claim_verifier.py`、`dedup.py`。

## 配置

`settings/defaults.json` 的 `controls`：

| 键 | 缺省 | 含义 |
|---|---|---|
| `assess.perRun` | 10 | 每次运行评估的问题数上限 |
| `assess.claimSamples` | 5 | 主张中的信号样本数 |
| `assess.insufficientToManual` | 3 | 连续几次证据不足才转人工 |
| `assess.watchReopenOccurrences` | 3 | 观察的问题再出现几次后恢复评估 |
| `assess.suppressionDays` | 30 | 判为不成立时抑制规则的有效天数 |
| `assess.severityGuide` | null | 项目语境下的严重度说明(项目在工作区 settings 里写) |
| `assess.dedup` | 90 天、10 个、0.9、0.5 | 查重候选的天数与个数；标题相似度：不低于 sameTitle 且位置相同直接判重复，低于 differentTitle 且位置不相交不算候选 |
| `assess.refute` | P0、P1；security、data；authorization、data-ownership、data-correctness、credential-leak | 证伪复核的触发条件 |
| `assess.sizes` | small ≤ 3 个文件、medium ≤ 10 个 | 粗规模档的文件数门槛 |
| `assess.treatment` | 七条规则 | 处理方式的决策树，最后一条不带条件 |
| `assess.vagueWords` | 中英日的含糊词 | 结论中不许出现的词 |
| `assess.triage.rounds`、`assess.dedup.rounds` | 1 | 检查不过时交回重做的次数 |
| `knowledge.matchEntries`、`knowledge.matchTokens` | 8、3000 | 放进提示的知识条目上限 |

模型与独立性：`assess.triage`(opus)、`assess.refute`(flash-high，`independence` 要求与取证不同)、`assess.dedup`(flash)。

## 设计依据

- **三种情况分别处理**：已取证的复用、能复现的不再证明存在、没法复现的只轻量推测，按已知信息决定花多少 token(44 号计划「分情况判断」)。
- **证伪复核只用于高风险**：P2、P3 取证一次即定，复核的结论分歧才转人工；复核是盲审且模型不同，避免同一个模型的同一个盲点(旧 `pipeline/triage/steps/refute.py`、`prompts/claim_verifier.py`)。
- **查重先用程序**：明显重复或明显不重复的不调模型；错误合并会吞掉真问题，所以拿不准与模型输出不合格时都按不同根因(旧 `pipeline/triage/steps/dedup.py`)。
- **证据检查全由程序判定**：位置真实、反证追到入口、不成立指出来源、无含糊措辞，都是可以机器核对的；不另设模型评审(旧 `evaluation/rubrics/triage.json`、`scorers/code.py`)。位置补全只认唯一后缀、只去开头的 `./`(2ef30cc 修复)。
- **证据不足先观察**：偶发的问题下次出现时带着新证据自动重评，连续几次仍不足才打扰用户。
- **只给粗规模档**：实施出方案时会重估，评估只要能分流。
- **P0 排在取舍之前**：不会被一条已接受的取舍吞掉；取舍编号由程序核对，防止模型编号吞问题(旧 `domain/triage.py:disposition`、`steps/tradeoff.py`)。
- **一个问题一个事务**：结论、状态、抑制、并入、建 Issue 一起成功或一起回滚，Issue 文件与问题的评估快照同时恢复(旧 `steps/persist.py`)。
- **判不成立的 P0 必经复核**：不论哪种情况，P0(预估或取证给的)判不成立都要复核且复核也不成立才算，避免一次取证就关掉越权类问题(旧 `domain/triage.py:disposition`)。
- **并行只在不相关的问题之间**：位置或文件相同的放一组按顺序做，后一个查重时能看到前一个的结论。

## 不做什么

- 不细估改动行数与复杂度分档(四档规模已砍)；
- 不做同文件合并分诊(第二批)；
- 不发本机通知(协议层「本机通知：不发」)，要人看的写进 `90-…-pending.md`；
- 不自动写知识库：只在交接里提 `knowledgeSuggestions`。
