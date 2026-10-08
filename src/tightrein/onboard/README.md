# onboard：接入项目

## 是什么

把一个项目接进 tightrein：建工作区、探测仓库、让用户确认每个模块启用与否、试跑、标为就绪。项目专属的一切(选了什么、取值、地址、密钥、自定义脚本、数据)都在这个项目的工作区里，全局的 `settings/` 只放全局的。

## 流程

| 步骤 | 命令 | 程序 | 做什么 |
|---|---|---|---|
| 1 指定仓库 | `tightrein project add <仓库路径> [--name <名>]` | `add.py` | 先列出要建的目录与文件(确认后执行)；已有同名工作区时拒绝，不覆盖 |
| 2 探测 | (add 自动做) | `detect.py` | 只读文件与 git，不调用模型：测试、lint、构建、类型检查命令，主分支，Sentry SDK，接口描述文件，CI 与部署工作流；生成 `setup.json`、`settings.json` 的草稿，探测不到的写 null；从提交历史推断的提交与分支风格写进 setup.md 与命令输出，只供确认，不自动采用 |
| 3 确认 | 用户编辑 `setup.json`、`settings.json`，填 `sites.json`、`secrets.json` | — | 每个模块定启用、不启用还是自定义；自定义的按方法文档写 `scripts/` 下的脚本。`release.accept`(验收：不启用时合并并部署即完成，不观察回归)与 `release.github_issues`(GitHub Issue 镜像：不启用时只记在本地)只是开关，只能 enabled 或 disabled，不启用不算盲区 |
| 4 试跑 | `tightrein project check [<模块>]` | `check.py` | 每个启用与自定义的模块各试跑一次(只取数据，不调用模型)；只读 worktree 切到主分支，把检查命令全量跑一次 |
| 5 就绪 | `tightrein project ready` | `cli/commands/project.py` | 试跑全部通过、之后没改过 `setup.json` 才标为就绪，并装上定时器；调度只跑就绪的项目 |

`setup.md` 由 `render.py` 从 `setup.json` 渲染(add 之后、每次试跑之后、`project show` 时)，不手写。

## 输入与输出

### 工作区目录

```
workspaces/<项目>/
  setup.json      接入清单：每个模块启用、不启用还是自定义(唯一来源；读取与校验在 setup.py)
  setup.md        接入清单的渲染
  settings.json   项目级配置：project(项目事实)与 overrides(对全局控制字段的覆盖)
  sites.json      平台地址、查询条件(不提交)
  secrets.json    密钥(不提交，权限 600)
  scripts/        自定义模块的脚本
  knowledge/      知识库条目(conventions/、patterns/、lessons/)
  data/           运行中产生的一切(数据库、issues/、problems/、runs/)
  worktrees/      实施用的 worktree；试跑的只读基线 worktree 为 worktrees/baseline
```

### 试跑结果

存 store 的 state 表 `onboard.check`：时间、`setup.json` 的哈希、每一项(`模块键` 或 `baseline.<命令名>`)的 passed、failed、skipped 与说明、错误明细。`project ready` 写 `onboard.ready`；重新试跑会清掉就绪，要再执行一次 ready。

### 自定义脚本的协议

- 命令：`scripts/` 下的脚本，`.py` 用 tightrein 自己的解释器跑，其余直接执行；工作目录是工作区；
- 标准输入：一个 JSON 对象 `{"trial": true, "project", "module", "scratch"}`；`scratch` 是这次调用专用的临时目录，结束即删；
- 环境变量：只给白名单(protocol/security.child_env)，加上 `setup.json` 中该模块登记的凭据(`TIGHTREIN_SECRET_<条目名>`)；
- 标准输出：单个 JSON 对象；方法文档旁有 `<文档名>.schema.json` 时按它校验，列出每条错误的路径；
- 归类：无法启动、超时为不通过；标准输出不是单个 JSON 对象为协议错误；退出码非 0 但标准输出是合法响应时以响应为准；退出码非 0 且没有合法响应时附上标准错误的最后几行(已脱敏)。

## 配置

- 读取顺序：`settings/defaults.json` → `settings/controls.json`(本机) → 工作区 `settings.json` 的 `overrides`，后者覆盖前者；单个值直接覆盖，列表缺省整体替换，键名后加 `+` 表示追加；受保护文件两级只能追加(settings/load.py)；
- `tightrein project config [<键>] --explain` 列出每一层给出的值；
- 试跑的超时：自定义脚本取 `limits.timeouts.command`，基线检查命令取 `limits.timeouts.tests`。

## 设计依据

- **接入期只做只读的事**：识别技术栈并推荐检查命令、把只读 worktree 切到主分支全量跑一次检查命令确认基线可用；接入中不采集入库、不改代码、不提 PR(旧 `orchestrator/onboarding/service.py`、`checklist.py`)。基线不过时，之后每个 Issue 的自检都会被既有失败干扰，先在接入时暴露出来。
- **探测不到写 null**：不猜，交给用户在 `setup.md` 看到「待填」；`setup.load` 对漏写与缺字段报错，不按缺省悄悄处理(44 号计划「接入清单」)。
- **脚本结果归类**：沿用旧 `extensions/invoke.py:interpret` 的规则(非 0 退出码以合法响应为准、单个 JSON 对象、每条 schema 错误带路径、每次调用一个临时目录)，项目脚本出错时给出的信息足够定位。
- **就绪带 setup.json 的哈希**：试跑之后又改了清单，就要重新试跑，避免按没验证过的配置无人值守运行。

## 不做什么

- 不调用模型(会调用模型的静态巡检在试跑中跳过，第一次运行时再看)；
- 不改项目仓库(不 fetch 以外的写操作；只读基线 worktree 建在工作区里)；
- 不替用户决定模块启用与否，只给出探测到的线索。
