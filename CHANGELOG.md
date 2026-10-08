# 更新日志

本项目的重要变更都记录在此文件中。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

## [未发布]

### 修复

- 评估的取证结论(`assess.triage`，证伪复核共用)去掉「先写」的 `analysis` 字段：Opus 把它判为导出推理(`reasoning_extraction`)，每次都拒答；Sonnet 先写长篇分析耗尽单次输出额度，后面的必填字段被截掉，反复格式不符直到轮数到限。提示词中「推理」「自由书写」的说法一并改掉，知识整理 schema 中「先写」的描述去掉。
- 同一调用点在本次运行中被拒绝过一次后，之后的调用直接走备用模型，不再每次先被拒一遍；started 标记记下 `fallbackFrom`，复盘记为 `call-fallback-<状态>`。
- 新建 worktree 前先 `git worktree prune`：目录被删或挪走、登记还在时，不再因「missing but already registered」停下等人处理。

## [0.2.0] - 2026-10-08

重新设计：按「采集 → 评估 → 实施 → 发布 → 复盘」五个阶段与一个协议层重写，不兼容 0.1.0 的配置、命令与工作区。

### 变更(不兼容)

- 仓库改为 src 布局：代码在 `src/tightrein/`，测试在 `tests/`(结构与包一一对应)，全局取值在 `settings/`，第三方 skills 在 `vendor/`；`core/`、`skills/`、`third_party/` 取消。安装改为在仓库根 `python3.12 -m venv .venv && .venv/bin/pip install -e .`。
- 阶段重新划分：聚合并入采集(去重)，分诊与立项合为评估，修复改称实施(一条流程：准备、定位、方案、定案、编码、自检、审查、交付，按已有信息跳过)，验证中的 PR 前检查并入实施的自检、验收并入发布，学习改为复盘(只记录 tightrein 自身的问题)，知识库独立出来。
- 跨阶段的规则集中到协议层 `src/tightrein/protocol/`：边界与关卡、命名、交接、运行时限、资源(按订阅额度，不按美元)、恢复与控制、安全、记录、调度、git；每一项一份 md 加同名程序。
- 配置改为 `settings/defaults.json` → `settings/controls.json`(本机) → 工作区 `settings.json`，控制键为「阶段.模块.小步骤」逐层继承；原 `~/.config/tightrein/config.yaml`、`project.yaml`、`models` 与 `routes` 两张表取消。
- 接入改为 `project add` → 编辑工作区的 `setup.json`(每个模块启用、不启用还是自定义的唯一来源)、`settings.json`、`sites.json`、`secrets.json` → `project check` 试跑 → `project ready`；凭据从钥匙串改为工作区的 `secrets.json`(权限 600)。
- 命令重新划分为 13 个日常命令(`status`、`watch`、`show`、`approve`、`reject`、`new`、`run`、`pause`、`stop`、`resume`、`take`、`give`)加 5 个分组(`project`、`problem`、`retro`、`knowledge`、`admin`)；`continue` 并入 `run`，`find` 并入 `show --search`，单步命令改用 `run <阶段>`，`issue` 分组取消。敲老写法时报用法错误并给出新写法。
- 文档只留四类：教程、操作指南、参考(由程序生成)、总体架构；各模块的设计依据写在模块自己的 `README.md`。

### 移除

- 复现测试先行(修复前失败、修复后通过)、子任务拆分与三条修复通道、交互修复会话、评测与扩展子系统、本机通知。

## 0.2.0 之前的改动(在 0.1.0 的结构上，已并入重新设计)

### 修复

