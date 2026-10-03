---
name: release
description: 用 tightrein 把验证通过的修复送进 main：提交、同步主干、推送、提 PR，每个 git 写操作都生成待确认操作、由用户逐个确认(项目声明不逐次确认时直接执行)；之后查看 PR、部署与生产发布的状态，按规则自动合并或写决策简报，回归时提撤销 PR，生成工作总结与 PR 回复草稿，最后由用户发起清理。合并前验证通过后使用。
---

# release

发布是纯脚本：`tightrein release` 生成待确认操作，确认后由本工具执行。**缺省本工具不合并 PR**，PR 由用户在 GitHub 上审核并合并或关闭；**不涉及生产发布**。不要自己执行任何 git 写操作。

项目可以在 `project.yaml` 的关卡表 `gates` 与 `release` 段声明(`tightrein config show` 可查看生效值)：

- `gates.release-writes: auto`：建修复分支、提交、合并主干、推送修复分支、提 PR、在 PR 上发 AI 评审结论、提撤销 PR 由本工具直接执行，输出写明「已直接执行」，不再出现这些待确认操作；转述执行结果即可。
- `gates.merge: auto`：`release track` 判断合并条件(PR 阶段的检查通过、评审没有未处理的阻断意见、CI 必需检查没有失败、PR 头部是本工具推送的 commit、没有人请求修改)。改动涉及 `release.autoMergeBlockPaths`(CI 工作流、依赖清单、迁移、权限认证、密钥配置等)时一律不自动合并，写决策简报 `data/fixes/<编号>/merge-decision.md` 交用户(必须交用户的关卡 `high-risk-merge`)；主分支有分支保护或规则集要求必需检查时开启 GitHub 原生自动合并，由 GitHub 在检查通过后合并；没有时满足全部条件(另加 PR 无冲突且检查通过)由本工具合并并删除远程修复分支。不满足时输出「未自动合并」与原因，转述给用户即可。
- `release.reviewComment`(缺省开启)：PR 创建后把修复最后一轮 AI 评审的结论以评论写入 PR，只作说明，不是批准。

这两个关卡不改变下文的任何禁止项：冲突解决、放弃合并、删除 worktree 与本地分支(关卡 `delete`)仍需用户确认。部署后确认发现回归时编排会提撤销 PR(`release revert` 的同一操作)，交用户决定是否合并。

## 命令

```
tightrein release <编号>
tightrein release commit <编号> [--accept-findings]
tightrein release sync <编号> [--continue | --abort]
tightrein release push <编号>
tightrein release pr <编号>
tightrein release track
tightrein release summary <编号>
tightrein release revert <编号> --reason <回归现象>
tightrein fix cleanup <编号>
tightrein confirm <操作编号>
tightrein reject <操作编号> [--note <说明>]
```

- `release <编号>`：从当前进度连续推进(提交 → 同步主干 → 推送 → 提 PR)，停在下一个待确认操作。
- `release commit`：修复最后一轮的确定性检查没有全部通过时会列出未通过的项；请用户决定先处理(`fix apply <编号> --review-only` 或 `fix start <编号>`)，还是用 `--accept-findings` 照常提交(接受的项写进 Issue 历史与 PR 描述)。
- `release track`：查看 PR、部署与进入 master 的状态，通常由定时运行执行。部署信息由 `extensions.deploy-source` 选用的方法读取(`core/github-actions`、`core/github-deployments`、`core/vercel`)；没有配置时合并后经过 `release.deploy.observationHours` 视为已部署。
- `release revert`：已合并的修复导致回归时，提一个撤销该合并提交的 PR(新分支、`git revert`、推送、开 PR)，不合并，交用户决定；生成的是待确认操作，Issue 状态不变。

## 出现待确认操作时，每个都要请用户明确同意

展示操作时向用户转述：将执行的完整命令、作用的分支与文件(按文件名排序)、对工作区与历史的影响、是否影响远程、能否撤销以及如何撤销。用户明确同意后才执行 `tightrein confirm <操作编号>`；**同意只对这一个操作有效**，下一个操作重新询问。用户拒绝时执行 `tightrein reject <操作编号> --note <说明>`。

删除 worktree 与本地分支(`fix cleanup`)需要确认两次，第二次要求输入分支名；`git branch -d` 只删除已合并的分支，未合并时 git 会拒绝，本工具不使用 `-D`。

## 不使用的操作

不使用 `rebase`、`push --force`、`reset --hard`、`commit --amend`，也不删除远程分支；同步主干一律用 merge。这些操作只有用户主动提出时才考虑，并由用户自己执行。

## 出问题时

- **合并冲突**：本工具生成冲突报告 `data/fixes/<编号>/conflicts.md`，逐个文件列出两侧的相关提交与冲突片段。两侧对同一段各有实现时是互斥实现，请用户选择保留哪一侧，不要自行取舍、不要修改冲突文件。用户解决后执行 `release sync <编号> --continue`；放弃合并执行 `release sync <编号> --abort`(会丢弃合并中的全部改动)。合并了 main 之后要重新执行合并前验证。
- **推送被拒**：通常是远程有新提交。停下说明原因，执行 `release sync <编号>`，不要强制推送。
- **PR 与 main 冲突**：在本地执行 `release sync <编号>` 合并 main 解决，不在网页上解决冲突。
- **PR 被关闭且未合并**：Issue 以「修复未采纳」取消，关联问题转为已忽略；PR 上的用户说明写入 Issue 历史。关闭理由是「不是缺陷」「无法复现」一类时，建议用户用 `retriage <问题> --verdict` 改判。
- **部署失败**：通知用户并附日志链接，是否由本修复引起由用户判断；需要修复时新开 Issue 走完整流程。
- **需要手动部署的部分**：改动涉及需要手动部署的路径时提示用户联系负责人。

## 分支、提交与 PR 的格式

先按项目约定，没有时用通用格式。优先级：`project.yaml` 的 `git.conventions` > 项目中写明的约定(PR 模板、commitlint、`CONTRIBUTING.md`、`AGENTS.md` 中带占位符的模板，拿不准时不采用) > 通用格式。通用格式：

- 分支名 `<类型>/<Issue 编号>-<简称>`，类型由任务类型映射为 `feature`、`bugfix`、`hotfix`(立即修的 P0)、`chore`；项目要求个人前缀(`git.personalPrefix: true`)时前面加本机用户配置的 `branchPrefix`。
- 提交信息第一行 `<类型>(<范围>): <一句话>`(Conventional Commits，一句话用项目语言)，空一行后写为什么这样改；范围、一句话与原因由写代码的模型给出。
- PR 标题是一句祈使语气的完整话；描述不分固定小节：解决什么问题、为什么这样做、局限，之后是关联(`Closes #<编号>`、父 Issue 与前一个子任务)，最后是程序生成的验证结果。项目有 PR 模板时按模板的标题放进同样的内容。

内容全部取自已有文档，不要改写。

部署后确认通过后，`release` 会起草一段「已在测试环境验证通过」的 PR 回复写入 `data/fixes/<编号>/pr-comment.md`，交用户确认后由用户自己回复到 PR。
