# tightrein

[![test](https://img.shields.io/github/actions/workflow/status/pocui2333/tightrein/test.yml?branch=main&label=test)](https://github.com/pocui2333/tightrein/actions/workflows/test.yml) [![license](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE) [![python](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/)

简体中文 | [English](README.en.md)

**自动发现并受控修复代码缺陷的工程流水线。**

tightrein 持续运行一条闭环流水线：感知应用缺陷，基于证据分诊，交由 AI 编码 agent（[Claude Code](https://docs.claude.com/en/docs/claude-code)、[Codex CLI](https://github.com/openai/codex)、Antigravity CLI）在独立 worktree 中修复，验证通过后提交 PR。每一步都在严格设定的边界内运行：agent 能执行哪些命令、最多修改多少代码、消耗多少预算、哪些高风险操作必须由人工确认。

> [!WARNING]
> tightrein 处于 alpha 阶段，会让 AI agent 操作代码与环境。建议先接入测试环境，使用只读凭据，并保留默认的人工审批关卡。

## 特性

- **多源信号与证据分诊**：信号来自 API 模糊测试（[Schemathesis](https://schemathesis.io/)）、静态审查（[Semgrep](https://semgrep.dev/) 与模型审查）、错误追踪（Sentry）、日志（Loki）、告警（Alertmanager）与自定义项目探针；先去重合并，再由独立模型取证对驳，确认成立后才建 Issue。
- **复现测试先行**：修复缺陷前必须先生成在当前代码上失败的测试（Red），修复完成后该测试必须通过（Green）。
- **强制执行边界**：命令白名单、执行环境中剥离凭据、独立只读 worktree；每次运行前后比对 git 状态与文件快照。
- **微型可审 PR**：单次 PR 默认限制最多 5 个文件、200 行改动（不含测试），杜绝借修 Bug 之名的扩散性重构；大任务自动拆分。
- **人工决策关卡**：放行 Issue、确认修复计划、高风险合并均需人工确认；每个关卡可单独配置为自动，但高风险路径始终人工把关。
- **预算与熔断**：按次、按天、按周设定 Token 与费用上限；反复失败或停滞时自动熔断并交回人工处理。
- **异构模型评审**：支持按阶段指定工具与模型能力；代码评审方与修复实施方强制使用不同模型，避免自我验证假阳性。
- **从结果中学习**：修复与误报中沉淀的经验自动反馈为规则与技能，持续提升后续分诊与修复质量。

## 工作原理

流水线划分为 8 个严格受控的阶段：

| 环节 | 代号 | 说明 |
|---|---|---|
| 采集 | `collect` | 运行配置好的数据源与探针，捕获原始异常信号。 |
| 聚合 | `aggregate` | 归一化信号，基于指纹与堆栈去重合并为待分诊问题。 |
| 分诊 | `triage` | 取证判定问题是否成立，给出严重度、修复策略（立即修、排期修、观察或不修）。 |
| 立项 | `issue` | 在本地生成结构化 Markdown 任务，可选同步到 GitHub Issues。 |
| 修复 | `fix` | 在独立隔离的 worktree 中制定计划、编写复现测试并实施极简修复。 |
| 验证 | `verify` | 重新运行复现测试套件，执行代码规范与非测试代码 Diff 膨胀率检查。 |
| 发布 | `release` | 自动建分支、提交、开 PR，跟踪部署并在检测到回归时提出撤销。 |
| 学习 | `learn` | 统计指标，沉淀避坑经验与解题模式，输出规则改进建议。 |

支持手动触发、定时运行（launchd）或由代码提交与部署事件触发。等待人工审批的事项集中呈现于待办决策列表：`tightrein status`。

## 运行要求

- Python 3.12+、git、已认证登录的 [GitHub CLI (`gh`)](https://cli.github.com/)
- 至少配置一个 agent 工具：Claude Code、Codex CLI 或 Antigravity CLI

平台特性支持：

| 功能 | macOS | Linux / Docker |
|---|---|---|
| 平台令牌安全存储 (Sentry、Loki 等) | 系统钥匙串 (Keychain) | 环境变量配置 |
| 定时巡检 (`tightrein project schedule install`) | launchd | cron / systemd |
| 桌面通知 | 原生 osascript 通知 | 关闭通知 (`notify: {method: none}`) |

暂不支持原生 Windows，建议在 WSL2 环境下运行。

## 安装

```sh
git clone https://github.com/pocui2333/tightrein.git
cd tightrein
python3 -m venv core/.venv
core/.venv/bin/pip install -e core
core/.venv/bin/tightrein admin install    # 注册 skills，并在 ~/.local/bin 建立 tightrein 命令
```

`~/.local/bin` 在 `PATH` 中时，之后在任何目录直接敲 `tightrein` 即可；否则把 `core/.venv/bin` 加入 `PATH`。`tightrein --help` 列出全部命令，见[命令参考](docs/reference/cli.md)。

## 快速上手

**1. 配置模型。** 新建 `~/.config/tightrein/config.yaml`：

```yaml
models:                       # 模型别名：工具、模型、推理强度与价格
  opus:   {tool: claude, model: opus, effort: high, inputUsdPerMTok: 5, outputUsdPerMTok: 25}
  sonnet: {tool: claude, model: sonnet, inputUsdPerMTok: 3, outputUsdPerMTok: 15}
routes:                       # 调用点 → 别名；没写的调用点用 default
  default: opus
  triage.refuter: sonnet      # 评审者须与被评审者使用不同的模型
  fix.review.deep: sonnet
```

`tightrein project config --routes` 列出每个调用点实际用的模型，调用点与条件见[配置说明](docs/reference/configuration.md)。

**2. 接入项目。** 工作区位于 `workspaces/`（不纳入版本库）。接入完成前，tightrein 只执行只读检查。

```sh
tightrein project init --workspace workspaces/my-app --repo ~/code/my-app
tightrein project worktree init --workspace workspaces/my-app     # 申请建只读 worktree，按提示 tightrein approve 确认
tightrein status --workspace workspaces/my-app           # 查看接入项与推荐命令
```

**3. 运行。** 首次巡检手动触发，后续可由定时任务或事件调度接管。

```sh
tightrein run --workspace workspaces/my-app --select collect+ --probe static --dry-run   # 试运行：列出将要执行的步骤
tightrein run --workspace workspaces/my-app --select collect+ --probe static             # 正式执行采集与巡检
tightrein watch                                                                         # 在另一个终端实时监控
tightrein project schedule install                                                              # 可选：macOS 工作日定时运行
```

完整接入演练见[教程：第一次运行](docs/tutorials/first-run.md)。

## 文档

- [教程：第一次运行](docs/tutorials/first-run.md)
- [配置与运行说明](docs/reference/configuration.md)：采集来源、审批关卡、预算、GitHub 镜像、修复通道、agent 工具与模型
- [参考](docs/reference/README.md)：采集方法、交接文档、项目探针
- [操作指南](docs/how-to/README.md)
- [设计与架构说明](docs/explanation/README.md)
