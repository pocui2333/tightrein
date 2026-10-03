# tightrein

[![test](https://github.com/pocui2333/tightrein/actions/workflows/test.yml/badge.svg)](https://github.com/pocui2333/tightrein/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)

简体中文 | [English](README.en.md)

**自动发现并修复代码缺陷，缰绳始终在你手中。**

tightrein 持续运行一条流水线：发现应用中的缺陷，基于证据分诊，交由 AI 编码 agent([Claude Code](https://docs.claude.com/en/docs/claude-code)、[Codex CLI](https://github.com/openai/codex)、Antigravity CLI)修复，验证后提交 PR。每一步都在你设定的边界内运行：agent 能执行哪些命令、能改多少代码、能花多少钱、哪些决定必须由你做出。

> [!WARNING]
> tightrein 处于 alpha 阶段，会让 AI agent 操作你的代码与环境。请先接入测试环境，使用只读的平台令牌，并保留缺省的审批关卡。

## 特性

- **端到端流水线**：信号来自 API 模糊测试([Schemathesis](https://schemathesis.io/))、静态审查([Semgrep](https://semgrep.dev/) 与模型审查)、错误追踪(Sentry)、日志(Loki)、告警(Alertmanager)与自定义的项目探针；先去重，再逐条取证，确认成立后才建 Issue。
- **复现优先**：修复缺陷前先写一个在当前代码上失败的测试，修复后它必须通过。
- **人工审批关卡**：缺省由你放行 Issue、确认修复计划、推送与合并；每个关卡可单独改为自动，高风险改动始终需要人工确认。
- **强制执行边界**：命令白名单、agent 环境中不含凭证、只读 worktree；每次运行前后比对 git 状态与文件快照。
- **小而可审的 PR**：缺省每个 PR 最多 5 个文件、200 行(不含测试)，更大的工作拆分为子 Issue。
- **预算与熔断**：按次、天、周设定花费上限；反复失败或没有进展时交回人工处理。
- **自由组合 agent**：可以按角色指定工具与模型；评审者必须与被评审者使用不同的模型。
- **从结果中学习**：修复与误报中得到的经验会反馈到分诊与修复环节。

## 工作原理

| 环节 | 内容 |
|---|---|
| **collect** 采集 | 运行已配置的来源，记录原始信号。 |
| **aggregate** 聚合 | 归一化信号，并去重合并为问题。 |
| **triage** 分诊 | 取证判断问题是否成立，给出严重度、任务类型、规模与处理方式：立即修、排期修、观察或不修。 |
| **issue** | 在本地写 Markdown 格式的 Issue，可选同步到 GitHub。 |
| **fix** 修复 | 制定计划，写复现测试，实施改动，运行检查并评审。小而低风险的改动走快速通道，大的拆分处理。 |
| **verify** 验证 | 合并前与部署后重新运行复现检查。 |
| **release** 发布 | 建分支、提交、开 PR，跟踪合并与部署；发现回归时提出撤销。 |
| **learn** 学习 | 统计指标，记录经验，提出规则与配置的改进建议。 |

可以手动运行、定时运行(launchd)，也可以由新提交、新部署等事件触发。等你决定的事项集中在一个收件箱：`tightrein status`。

## 运行要求

- Python 3.12+、git、已登录的 [GitHub CLI](https://cli.github.com/)
- 至少一个 agent 工具：Claude Code、Codex CLI 或 Antigravity CLI

以下功能目前需要 macOS：

| 功能 | 依赖 | 不使用时 |
|---|---|---|
| 平台令牌(Sentry、Loki、Vercel 等的只读令牌) | 钥匙串 | 不接入这些平台 |
| 定时运行 `tightrein schedule install` | launchd | 手动运行或用其他调度器调用 `tightrein tick` |
| 桌面通知 | `osascript` | 在 `~/.config/tightrein/config.yaml` 中设置 `notify: {method: none}` |

其余功能不依赖 macOS，但目前只在 macOS 上测试过。不支持原生 Windows。

## 安装

```sh
git clone https://github.com/pocui2333/tightrein.git
cd tightrein
python3 -m venv core/.venv
core/.venv/bin/pip install -e core
core/.venv/bin/tightrein install    # 把 skills 装进本机已有的 agent 工具
```

把 `core/.venv/bin` 加入 `PATH`，或直接调用 `core/.venv/bin/tightrein`。

## 快速上手

**1. 配置模型。** 新建 `~/.config/tightrein/config.yaml`：

```yaml
agents:
  defaultTool: claude
  stages:
    triage:
      refuter: {capability: standard}   # 评审者须与被评审者使用不同的模型
    fix:
      review:
        deep: {capability: standard}
  capabilities:
    light:    {claude: {model: haiku, inputUsdPerMTok: 1, outputUsdPerMTok: 5}}
    standard: {claude: {model: sonnet, inputUsdPerMTok: 3, outputUsdPerMTok: 15}}
    strong:   {claude: {model: opus, effort: high, inputUsdPerMTok: 5, outputUsdPerMTok: 25}}
```

**2. 接入项目。** 工作区位于 `workspaces/`(不纳入版本库)。接入完成前，tightrein 只执行只读操作。

```sh
tightrein workspace init --workspace workspaces/my-app --repo ~/code/my-app
tightrein worktree init --workspace workspaces/my-app     # 申请建只读 worktree，按提示 tightrein confirm 确认
tightrein status --workspace workspaces/my-app           # 待回答的接入项、推荐答案与对应命令
```

**3. 运行。** 第一次巡检手动触发；之后可以交给定时任务。

```sh
tightrein run --workspace workspaces/my-app --select collect+ --probe static --dry-run   # 只列出将要执行的步骤
tightrein run --workspace workspaces/my-app --select collect+ --probe static
tightrein watch                                         # 在另一个终端实时查看
tightrein schedule install                              # 可选：工作日定时运行
```

完整的演练见[教程：第一次运行](docs/tutorials/first-run.md)。

## 文档

- [教程：第一次运行](docs/tutorials/first-run.md)
- [配置与运行说明](docs/reference/configuration.md)：采集来源、审批关卡、预算、GitHub 镜像、修复通道、agent 工具与模型
- [参考](docs/reference/README.md)：采集方法、交接文档、项目探针
- [操作指南](docs/how-to/README.md)
- [设计](docs/explanation/README.md)

## 参与贡献

见 [CONTRIBUTING.md](CONTRIBUTING.md)。报告安全漏洞见 [SECURITY.md](SECURITY.md)。

## 许可证

[MIT](LICENSE)
