# code：编码

## 是什么

`implement.code`：唯一可写的小步骤，照已确认的方案改代码；逻辑、接口、数据处理类改动同时写测试，编码过程中只跑受影响的测试。

## 流程

1. 核对定案确认的就是当前方案(`approve.confirmed`)；
2. 修正说明：上一轮没通过那一步中最靠前一类的局部问题；越界撤回后的重做写明越界原因；
3. 第二轮起按上一轮自检的结果记检查点或退回(`checkpoint.py`)，退回说明放在修正说明最前面；
4. 调用模型：同一版方案、同样条件时续接上一轮的会话(`implement.code.continue`，不再重复角色与规则)，否则新开(`implement.code`)；
5. 程序检查本轮改动：越出 worktree、碰了禁改文件即撤回本轮全部改动(第一次由 implement.py 安排重做)；调用失败为局部问题；模型中止(`aborted`)为方案缺口。撤回用检查点恢复(`prepare/workspace.restore`：内容、暂存区与分支一起回到这一轮开始时，agent 自己的提交也退掉)；恢复本身失败不抛出，交接加一项需要用户的 `restore_failed`。撤回过的交接把检查点(`worktreeCommit`)记成这一轮开始时，不由 implement.py 现取快照：恢复失败时 worktree 停在半截状态，记成检查点后每次续跑都会退回它；记成这一轮开始时，用户处理好之后续跑再恢复一次就回到正常状态。

发布合并 main 冲突退回编码时(`merge.py`)：调用模型之前程序合并发布记下的 main(`merge --no-ff`)，这一轮不记、不退检查点；本轮改动相对合并进来的 main 算。冲突按暂存区里各文件占的阶段(`ls-files --unmerged`)分两类：

- 两侧都有、文件里留了冲突标记的：冲突说明(这些文件、main 一侧的相关提交、保留两侧意图的要求)放在修正说明最前面，由模型改文件解决；
- 没有冲突标记的(一侧删了另一侧改了、改名、二进制文件等)：改文件解决不了，取哪一侧都会丢掉另一侧的改动。用户还没选时放弃这次合并、不调用模型，交接为需要用户，带上关卡内容(`gate`：逐个文件列出冲突类型，`approve --option 1` 保留修复一侧、`--option 2` 保留 main 一侧；只认这个关卡上的决定，重出方案后定案关卡上的 `--option` 是给需要拍板的点选的)；选了之后(之后的轮次沿用)程序合并时把这些文件写成那一侧(那一侧没有这个文件就删掉)，冲突说明写明已按用户的选择处理，留冲突标记的照常交给模型。

模型返回后：冲突标记都去掉了由程序完成合并提交(只暂存冲突文件，删掉的记为删除，其余合并结果已在暂存区)；还留冲突标记、调用失败、中止或越界就放弃合并，worktree 连同分支回到合并前，下一轮重新合并；模型中止说明两侧是同一功能的互斥实现，归为需要用户，由用户选保留哪一侧。合并与合并提交不记幂等键：它们会被放弃、会因中断随检查点退回，做没做以那个 main 是否已是 HEAD 的祖先为准。交接另写 `mergedMain`(`main`、`conflicts`、`unmergeable`(`path`、`kind`)、`side`、`commit`)。

## 输入与输出

- 输入：方案中编码要用的字段(`PLAN_FOR_CODE`，根因假说不带证据)、前端设计说明、验收标准、代码笔记、允许的命令、修正说明、用户的决定、知识条目；
- 输出：交接事实 `changedFiles`、`linesAdded`、`linesDeleted`、`testsWritten`、`verification`、`deviations`、`incidentalFindings`、`baseCommit`、`outOfScope`、`knowledgeSuggestions`、`release`(提交与 PR 的文字)、`diffHash`、`sessionId`、`conditions`、`corrections`、`rolledBackTo`、`violations`、`redo`、`blockers`；改动量与 `diffHash` 只在本轮通过时写(越界撤回、调用失败时没有可计的改动)；模型输出格式 `code.schema.json`。
- `incidentalFindings`：任务外发现，每条结构照 `collect/incidental/finding.schema.json`；`baseCommit` 是本轮的比较基准，采集任务外发现时据此记发现的版本。

## 配置

`controls.implement.code`(模型、`access: write`、轮数、时限、`checkpointRollbackFindings`)、`project.commands`。

## 设计依据

- 同一会话续接(旧 `fix_executor.task(continued)`)：用上提示缓存；
- 修正模式的禁令与「自述只写证据」(旧 `fix-executor.md`、`fix-rules.md`)写在 `prompts/implement.code.md`；
- 检查点与回退(旧 `checkpoint.py`，38-external-techniques 第 3 项)：触发条件中的「复现检查通过」改为「编码写的测试与受影响测试通过」(自检交接中测试命令的结果)；
- 越界撤回重做一次(`protocol/boundaries.md`)。

## 不做什么

不写「修复前失败」的复现测试；agent 不执行 git 写操作(解决冲突时的合并与合并提交也由程序做)；改动量超上限交给自检按「先收敛一次」处理。用户的决定与补充高于方案：照用户的决定做，偏离方案的写进 `deviations`。