- 计划拆分时，留给后续子任务的验收标准写进 `deferredAcceptance`：本次的写代码与评审(含深度盲审)把它们单列为「本次不做」，不再因此记阻断项。此前深度评审拿整个 Issue 的验收标准审第一个子任务，把留给后续的部分判为遗漏。
- 评审打回或用户拒绝后重出计划时复用此前的勘察结论(`data/fixes/<编号>/scouting.json`)，不再从勘察重来。
- 工具因临时的接口或网络错误失败(例如 agy 的 `API error … EOF`)时就地重试同一次调用(`runtime.runner.transientRetries`，缺省 2 次)，不计入格式重试。
- 无人值守调用 agy 时不会弹出浏览器登录页：登录凭据的读取被中断时 agy 会转入浏览器登录，现在 PATH 最前面的 `open` 什么都不做。

### 变更

- 修复环节新增「分层代码摘要」：勘察通过后由程序按勘察的位置生成一次(核心位置截取原文，相关位置只取定义行，另列涉及的文件)，存为 `data/fixes/<编号>/brief.json`；出计划、写复现测试、写代码与轻量评审都以它为准，提示要求只打开要改或要核对的行段，不再各自通读代码。
- 写代码只拿计划中的步骤、修改位置与约束，轻量评审只拿计划摘要、代码摘要与 diff，不再传整份计划；材料用 `<plan>`、`<diff>`、`<code_brief>` 包住。勘察、取证、证伪复核、增量审查与变体扫描的角色说明都加上「先搜索、后阅读」与停止条件；通用的评分条目改称「交付检查项」，不再与 Issue 的验收标准重名。

### 修复

- 关卡 merge 为 auto 时，`continue` 遇到待合并的 PR 当场跟踪并按条件自动合并，不再只靠定时运行(工作区暂停时 PR 一直停在待合并)。
- `status` 在关卡 fix-session 为 auto 时推荐 `tightrein continue <编号>`，不再推荐交互会话；`new` 之后的下一步同样如此。
- `tightrein reject <计划的操作编号> -m <要求>` 按要求重出修复计划(原来只记为拒绝)；待确认计划的提示改为这种写法。
- agy 的提示要求每次只执行一条命令(用 `&&`、`||` 串起的命令中只要有一条不在白名单里，整条会被拒绝并结束本轮)；输出格式说明写明格式由调用方提供，不必在项目里查找。

### 变更(不兼容)

- 命令行重新规整：日常命令在顶层(`status` `watch` `show` `find` `new` `continue` `approve` `reject` `run` `pause` `resume`)，其余收进 `issue`、`problem`、`project`、`admin` 四组，流水线单步命令不变。不带命令时等同 `status`；`-w` 可写项目名；帮助只列人会用到的参数。老写法(`next`、`pending`、`confirm`、`issue approve`、`issue create --manual`、`retriage`、`workspace`、`install` 等)已删除，敲老写法时提示新写法。全部命令见 `docs/reference/cli.md`。
- `tightrein admin install` 在 `~/.local/bin` 建立 `tightrein` 命令链接。
- 选模型改为两张表：`models`(模型别名：工具、模型、推理强度与价格)与 `routes`(调用点 → 别名，没写的用 `default`)，写在本机用户配置的顶层，`project.yaml` 可按项覆盖。调用点是固定的清单(`fix.planner`、`fix.review.deep`、`triage.refuter` 等，见 `docs/reference/configuration.md`)，`fix.planner`、`fix.scout`、`fix.executor` 可按 `high-risk`、`frontend`、`large` 条件另走路由(例如 `fix.planner.high-risk: fable`)。`tightrein project config --routes` 列出每个调用点实际用的模型与来源。原来的 `agents` 段、`capabilities`、`roleCapabilities`、`defaultTool`、`evaluation.judge` 以及 `stages` 中的 `tool`、`model`、`capability`、`refuter`、`session` 已删除，配置中还有时加载即报出该键与新写法；核心不再给缺省档位，须自己写 `models` 与 `routes.default`。`stages` 中的上限与 `paths` 不变。命令行的 `--runner` 现在对所有调用点生效。
- improvement-writer 的 model 类建议改为给出模型别名(`model`)，不再给能力档。

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

[未发布]: https://github.com/pocui2333/tightrein/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/pocui2333/tightrein/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/pocui2333/tightrein/releases/tag/v0.1.0
