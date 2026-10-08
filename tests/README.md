# tests

tightrein 自己的测试。不联网、不调用真实模型；依赖外部工具的标为 `external`，没装时跳过。

## 结构

与 `src/tightrein/` 一一对应：每个阶段、模块、小步骤一个文件夹，测试文件为源文件名加 `test_` 前缀。找某段代码的测试，
按同一路径到 `tests/` 下找：

```
src/tightrein/implement/check/rules.py   →  tests/implement/check/test_rules.py
src/tightrein/protocol/git/github.py     →  tests/protocol/git/test_github.py
```

只有两个文件夹不对应源码：

| 文件夹 | 内容 |
|---|---|
| `whole/` | 整体测试：示例项目从采集跑到发布(`test_pipeline.py`)；回归路径(`test_regression.py`) |
| `fixtures/` | 共用的样例：示例项目与工作区、假 GitHub、录制的模型输出 |

- pytest 用 importlib 导入模式(`pyproject.toml` 的 `--import-mode=importlib`)：测试文件夹不需要 `__init__.py`，不同
  文件夹里同名的测试文件互不冲突；也因此测试文件之间不能互相 import。同一文件夹共用的夹具写在该文件夹的
  `conftest.py`；跨文件夹共用的放 `fixtures/`，由用到它的文件夹的 `conftest.py` 把 `tests/` 放进导入路径后
  `from fixtures import sample`(见 `whole/conftest.py`)；
- 依赖外部工具(Semgrep、Schemathesis、Playwright)的测试放在对应模块的文件夹，标 `@pytest.mark.external`，并在工具
  没装时 `pytest.skip`；
- 测试与代码一起改；按约定改动做完后整体跑一次全部测试，不一小块一小块地测。

## 怎么跑

在仓库根目录(用仓库的 `.venv`)：

```sh
.venv/bin/python -m pytest                                        # 全部
.venv/bin/python -m pytest -m "not external" -p no:cacheprovider  # 跳过依赖外部工具的(CI 的写法)
.venv/bin/python -m pytest tests/whole -q                         # 只跑整体测试(约一分钟)
.venv/bin/ruff check src tests && .venv/bin/mypy                  # 静态检查
```

整体测试要 `git` 2.38 以上(假 GitHub 合并用 `git merge-tree --write-tree`)。

## 整体测试与样例(`whole/`、`fixtures/`)

`whole/test_pipeline.py` 经命令行入口(`cli.main.main`，与 `tightrein run -p demo` 相同的组装)一轮轮推进，
每轮之后把 `FixedClock` 拨快 13 小时，直到 Issue 结束：

1. 采集：项目探针 `fixtures/probe_average.py` 取远程主分支的 `calc.py`，真跑 `average([])`，崩溃即报信号 → 去重成问题；
2. 评估：取证(回放) → 写成 Issue，低风险自动放行；
3. 实施：准备 worktree → 定位(评估的代码笔记已有核心位置，跳过) → 方案(回放) → 定案(自动) → 编码(回放，录制的
   `changes.patch` 在 worktree 里改代码) → 自检(真跑示例项目的 pytest) → 审查(回放) → 交付；
4. 发布：提交、推送到本地裸远程、假 GitHub 的 PR 与 CI、合并(在裸仓库上真的合进 main)；
5. 验收：没有部署来源时按合并时间加 `assumeDeployedAfter` 视为已部署，观察期内探针在线上版本上不再报即通过；
6. 每轮结束都复盘。

断言：每一步的 handoff.json 落盘且为 passed、Issue 最终 done、`90-issue-deliver.md` 存在、修复进了远程 main、
没有 TCP 连接、没有启动联网程序与 agent 工具、模型调用全部来自录制；部署后才能确认的验收标准(观察期内不再出现)
不交给审查判断，由验收按它确认。

`whole/test_regression.py` 走回归路径(情形 `regression`)：第一次编码只处理了 `None`，合并后探针在线上版本上照报 →
验收判为回归，提撤销 PR、退回待修 → 第二次修复(上一次的交接归档在 `attempt_1/`，新的修复目录、新的 PR) → 验收
通过。

