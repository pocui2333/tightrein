# 11. 边界

## 11.1 原则

- **最小权限**：每个 agent 只拿到完成任务所需的工具。
- **读写分离**：采集、分诊、评审类 agent 只读；写操作由确定性脚本执行，agent 只提交变更请求(10.2)。
- **写操作有上限**：每类写操作都有次数上限。
- **上限与规则写在配置中**：轮数、预算、改动量上限、受保护文件、风险判定规则、各调用点用哪个模型(模型别名与路由表)都是配置项，按 architecture/01 5.1 的四层配置合成，不写死在代码中。
- **失败即停**：触及轮数或预算上限、输出不符合 schema、检查不通过时一律停下，写明卡点并转人工，不自动降级继续。
- **工具的权限规则不是全部**：以 Claude Code 为例，其 Bash 权限规则只匹配命令文本，本身不构成安全边界。关键限制由核心在工具之外再检查一遍(9.5)。

## 11.2 各 skill 的边界

| skill | agent 可用工具 | 可写范围 | 轮数与预算 | 停止条件 |
|---|---|---|---|---|
| `collect`(静态巡检部分) | Read、Grep、Glob、只读 git 命令 | 无，结果以 JSON 返回 | 按 `project.yaml` 设定 | 超出上限、输出不合 schema |
| `triage` | Read、Grep、Glob、只读 git 命令 | 无，结果以 JSON 返回 | 每个问题单独设定 | 同上；证据不合格重做 2 次后仍不合格 |
| `fix` | 修复会话：Read、Grep、Glob，Bash 只允许 `tightrein fix` 的子命令与只读 git 命令；`fix-scout`、`fix-planner`、`fix-reviewer`：Read、Grep、Glob 与只读 git 命令；`fix-executor`、`repro-writer`：另加 Edit、Write，Bash 只允许项目检查命令 | 会话与只读角色不可写；`fix-executor` 只限该 Issue 的修复 worktree | 按复杂度(13.3)设定；评审按轻量与深度分别设定 | 同上；改动量超过当前档或单个 PR 上限(5.5)；触及受保护文件；修改超过 `thresholds.fix.reviewRounds` 轮 |
| `learn`(`lesson-writer`、`rule-writer`、`improvement-writer`) | Read、Grep、Glob | 无，结果以 JSON 返回；经验、规则与改进建议由程序校验后写入 | 按 `stages.learn.tasks` 设定 | 同上 |
| 其余(`aggregate`、`issue`、`verify`、`release`) | 以脚本为主；LLM 只负责写摘要 | 各自的数据目录 | — | 脚本返回错误 |

**所有 agent 共同禁止**

- git 写操作：只由核心的 `vcs` 在用户确认后执行，建分支与 worktree 由 `fix` 发起(5.3)，其余由 `release` 发起(第 7 章)；改进建议由用户批准后自己应用(14.5)，本工具没有合入它的路径。
- 读取凭证与连接配置：`project.yaml` 的 `credentialFiles` 所列的文件，用权限规则 `deny` 加 hook 双重拦截。示例项目中的取值：`.env`、`appsettings*.json`、密钥文件。
- 修改评测集、验证脚本、复现检查与本插件的配置。

**受保护文件**：修复时改动 `project.yaml` 的 `protectedPaths` 所列的文件，或增删 `protectedPatterns` 所列的内容，必须在确认修复计划时单独向用户说明，未确认时停止。示例项目中的取值：

- `Migrations/MigrationList.cs`(数据库结构)
- `PermissionMatrix.cs` 与各处 `[Authorize]`、`[AllowAnonymous]`(权限)
- `deploy/`、`.github/`(部署)
- 语言文件以外的配置文件

## 11.3 实现方式

- **第一层，工具自身的限制**：适配器把 11.2 的边界翻译成所用工具的机制，例如 Claude Code 的工具白名单、只读权限模式与 PreToolUse hook，Codex CLI 的只读或 workspace-write 沙箱与审批策略，Antigravity CLI 的权限模式与沙箱(无人值守时需要确认的写入与命令一律拒绝)。
- **第二层，核心的检查**：不论使用哪个工具都生效，见 9.5：只读 worktree 设为不可写、运行结束后检查改动范围与受保护文件、比较运行前后的 git 状态、agent 进程中不含凭证、超时终止。
- **预算**：每次调用记录费用与 token(工具不提供费用时按 token 估算)，归到所属的环节与角色，供 14.2 的环节效益统计；核心累计每个环节当天的花费，超过配置的上限后，当天不再启动该环节，并在运行摘要中说明。
