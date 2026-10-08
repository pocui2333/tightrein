# 第一次运行

用一个只有几行代码的示例项目，走完一遍：安装 → 接入(`project add`) → 填四个文件 → 试跑(`project check`) → 就绪(`project ready`) → 运行(`run`) → 看结果(`status`、`watch`)。

做完你会知道：

- 一个项目的工作区里有哪些文件，各管什么；
- 接入时 tightrein 检查什么，为什么试跑通过才能就绪；
- 怎样手动触发一次运行，停在人工关卡时在哪里看、怎么回应。

第 1 到第 6 步不调用模型。第 7 步提一个需求，会调用模型(按订阅额度计，不按次付费)。

## 0. 准备

- macOS(定时器用 launchd；其他系统也能手动运行，只是 `project ready` 不装定时器)；
- Python 3.12 以上、git；
- 已登录的 [GitHub CLI](https://cli.github.com/)(`gh auth status` 通过)：发布阶段用它提 PR、看 CI；
- agent 工具：缺省配置用到 Claude Code(`claude`)与 Antigravity CLI(`agy`)，都用订阅登录。只有 `claude` 时见第 4 步的「只用 Claude」。

## 1. 安装

```sh
git clone https://github.com/pocui2333/tightrein.git
cd tightrein
python3.12 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/tightrein admin install
```

`admin install` 先检查后执行：检查 agent 工具装了没有、`vendor/` 中第三方 skills 的副本与 `vendor/lock.json` 是否一致；都通过才给 agy 的命令白名单补上只读命令，并在 `~/.local/bin` 建 `tightrein` 命令链接。只想看它要做什么就加 `--dry-run`。

`~/.local/bin` 在 `PATH` 里时，之后在任何目录都能直接敲 `tightrein`；下面的命令都这样写。

```sh
tightrein --version
```

工作区建在 tightrein 仓库的 `workspaces/` 下(不进 git)。想放在别处，设环境变量 `TIGHTREIN_HOME` 指向一个含 `settings/` 的目录。

## 2. 建一个示例项目

示例项目有一个缺陷：`average([])` 会因除以零崩溃。

```sh
mkdir -p ~/demo-app/tests && cd ~/demo-app

cat > calc.py <<'EOF'
def average(values):
    return sum(values) / len(values)
EOF

cat > tests/test_calc.py <<'EOF'
from calc import average


def test_average():
    assert average([1, 2, 3]) == 2
EOF

cat > pyproject.toml <<'EOF'
[project]
name = "demo-app"
version = "0.1.0"

[tool.pytest.ini_options]
pythonpath = ["."]
EOF

python3 -m venv ~/demo-venv && ~/demo-venv/bin/pip install -q pytest

git init -q -b main && git add . && git commit -qm "init"
git init -q --bare ~/demo-app-origin.git
git remote add origin ~/demo-app-origin.git && git push -q -u origin main
```

这里用一个本地的裸仓库当 `origin`：tightrein 从 `origin` 取主分支的最新 commit。没有 GitHub 远程时，发布阶段提 PR 这一步做不了，教程只走到实施。

## 3. 接入：`project add`

```sh
tightrein project add ~/demo-app
```

它先列出要建的目录与文件，问 `y/N`；确认后：

1. 建工作区 `workspaces/demo-app/`(名字缺省取仓库目录名，`--name` 可改)；
2. 探测仓库(只读文件与 git，不调用模型)：从 `pyproject.toml`、`package.json`、`Makefile` 等取测试、lint、构建、类型检查命令，从 git 取主分支，看依赖里有没有 Sentry SDK、有没有接口描述文件、有没有 CI 与部署工作流；
3. 写出 `setup.json`、`settings.json` 的草稿与 `setup.md`。探测不到的写 null，`setup.md` 里列为「待填」。

工作区里的文件：

```
workspaces/demo-app/
  setup.json      接入清单：每个模块启用、不启用还是自定义(唯一来源)
  setup.md        接入清单的渲染，给人看；不要手改
  settings.json   项目级配置：project(项目事实)与 overrides(对全局配置的覆盖)
  sites.json      平台地址、查询条件(不提交)
  secrets.json    密钥(不提交，权限 600)
  scripts/        自定义模块的脚本
  knowledge/      知识库条目
  data/           运行中产生的一切：数据库、issues/、problems/、runs/
  worktrees/      实施用的 worktree；试跑用的只读基线在 worktrees/baseline
```

输出最后一行是下一步命令。现在先别急着试跑，草稿还有待填项。

## 4. 填四个文件

### setup.json：每个模块启用与否

打开 `workspaces/demo-app/setup.json`。`modules` 下每个模块都必须明确写出，三种状态各有必填字段：

| `status` | 意思 | 必填 |
|---|---|---|
| `enabled` | 启用 | 模块有可选方法时写 `method`(如平台错误的 `sentry`) |
| `disabled` | 不启用 | `reason`；`impact` 写少了什么能力、靠什么兜底(为 null 时在 `status` 与 `setup.md` 中列为盲区) |
| `custom` | 项目自己实现 | `script`(相对工作区的脚本路径)、`guide`(照哪份方法文档写的)、`reason` |

不适用的字段写 null，不省略。示例项目没有线上服务，除任务外发现与验收外都不启用，静态巡检也先关掉(它会调用模型)：

```json
{
  "project": "demo-app",
  "updatedAt": "2026-10-08T00:00:00Z",
  "modules": {
    "collect.project_probes":  {"status": "disabled", "method": null, "script": null, "guide": null, "reason": "示例项目没有定时任务", "impact": null},
    "collect.platform_errors": {"status": "disabled", "method": null, "script": null, "guide": null, "reason": "没有接错误追踪平台", "impact": null},
    "collect.access_log":      {"status": "disabled", "method": null, "script": null, "guide": null, "reason": "没有线上服务", "impact": null},
    "collect.alerts":          {"status": "disabled", "method": null, "script": null, "guide": null, "reason": "没有告警系统", "impact": null},
    "collect.api_fuzz":        {"status": "disabled", "method": null, "script": null, "guide": null, "reason": "没有接口", "impact": null},
    "collect.static":          {"status": "disabled", "method": null, "script": null, "guide": null, "reason": "教程中先不调用模型", "impact": "需求由用户用 tightrein new 提"},
    "collect.incidental":      {"status": "enabled",  "method": null, "script": null, "guide": null, "reason": null, "impact": null},
    "implement.check.runtime": {"status": "disabled", "method": null, "script": null, "guide": null, "reason": "没有要启动的服务", "impact": "只靠单元测试"},
    "release.deploy":          {"status": "disabled", "method": null, "script": null, "guide": null, "reason": "没有部署", "impact": "合并后按 assumeDeployedAfter 视为已部署"},
    "release.accept":          {"status": "enabled",  "method": null, "script": null, "guide": null, "reason": null, "impact": null},
    "release.github_issues":   {"status": "disabled", "method": null, "script": null, "guide": null, "reason": "Issue 只记在本地", "impact": null}
  }
}
```

每个模块能选什么、要填什么，见各模块的 README(`src/tightrein/collect/<模块>/README.md` 等)与 [平台方法](../reference/methods.md)。

### settings.json：项目事实与覆盖

```json
{
  "project": {
    "repo": "/Users/you/demo-app",
    "mainBranch": "main",
    "language": "zh",
    "commands": {"test": "/Users/you/demo-venv/bin/python -m pytest -q", "lint": null, "build": null, "typecheck": null},
    "testPatterns": ["*_test.py", "test_*.py", "tests/**"],
    "frontendPatterns": null
  },
  "overrides": {}
}
```

- `project`：项目事实。`repo` 写绝对路径；`language` 是输出语言(`zh` 或 `en`)；`commands` 是项目的检查命令，探测出的写法(如 `python -m pytest -q`)在你的环境里不一定能跑，按实际改。命令在 worktree 中执行，被 git 忽略的目录(如项目里的 `.venv`)不在 worktree 里，所以这里写解释器的绝对路径；装依赖之类的准备命令写成 `commands.prepare`。
- `overrides`：对全局配置的覆盖，键与 `settings/defaults.json` 相同，只写要改的。例如把改动量上限调小、给这个项目登记项目探针：

  ```json
  "overrides": {
    "boundaries": {"changeCap": {"files": 5, "lines": 200}},
    "controls": {"implement.code": {"turns": 20}}
  }
  ```

  读取顺序 `settings/defaults.json` → `settings/controls.json`(本机) → 这里，后者覆盖前者；列表整体替换，键名后加 `+` 表示追加。看某个键最后取哪个值、来自哪一层：

  ```sh
  tightrein project config boundaries.changeCap --explain -p demo-app
  ```

全部键与缺省值见 [配置字段](../reference/configuration.md)。

### sites.json：平台地址

按平台分组写地址与查询条件，不写密钥。示例项目不需要，保持 `{}`。真实项目例如：

```json
{
  "target": {"baseUrl": "https://staging.example.com", "environment": "staging"},
  "sentry": {"url": "https://sentry.io", "organization": "my-org"}
}
```

`target` 是被测服务的地址，项目探针与 API 模糊测试共用。全局的(不属于某个项目的，如 GitHub 的地址)写在 `settings/sites.json`，格式照 `settings/sites.example.json`。

### secrets.json：密钥

平铺的条目，键名写成「平台.条目」，值一律是字符串：

```json
{"sentry.token": "…", "loki.token": "…"}
```

文件权限必须是 600(`chmod 600 workspaces/demo-app/secrets.json`)，否则拒绝运行。读到的值登记进脱敏，永远不交给 agent 进程，不写进提示、交接文件与日志。示例项目保持 `{}`。

### 只用 Claude 时

缺省配置中有几个调用点用 agy 的模型(别名 `flash`、`flash-high` 等)。没有 agy 时，在 `settings/controls.json`(本机，不提交)把它们改成 Claude 的模型：

```json
{
  "controls": {
    "assess.refute": {"model": "sonnet"},
    "assess.dedup": {"model": "sonnet"},
    "implement.locate": {"model": "sonnet"},
    "implement.design.frontend": {"model": "sonnet"},
    "implement.check.screenshots": {"model": "sonnet"},
    "implement.review.deep": {"model": "sonnet"}
  }
}
```

证伪复核与深度审查要求与被审的一方用不同的模型(`independence`)，所以这里用 `sonnet` 而不是缺省的 `opus`。

## 5. 试跑：`project check`

```sh
tightrein project check -p demo-app
```

每个启用与自定义的模块各试跑一次：只取数据，不调用模型，不写入问题；会调用模型的模块与没有单独取数程序的模块记为跳过。另外把只读基线 worktree 切到主分支，把 `project.commands` 全量跑一次，确认基线可用：基线不过，之后每个 Issue 的自检都会被既有失败干扰。

```
试跑：通过 1，不通过 0，跳过 2；结果已写进 …/workspaces/demo-app/setup.md
  collect.incidental  跳过  没有新的或内容变了的交接文档
  release.accept  跳过  没有可单独试跑的取数程序，在第一次运行时检查
  baseline.test  通过  `/Users/you/demo-venv/bin/python -m pytest -q` 在 430fa74b4010 上通过
下一步：tightrein project ready
```

有不通过的，按说明改文件再试跑。`setup.json` 不合格(漏写模块、缺必填字段、脚本不存在)时直接列出全部问题。只试跑一个模块：`tightrein project check collect.platform_errors`。随时用 `tightrein project show` 在终端看接入清单。

## 6. 就绪：`project ready`

```sh
tightrein project ready -p demo-app
```

试跑全部通过、且之后没改过 `setup.json` 才能标为就绪；改过就要重新试跑。在 macOS 上它同时装上定时器(用户级 LaunchAgent `local.tightrein.demo-app`)：每 `schedule.tick`(缺省 15m)醒来一次，只在 `schedule.window`(缺省 00:00 到 07:00)内运行，自动推进到 `schedule.advanceTo`(缺省 release)为止。调度只跑就绪的项目；手动的 `run` 不受时段限制。

## 7. 运行：`run`

先看一轮会做什么：

```sh
tightrein run --dry-run -p demo-app
```

```
将要做 2 件事(--dry-run，没有执行)：
  collect  collect.incidental  到点
  retro  -  每次运行结束都执行
```

示例项目没有启用会产出问题的来源，我们自己提一个需求(这一步开始调用模型)：

```sh
tightrein new "average([]) 在空列表上除以零崩溃，应返回 0" --type bug --severity P2 -p demo-app
```

`new` 直接进评估：取证、定严重度与处理方式，要修的写成 Issue。然后推进一轮：

```sh
tightrein run -p demo-app
```

不带阶段时按状态推进：采集 → 评估 → 实施 → 发布，最后总是复盘。实施是一条流程：准备 → 定位 → 方案 → 定案 → 编码 → 自检 → 审查 → 交付，按已有信息跳过。遇到要人决定的就停在关卡，退出码为 4。只跑一个阶段写 `tightrein run implement`，只推进一个对象写 `--object 0001`。

## 8. 看结果：`status` 与 `watch`

```sh
tightrein status -p demo-app      # 一次性快照；不带命令时也是它
tightrein watch -p demo-app       # 实时界面，在另一个终端开着；p 暂停、s 急停、c 切换采集看板、q 退出
```

`status` 依次是：系统总览(接入、控制、时段、下次定时、额度)、等你处理、进行中与排队、各阶段存量、今天与本周的汇总、健康与盲区。「等你处理」的每一条都给出要敲的命令与对应文档，例如：

```
▲ 等你处理 1
  0001 P2 待审核 average 空列表 · 实施·定案 · 已等 3m · 方案需确认
       tightrein approve 0001                     data/issues/0001/90-issue-pending.md
```

回应关卡：

```sh
tightrein show 0001 --doc pending       # 看待审核文档：要决定什么、推荐与理由、不决定会怎样
tightrein approve 0001                  # 通过；要拍板的加 --option <序号>
tightrein reject 0001 --note "空列表应抛 ValueError"   # 不通过，方案按原因重出
tightrein run --object 0001             # 接着推进
```

一个 Issue 给人看的只有三种文档：`90-issue-pending.md`(待审核)、`90-issue-failure.md`(出问题)、`90-issue-deliver.md`(交付)。`tightrein show 0001 --steps` 列出每一步的结论与量化数据(耗时、token、轮数)。

控制运行：`pause`(当前一步做完后停)、`stop`(急停，当前一步退回检查点)、`resume`；`take 0001` 手动接管、`give 0001` 交还。

## 9. 收尾

```sh
tightrein project remove demo-app       # 卸下定时器、删除工作区；不动仓库
rm -rf ~/demo-app ~/demo-app-origin.git ~/demo-venv
```

## 接下来

- 接入真实项目：[接入项目](../how-to/onboard-project.md)；
- 全部命令：[命令](../reference/commands.md)；
- 整体怎么组成：[总体架构](../explanation/overview.md)。
