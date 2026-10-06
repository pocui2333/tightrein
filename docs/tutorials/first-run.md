# 第一次运行

本教程用一个只有几行代码的示例项目，带你走完 tightrein 的接入流程，并运行第一次巡检。完成后你会知道：

- 怎样接入一个项目，以及接入时 tightrein 会检查什么；
- 审批关卡长什么样：tightrein 要做有影响的操作时，会先停下来等你确认；
- 怎样手动触发一次巡检，以及在哪里看结果。

全程大约 15 分钟。除最后一步外都不调用模型，不产生费用。

## 准备

- 已按 [README](../../README.md#安装) 安装 tightrein，并把 `core/.venv/bin` 加入了 `PATH`(`tightrein --version` 能输出版本号)；
- 已按 [README](../../README.md#快速上手) 写好 `~/.config/tightrein/config.yaml`；
- 本教程的命令都在 tightrein 仓库的根目录执行。

## 1. 建一个示例项目

示例项目有一个缺陷：`average([])` 会因除以零而崩溃。

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

printf '.venv/\n__pycache__/\n' > .gitignore
python3 -m venv .venv && .venv/bin/pip install -q pytest
```

tightrein 要求项目有 `origin` 远程。这里用一个本地的裸仓库代替 GitHub：

```sh
git init -q -b main && git add . && git commit -qm "init"
git init -q --bare ~/demo-app-origin.git
git remote add origin ~/demo-app-origin.git && git push -q -u origin main
```

回到 tightrein 仓库的根目录，继续下面的步骤。

## 2. 新建工作区

```sh
tightrein project init --workspace workspaces/demo --repo ~/demo-app
```

预期输出(节选)：

```
已新建工作区 .../workspaces/demo
接入中：完成 1 项，还差 6 项需要回答，0 项失败
- [完成] stack 识别技术栈：python(pyproject.toml)
- [需要回答] checks 检查命令在基准版本上通过：没有配置检查命令(checks.commands)；推荐：采用 python -m pytest -q
- [需要回答] conventions 项目约定(分支、提交、PR)：……
```

输出的最后一行提示下一步：`下一步：tightrein project worktree init --workspace workspaces/demo`。工作区 `workspaces/demo/` 保存这个项目的配置(`project.yaml`)、Issue 与运行数据。接入完成之前，tightrein 只做只读的事。

## 3. 建只读 worktree：你的第一个审批

tightrein 在项目的一份独立检出(worktree)上取证和运行检查，不碰你正在用的工作目录。

```sh
tightrein project worktree init --workspace workspaces/demo
```

tightrein 不会直接执行，而是列出将要做什么、会不会影响远程、怎样撤销，然后停下：

```
……
是否影响远程：否
能否撤销：能；撤销方法：git worktree remove .../workspaces/demo/worktrees/readonly
同意后执行：tightrein approve OP-0001；拒绝：tightrein reject OP-0001
```

这就是审批关卡。确认执行：

```sh
tightrein approve OP-0001 --workspace workspaces/demo
```

预期输出：`OP-0001 已执行`。

## 4. 回答接入问题

检查命令是修复后自检的依据。只读 worktree 里没有示例项目的 `.venv`，所以这里写虚拟环境中 Python 的绝对路径：

```sh
tightrein project answer checks --workspace workspaces/demo \
  --value "$HOME/demo-app/.venv/bin/python -m pytest -q"
```

tightrein 会立即在主分支上运行这条命令，预期输出中出现：

```
- [完成] checks 检查命令在基准版本上通过：1 条命令在主分支上通过
```

其余几项(项目约定与各平台接入)对示例项目都采用推荐答案：

```sh
for item in conventions platform:deploy-source platform:error-tracking platform:log-platform platform:alert-source; do
  tightrein project answer "$item" --workspace workspaces/demo --recommended
done
```

## 5. 确认接入完成

```sh
tightrein status --workspace workspaces/demo
```

预期输出中不再有「接入中」，待处理为 0 项：

```
待处理 0 项
还没有运行记录
```

## 6. 运行第一次巡检

先看看将要执行什么：

```sh
tightrein run --workspace workspaces/demo --select collect+ --probe static --dry-run
```

```
将要执行(按当前状态判断，前面的步骤新产生的问题与 Issue 不计入)：
- 执行 chain-collect：链选择：collect --probe static
- 执行 chain-aggregate：链选择：aggregate
……
```

`--select collect+` 表示从采集开始往后推进，`--probe static` 指定用静态审查采集。去掉 `--dry-run` 正式运行。**这一步会调用模型并产生费用。**

```sh
tightrein run --workspace workspaces/demo --select collect+ --probe static
```

运行期间可以在另一个终端用 `tightrein watch` 实时查看进度。完成后：

```sh
tightrein status --workspace workspaces/demo
```

如果审查发现了 `average([])` 的除零缺陷，分诊会给出结论，Issue 写在 `workspaces/demo/issues/` 下；需要你决定的事项(例如放行 Issue)列在 `status` 中，每件附推荐做法与对应的命令。

## 下一步

- 放行 Issue 后，用 `tightrein fix` 系列命令修复，或在 `project.yaml` 的 `gates` 中放开关卡，让 tightrein 无人值守地推进：见[配置与运行说明](../reference/configuration.md#审批关卡预算与无人值守)。
- 接入错误追踪、日志与告警平台：见[方法目录](../reference/methods.md)。
- 编写项目自己的探针：见[如何编写项目探针](../how-to/write-project-probe.md)。
- 清理本教程的产物：删除 `workspaces/demo/`、`~/demo-app/` 与 `~/demo-app-origin.git/`。
