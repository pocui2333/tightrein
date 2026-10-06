# 更新日志

本项目的重要变更都记录在此文件中。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

## [未发布]

### 变更

- agy 无人值守时可以搜索代码：`tightrein install` 在 agy 的命令白名单中补上 `git grep` 等只读命令，提示末尾列出已放行的命令与工具调用上限，要求先搜索再读文件。此前提示让 agy 不要调用 shell，它只能逐个打开文件，勘察又慢又费 token。
- 勘察的位置必须是当前代码中已有的文件与行，没有设计问题时 `designIssue` 写 `null`。
- 用户亲自提出的需求视为已同意按设计层面修复，勘察标出设计问题时不再停下等决定。

### 修复

- `tightrein watch` 在 `continue` 推进时不再显示空白：流程脉络与步骤明细改为进行中的 Issue 的修复步骤、第几次调用、已运行时长与上一次失败的原因(超时、格式不符等)。

- 合并 `origin/main` 后不再一律重新评审：改动哈希以合并进来的 main 版本为基准，main 没改到修复的文件时只重新验证。
- 改动哈希(`diffHash`)与改动是否已提交无关：修复新增的文件提交后不再被当成新的改动。此前任何带新文件(包括复现测试)的修复在提交并合并主干后都会被要求重新评审。
- 同步主干时「与本修复改动文件的交集」从分叉点计算，不再把修复自己的文件算进去。
- 无人值守的 `continue` 遇到需要重新评审时自行执行 `fix apply --review-only`；待决定的 Issue 提示 `fix start --force`。

## [0.1.0] - 2026-10-04

首个公开版本。

### 新增

- 端到端流水线：采集、聚合、分诊、Issue、修复、验证、发布与学习。
- 采集来源：API 模糊测试(Schemathesis)、静态审查(Semgrep 与模型审查)、错误追踪(Sentry)、日志(Loki)、告警(Alertmanager)与项目探针。
- 支持 Claude Code、Codex CLI 与 Antigravity CLI，可按环节与角色指定工具和模型。
- 审批关卡、命令白名单、凭证隔离、只读锁定、运行前后的边界检查与改动量上限。
- 预算(按次、天、周)与熔断。
- 本地 Markdown Issue，可选同步到 GitHub。
- 定时与事件触发的运行(launchd)、收件箱 `tightrein status` 与实时界面 `tightrein watch`。

[未发布]: https://github.com/pocui2333/tightrein/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/pocui2333/tightrein/releases/tag/v0.1.0
