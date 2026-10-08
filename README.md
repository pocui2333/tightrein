# tightrein

[![test](https://img.shields.io/github/actions/workflow/status/pocui2333/tightrein/test.yml?branch=main&label=test)](https://github.com/pocui2333/tightrein/actions/workflows/test.yml) [![license](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE) [![python](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/)

简体中文 | [English](README.en.md)

**自动发现并受控修复代码缺陷的流水线。**

tightrein 无人值守地跑一条闭环：从项目的运行数据与代码里找线索，取证判断是不是问题，要修的写成 Issue，交给 AI 编码 agent([Claude Code](https://docs.claude.com/en/docs/claude-code)、Antigravity CLI、[Codex CLI](https://github.com/openai/codex))在隔离的 worktree 里改，自检与审查通过后提 PR、合并，上线后在最初报出问题的地方确认。每一步都在严格的边界内：agent 能读写什么、能跑哪些命令、最多改多少、用多少额度、哪些事必须人来定。

> [!WARNING]
> tightrein 处于 alpha 阶段，会让 AI agent 操作代码与环境。建议先接入测试环境，使用只读凭据，并保留默认的人工关卡。

## 特性

- **多来源、先取证**：线索来自项目探针、错误追踪(Sentry)、日志(Loki)、访问日志、告警(Alertmanager)、API 模糊测试([Schemathesis](https://schemathesis.io/))、静态巡检([Semgrep](https://semgrep.dev/) 加模型审查)与任务中顺带的发现；先去重成问题，取证判定成立才写成 Issue，高风险的再由另一个模型盲审复核。
- **一条实施流程，按已有信息跳过**：准备 → 定位 → 方案 → 定案 → 编码 → 自检 → 审查 → 交付；评估留下的代码笔记各步共用，不再各自通读代码。
- **强制执行的边界**：只读命令白名单、子进程环境变量白名单(凭据永不交给 agent)、只读 worktree、禁改与高风险两级受保护文件、单个 PR 的改动量上限(缺省 10 个文件、400 行)。
- **人工关卡**：Issue 放行、方案定案、合并都可以要求人工确认；低风险的可配置为自动，但禁改文件被改、高风险路径的合并、超出改动量上限、需要拍板这几种写死为人工。
- **按订阅额度控制用量**：给用户自己留余量(5 小时窗口与每周额度)，每个 Issue 有 token 上限，没有进展或交回修改超过 3 轮即停，依赖与对象各有熔断。
- **审查与被审的模型不同**：证伪复核与深度审查要求与被审的一方用不同的模型，避免同一个盲点。
- **能续跑、可追溯**：每一步落盘一份交接(结论、必填事实、量化数据、备注)，也是检查点；中断后从检查点接着做，对外写操作带幂等键。
- **复盘与知识库**：每次运行结束检查 tightrein 自己的失败、浪费、误判与打扰，记成带评级的记录；项目的约定、缺陷模式与经验沉淀进知识库，评估与实施按位置取用。

## 工作原理

五个阶段：

| 阶段 | 命令中的名字 | 做什么 |
|---|---|---|
| 采集 | `collect` | 从七个来源收集线索(信号)，最后一步去重，整理成问题(新发现或回归) |
| 评估 | `assess` | 判断问题是否存在，定严重度(P0 到 P3)与处理方式；要修的写成 Issue 并等放行 |
| 实施 | `implement` | 在 worktree 里改代码，自检、审查，交给发布 |
| 发布 | `release` | 提交、同步主干、提 PR、等 CI、合并、跟踪部署、验收、清理 |
| 复盘 | `retro` | 检查这一轮 tightrein 自己的问题，记进记录簿 |

可以手动运行(`tightrein run`)，也可以由定时器(macOS 的 launchd)在允许的时段里自动推进到配置的那一步。需要人处理的都汇总在 `tightrein status`。整体结构见 [总体架构](docs/explanation/overview.md)。

## 运行要求

- macOS(定时器用 launchd；其他系统可以手动运行)、Python 3.12+、git；
- 已登录的 [GitHub CLI (`gh`)](https://cli.github.com/)；
- agent 工具：缺省配置用 Claude Code 与 Antigravity CLI，都按订阅登录；只用 Claude 时改几个调用点的模型即可(见教程)。

## 安装

```sh
git clone https://github.com/pocui2333/tightrein.git
cd tightrein
python3.12 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/tightrein admin install    # 检查工具与第三方 skills，给 agy 补只读命令白名单，在 ~/.local/bin 建 tightrein 命令
```

`~/.local/bin` 在 `PATH` 中时，之后在任何目录直接敲 `tightrein`。

## 快速开始

跟着 [教程：第一次运行](docs/tutorials/first-run.md) 走一遍：

```sh
tightrein project add ~/code/my-app      # 建工作区并探测仓库
# 编辑 workspaces/my-app/ 下的 setup.json、settings.json，填 sites.json、secrets.json
tightrein project check                  # 试跑：只取数据，不调用模型
tightrein project ready                  # 标为就绪并装上定时器
tightrein run                            # 手动推进一轮
tightrein status                         # 一次性快照；tightrein watch 实时看
```

## 命令

日常 13 个：

| 命令 | 做什么 |
|---|---|
| `tightrein` / `tightrein status [--json]` | 一次性快照；不带命令时默认执行 |
| `tightrein watch [--collect]` | 实时界面 |
| `tightrein show <编号> [--steps] [--doc pending/failure/deliver]` | 一个问题或 Issue 的详情；`show --search "<关键词>"` 按描述查找 |
| `tightrein approve <编号> [--option <序号>] [--note "<补充>"]` | 通过待审核的事项 |
| `tightrein reject <编号> --note "<原因>"` | 不通过，方案按原因重出 |
| `tightrein new "<需求描述>" [--severity P0..P3] [--type bug/feature]` | 自己提需求，直接进评估 |
| `tightrein run [collect/assess/implement/release/retro] [--object <编号>] [--dry-run]` | 手动触发：不带阶段按状态推进一轮 |
| `tightrein pause` / `stop` / `resume` | 当前一步做完后停下 / 急停 / 恢复 |
| `tightrein take <编号>` / `give <编号>` | 手动接管 / 交还 |

分组 5 个：

| 分组 | 子命令 |
|---|---|
| `project` | `add`、`check`、`ready`、`show`、`config`、`list`、`remove`：接入项目 |
| `problem` | `list`、`mute`、`unmute`、`reopen`：查看与处置问题 |
| `retro` | `list`、`show`、`close`：tightrein 自身问题的记录簿 |
| `knowledge` | `list`、`show`、`confirm`、`drop`、`add`：项目的知识库 |
| `admin` | `install`、`uninstall`、`rebuild`、`check`、`clean`：本机的安装、自检与维护 |

全局选项写在命令之后：`-p <项目>`、`--json`、`--lang zh/en`、`--yes`。全部参数见 [命令参考](docs/reference/commands.md)。

## 仓库布局

```
src/tightrein/   代码(src 布局)：collect、assess、implement、release、retro、knowledge、
                 onboard、agents、prompts、protocol、store、settings、cli
settings/        全局取值：defaults.json(提交)，controls.json、sites.json、secrets.json(本机)
vendor/          锁定版本的第三方 skills
tests/           测试，结构与 src/tightrein/ 一一对应
docs/            文档
workspaces/      各项目的工作区(不进 git)
```

每个模块文件夹都有一份 `README.md`：是什么、流程、输入与输出、配置、设计依据、不做什么。

## 文档

- [教程：第一次运行](docs/tutorials/first-run.md)
- 操作指南：[接入项目](docs/how-to/onboard-project.md)、[写项目探针](docs/how-to/write-project-probe.md)、[加一种方法](docs/how-to/add-method.md)
- 参考：[命令](docs/reference/commands.md)、[配置字段](docs/reference/configuration.md)、[交接格式](docs/reference/handoff.md)、[平台方法](docs/reference/methods.md)
- [总体架构](docs/explanation/overview.md)
- 参与开发：[CONTRIBUTING.md](CONTRIBUTING.md)