`fixtures/` 里的东西：

| 文件 | 用途 |
|---|---|
| `sample.py` | 建示例项目(`demo-app`：`calc.py` 有缺陷、pytest 测试、本地裸远程)与工具目录、示例工作区；`Gateway`(测试的 ProcessRunner)、`FakeGitHub`(gh 的替身)、`SampleReplay`(回放)、`Recorder`(录制)、`harness`、`drive` |
| `overrides.json` | 示例工作区 `settings.json` 的 overrides：登记探针；偶发信号出现 1 次即成问题 |
| `probe_average.py` | 示例探针，复制进工作区 `scripts/` |
| `answers/` | 手写的模型结构化结果：`<调用点>.json`(按该步骤的 schema)、`<调用点>.patch`(可写调用的改动)；同一调用点不同轮次的写成 `<调用点>.r<轮>.json`；同一(调用点、轮次)的第 N 次调用(如回归后的第二次修复)写成 `<调用点>[.r<轮>].c<N>.json` 与 `.patch` |
| `answers/regression/` | 回归情形先找这里，没有的再取 `answers/` |
| `recordings/`、`recordings_regression/` | 由 `record.py` 从 `answers/` 生成的录制集(两种情形各一份)，回放用；不要手改 |
| `record.py` | 重新录制 |

别的测试要一个能跑完整流程的工作区时，用 `sample.harness(tmp_path)` 取得 `Harness`：`h("run")` 执行一次命令，
`h.clock.advance(...)` 拨时钟，`h.github.pulls`、`h.runner.commands` 看外部调用。

### 录制的格式

照 `src/tightrein/agents/tools/replay.py`：`recordings/index.json` 按(调用点、对象、轮次、第几次调用)登记每次工具
调用，`<目录>/stdout.jsonl` 是工具的原始输出(这里是 `claude -p --output-format stream-json` 的格式)，`changes.patch`
在第一次调用时用 `git apply` 打进工作目录。每条带 `sha256`(提示、schema、访问级别)：与回放时的调用不符即拒绝回放。
提示中的样例根目录、解释器路径与 ULID 编号先换成 `<sample>`、`<python>`、`<ulid>` 再算哈希，所以录制与临时目录、
机器无关。

### 重新录制

提示模板、schema、流程或示例项目变了，回放会失败：整体测试的断言不过，失败调用的 `data/<对象>/…-raw.jsonl`
与运行摘要里出现 `replay-task-changed`(哈希不符)或 `replay-missing`(多了一次调用)。这时：

```sh
.venv/bin/python tests/fixtures/record.py --dry-run                 # 先试：打印每轮的步骤与缺答案的调用点，不写录制集
.venv/bin/python tests/fixtures/record.py                           # 清空并重写全部情形的录制集
.venv/bin/python tests/fixtures/record.py --scenario regression     # 只录一种情形(fix、regression)
```

缺答案的调用点会列出来：按该步骤的 `<名>.schema.json` 在 `answers/` 补一份 `<调用点>.json`，再跑一次。录制完跑
`.venv/bin/python -m pytest tests/whole -q` 确认，录制集(`recordings/`、`recordings_regression/`)与 `answers/` 一起提交。

要用真实模型的输出：失败的调用总会保存 `…-prompt.md` 与 `…-raw.jsonl`；成功的要带 `--debug` 运行(设
`AgentContext.debug`，成功的调用也保存 prompt 与 raw)：

```sh
tightrein run -p <项目> --debug                      # 一轮；只推进一个对象时加 --object <编号>
ls <工作区>/data/issues/<编号>/*-raw.jsonl              # 每次调用一份(问题的在 problems/，运行级的在 runs/)
```

raw 去掉 tightrein 的分隔行(`{"tightrein": {...}}`)即是 `stdout.jsonl`；更省事的是把 `result` 行的
`structured_output` 存成 `answers/<调用点>.json`，再用 `record.py` 生成。
