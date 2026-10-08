# 记录

记录链(每次运行的事件日志)、版本、保留期。程序在同名的 `records.py`；保留期的清理在 `store/retention.py`。

机器的进度只存一份，给人看的全部由它渲染：当前状态在 store 的 issues、problems 表，每个对象的历史在对象目录中每一步的 `handoff.json`，每次运行的历史在 store 的 runs 表与这里的事件日志。

## 记录链(events.jsonl)

**是什么**：每次运行一个 `events.jsonl`，放在运行目录 `data/runs/R-<时间>-<阶段>/`(路径由 `WorkspaceLayout.events(run)` 给出)。

**每行一个事件**：

| 字段 | 内容 |
|---|---|
| `at` | 时间，ISO 8601 UTC |
| `run` | 运行编号 |
| `subject` | 对象编号(Issue `0018`、问题 `P-0003`)；与对象无关的为 null |
| `point` | 调用点(控制键)，如 `implement.code` |
| `kind` | 种类：`trigger`(触发)、`decision`(决定)、`action`(动作)、`effect`(影响) |
| `summary` | 一句话 |
| `refs` | 引用：交接文件路径、幂等键等 |

**怎么做**：

- 只记关键事件：运行开始与结束、每一步开始与结束、写操作、到关卡、失败与停下；不记每一次工具调用(工具的会话记录只在失败或调试时保存为 `raw`)；
- 量化数据不在这里重复，引用 `handoff.json`；
- 每行以追加模式(`O_APPEND`)用一次 `os.write` 写入：多个进程同写一个文件时行与行不交错；
- 写入失败不抛出，原因记进 `EventLog.failures`，由运行摘要报告「日志写入失败」：日志写不了不该让正在进行的修复停下；
- 只对 `summary` 脱敏(`security.md`「脱敏」)；编号、时间、调用点、引用不处理，避免把编号与哈希中的数字串误判成手机号等脱掉；
- 种类不在四种之内是编程错误，直接抛 `ValueError`。

**在哪配置**：不可配置。

## 版本

**是什么**：每份 `handoff.json` 记下产生它时的 tightrein 的 commit、所用提示文件的哈希、settings 的哈希、工具名与版本、模型(`versions()`)。

**怎么做**：

- tightrein 的 commit 直接读仓库的 `.git`(HEAD、松散引用、`packed-refs`，worktree 的 `gitdir` 与 `commondir`)，不起 git 子进程：每份交接都要取一次；不是 git 仓库(如从安装包运行)时为 null；
- settings 除 `secrets.json`、`sites.json` 外都在 git 中，git 历史就是配置的版本记录，不另设版本系统。

## 保留期

| 内容 | 缺省 | 出处 |
|---|---|---|
| 运行目录与 `events.jsonl` | 90 天 | GitHub Actions 日志缺省 90 天(docs.github.com/en/organizations/managing-organization-settings/configuring-the-retention-period-for-github-actions-artifacts-and-logs-in-your-organization) |
| `prompt`、`raw` | 30 天 | 推断：可能含代码与密钥，比日志短 |
| Issue 归档目录(交接文件、三种文档) | 永久 | 推断：体积小，复盘与追溯要用 |
| 复盘记录 | 永久 | 推断 |
| worktree | 验收通过后删除 | 发布阶段已定 |

运行目录的年龄取自运行编号中的时间，不依赖文件修改时间。

**在哪配置**：`settings` 的 `records.retention`(`runs`、`raw`)。

## 设计依据

- 记录链(谁在何时因何做了什么、引用哪份产物)：AI agent guardrails checklist 的审计一项(dev.to/brennhill/ai-agent-guardrails-a-practical-checklist-42be)。
- 一行一次 `write` 加 `O_APPEND`：POSIX 规定以 `O_APPEND` 打开时每次写入前把偏移移到文件末尾，且这一步与写入之间不会插入别的修改(pubs.opengroup.org/onlinepubs/9699919799/functions/write.html)；本机文件系统上一次写入的整行不会与其他进程的行交错。
- 写失败不抛：日志是旁路，不能让它决定主流程成败；失败要被看见，所以记下并在运行摘要中报告。
