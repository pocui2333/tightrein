# 安全策略

## 支持的版本

tightrein 处于 0.x 阶段，只为最新发布的版本提供安全修复。

## 报告漏洞

**请不要通过公开的 Issue、讨论或 PR 报告安全漏洞。**

请通过 GitHub 的[私密漏洞报告](https://github.com/pocui2333/tightrein/security/advisories/new)提交，并尽量包含：

- 受影响的版本或 commit
- 复现步骤或概念验证
- 影响：攻击者能做到什么、需要什么前提
- 你的环境：macOS 版本、使用的 agent 工具与版本

收到后会尽快确认并评估。确认为漏洞的，修复发布后会在 GitHub Security Advisory 中公开说明；如果你愿意，会在其中致谢。

## 范围

tightrein 的职责是让 AI agent 在你的仓库中执行命令、修改代码。因此下列情形**属于设计行为，不视为漏洞**：

- agent 在 worktree 中运行项目登记的检查命令(工作区 `settings.json` 的 `project.commands`)，或在编码步骤中修改 worktree 里的文件；
- 你登记的检查命令、项目探针或自定义脚本本身有害；
- 你把某个可配置的关卡(`boundaries.gates` 的 `issue`、`design`、`merge`)设为自动后，tightrein 按配置自动执行相应操作；
- agent 工具或模型服务自身的漏洞(请报告给对应的厂商)。

下列情形**属于漏洞**，欢迎报告：

- 绕过下文的任一边界，例如只读步骤写入了文件、agent 进程取得了凭据、未经程序执行了 git 写操作；
- 令牌、密钥等敏感信息出现在日志、交接文件、给人看的文档、Issue、PR 中；
- 被测应用的响应、仓库中的文件等外部内容，能让 tightrein(而不仅是 agent)执行越权操作；
- 写死为人工的关卡(禁改文件被改、高风险路径的合并、超出改动量上限、需要拍板，见 `src/tightrein/protocol/boundaries.md`)可以被配置或输入改为自动执行。

## 安全边界

tightrein 把 agent 的产出视为不可信的，并由自身执行以下边界，不依赖 agent 工具的设置(详见 `src/tightrein/protocol/boundaries.md`、`security.md`)：

| 边界 | 机制 |
|---|---|
| 读写 | 采集、评估与实施中除编码外的小步骤只读：项目代码只开只读 worktree(游离 HEAD、去掉写权限)；编码只能写自己的 worktree；提交、推送、提 PR、合并都由程序执行，agent 不碰。 |
| 命令 | agent 只能运行只读命令白名单(`boundaries.readCommands`)与项目登记的检查命令；子进程以参数数组启动，不经 shell。 |
| 凭据 | 凭据只放在 `settings/secrets.json` 与工作区 `secrets.json`，权限必须是 600；读到即登记进脱敏，永不交给 agent 进程，不写进提示、交接文件与日志；子进程的环境变量按白名单给，名称或取值像凭据的一律去掉，`ANTHROPIC_API_KEY` 总是去掉。 |
| 每轮改动检查 | 编码每轮结束后检查本轮改动：越出 worktree 或改了禁改文件(`.env`、密钥文件等)即撤回本轮全部改动，再越界就停下交人。 |
| 改动量 | 单个 PR 超过 `boundaries.changeCap`(缺省 10 个文件、400 行)时不会被默默接受，交人决定。 |
| 外部内容 | 日志、错误信息、接口响应、仓库文件等外部内容在提示中当数据不当指令。 |
| git 写操作 | 从不 force push、不 rebase 已推送的历史、不 `reset --hard`、不直接写主分支；对外写操作带幂等键，中断后不会做两次；高风险路径的改动合并前必须人工确认。 |

这些措施能降低风险，但不能让无人值守地运行 agent 变得毫无风险。请优先接入测试环境，使用只读令牌，并在放开关卡前充分观察 tightrein 的行为。
