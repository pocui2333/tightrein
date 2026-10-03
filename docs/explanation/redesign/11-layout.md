# 目录划分

目录调整不单独进行，随对应模块的新设计一起落地，避免先搬家再改代码造成两次冲突。

| 位置 | 调整 | 随哪项改动 |
|---|---|---|
| `docs/` | 按 00-documentation.md 重组为 `tutorials/`、`how-to/`、`reference/`、`explanation/`；新设计落地后把 redesign 合并进 explanation，删除被取代的 architecture、design 旧内容 | 交接文档与文档体系 |
| `core/tightrein/pipeline/` 根目录下的散落文件 | 按职责归位：自主决定与流程表到 `orchestrator/policy/`；调用角色的公共代码到 `runner/`；项目检查到 `pipeline/checks/`，供修复与验证共用；其余到 `pipeline/common/` | 修复通道与分诊 |
| `core/tightrein/sources/` | 改名为 `sources/`，按采集方法分：平台错误、访问日志、业务告警、项目探针、`api_fuzz/`、`static/`、`incidental/`；删除 `e2e/`；复现检查的执行器 `regressions/` 移到 `pipeline/checks/` | 采集的新来源 |
| `core/tightrein/contracts/schemas/` | 按用途分：`handoff/`(各类交接文档)、`config/`(项目与用户配置)、`extension/`(扩展点契约)、`runner/`(调用模型的任务与结果)、`data/`(信号、问题、回归等) | 交接文档与文档体系 |
| 根目录 `tools/` 与 `core/tools/` | 根目录的改名为 `local/`，统一存放本机依赖(含第三方 skill 的缓存，从 `third_party/cache/` 移入)，不进版本库；`core/tools/` 改名为 `core/dev/`，存放开发用的脚本 | 交接文档与文档体系 |
| `skills/*/references/roles/` | 保持：skill 与模型使用同一份角色说明，不分两套 | — |
