# prepare：准备

## 是什么

实施的第一步(`implement.prepare`)：建修复分支与 worktree，跑准备命令，在基准 commit 上全量跑项目检查。另管 worktree 的检查点快照(各步交接的 `worktreeCommit`)。

## 流程

1. 分支名：已有 worktree 的分支 → Issue 记下的分支 → 按项目约定新生成(`protocol/git/format`)；名字已被别人占用加 `-2`；
2. worktree：`protocol/git/worktrees.create_fix` 从 `origin/<主分支>` 新建，已存在就复用；`controls.implement.prepare.links` 中的被忽略目录从主工作区软链接进来；
3. 准备命令(`project.commands.prepare`)，再跑基准检查(`baseline.py`)；任一失败判为配置错误，交接为没通过，implement.py 写「出问题」文档转人工。

## 输入与输出

- 输入：Issue(种类、严重度、标题或 `extra.slug`)、项目事实的命令；
- 输出：交接事实 `branch`、`worktree`、`baseCommit`、`worktreeCommit`、`prepareCommands`、`baseChecks`、`log`，没通过时加 `reason`(`branch_rejected`、`config_error`)；`skipped`(null)与 `knowledgeSuggestions`(空列表)由 implement.py 补上；命令输出写在 `31-implement.prepare-log.log`。

## 配置

`controls.implement.prepare.links`、`project.commands`、`limits.timeouts.tests`、`git.*`(分支格式与个人前缀)。

## 设计依据

- 基准检查先跑：基准上已有的失败不能算到修复头上(旧 `FixService._prepared`)；
- 按 commit 缓存(`data/cache/baseline/<commit>-<命令哈希>.json`)：同一基准的多个 Issue 只跑一次(44 号计划第 5 节「优化」)；起不来的命令不缓存，修好环境后会重跑；
- 检查点快照用临时索引与 `commit-tree`：不动 HEAD、分支与暂存区，中断后能精确退回(`protocol/recovery.md`「检查点续跑」)。

## 不做什么

不提交、不推送；不按内容判断受保护文件。
