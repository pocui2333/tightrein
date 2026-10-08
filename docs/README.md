# tightrein 文档

文档只分四类，按你的目的选：

| 类别 | 什么时候读 | 文件 |
|---|---|---|
| 教程 | 第一次用，想跟着做一遍 | [第一次运行](tutorials/first-run.md) |
| 操作指南 | 要完成一件具体的事 | [接入项目](how-to/onboard-project.md)、[写项目探针](how-to/write-project-probe.md)、[加一种方法](how-to/add-method.md) |
| 参考 | 查命令、配置字段、交接格式、平台方法 | [命令](reference/commands.md)、[配置字段](reference/configuration.md)、[交接格式](reference/handoff.md)、[平台方法](reference/methods.md) |
| 说明 | 想知道整体怎么组成、为什么这样设计 | [总体架构](explanation/overview.md) |

参考文档由程序从命令树、`settings/defaults.json`、各步骤的 schema 与方法清单生成，不手写：

```sh
.venv/bin/python -m tightrein.cli.reference
```

## 设计依据在哪里

每个模块的设计依据写在它自己文件夹的 `README.md` 里，改代码时就在旁边。每份 README 用同一个模板：

| 小节 | 写什么 |
|---|---|
| 是什么 | 一两句 |
| 流程 | 小步骤与顺序 |
| 输入与输出 | 交接中的必填事实 |
| 配置 | 有哪些控制字段，取值在 settings 的哪里 |
| 设计依据 | 每条：怎么做、为什么、出处 |
| 不做什么 | 砍掉的东西与原因 |

从这里开始找：

| 想了解 | 看 |
|---|---|
| 全局规则(边界、交接、时限、资源、恢复、安全、记录、调度、git) | `src/tightrein/protocol/README.md` 与同目录各 md |
| 配置文件放什么、怎么覆盖 | `settings/README.md` |
| 五个阶段 | `src/tightrein/collect/`、`assess/`、`implement/`、`release/`、`retro/` 下的 README |
| 知识库、调用 AI、提示词、存储 | `src/tightrein/knowledge/`、`agents/`、`prompts/`、`store/` 下的 README |
| 接入项目、命令行 | `src/tightrein/onboard/README.md`、`src/tightrein/cli/README.md` |
| 引用的第三方 skills | `vendor/README.md` |
| 开发环境、测试结构、实现原则 | `CONTRIBUTING.md` |
