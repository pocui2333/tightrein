# 参与贡献

感谢你对 tightrein 的关注。欢迎提交缺陷报告、功能建议、文档改进与代码。

## 先讨论，再动手

- **缺陷**：先搜索[已有 Issue](https://github.com/pocui2333/tightrein/issues)，没有重复时用「缺陷报告」模板新建，附上复现步骤与 `tightrein --version` 的输出。
- **新功能或较大的改动**：先开 Issue 说明动机与设想，达成一致后再提交 PR。
- **小改动**(错别字、文档措辞、明显的小缺陷)：可以直接提交 PR。
- **安全漏洞**：不要公开提交，按 [SECURITY.md](SECURITY.md) 私下报告。

## 实现原则(tightrein 自身的代码)

只管 tightrein 本身的代码：改 tightrein 都必须遵守。不是 tightrein 运行时给被管理项目写代码的规则(那是 `src/tightrein/protocol/coding.md`)。优先级从高到低：

### 1 运行速度优先

选对算法与数据结构，而不是事后补优化：

- 查找用哈希表与索引：指纹、编号、路径匹配用字典或集合 O(1) 查找；数据库只给常用查询建索引；不在循环里逐条查库(避免 N+1)，一次批量取；
- 增量与跳过：按内容哈希、commit、上次读取位置判断，没变就不做(采集调度、静态巡检、基线缓存都按此)；
- 并行：相互独立的来源、检查、文件处理并行(并发数由 `protocol/resources.md` 管)；
- 流式处理：大日志、大输出边读边处理，不整体读进内存；
- 不重复计算：同一次运行内算过的(代码摘要、diff、哈希、解析结果)缓存在内存中复用；正则预编译；
- 子进程少起：能一次命令拿到的不分多次(例如一次 `git` 取多项信息)；
- 每一步的耗时都在量化数据中(`durationMs`)，慢在哪里看数据，不凭感觉。

### 2 可读性第二

- 命名：见名知意，用领域里的词(与阶段、模块、小步骤、字段名一致)，不用缩写与含糊的词(data、info、handle、manager)；
- 排版：一个文件一件事，一个函数一件事；文件按「常量 → 类型 → 公共函数 → 内部函数」排列；函数短，嵌套浅，提前返回；
- 注释：只在重要位置写精炼的注释，说明「为什么」而不是「做了什么」：非显然的取舍、算法选择、边界条件、对外部工具行为的依赖(注明工具版本)；
- 类型：全部函数写类型标注；数据结构用 dataclass，不在模块之间传裸字典。

### 3 通用，不写特例

- 遇到一个具体问题，先想它属于哪一类，按类解决：放进协议层或配置，由数据驱动，不在步骤里加 `if 某种情况`；
- 新增同类情况只改配置或加一条数据(一行规则、一个方法清单、一份方法文档)，不需要再写代码；
- 这条与复盘的改进规范(`src/tightrein/retro/improve.md`)一致：先归类到协议层，能配就不写死。

### 4 不丢原有的精妙设计

重新设计不是推倒重写：验证过、解决过真实问题的设计要保留，不能丢，也不能在改动中改坏。每个模块 README 的「设计依据」记着这些设计与出处；改动前先读，改动后有测试覆盖它。

### 5 复用与提炼公共代码

- 同样的逻辑只写一处：子进程调用(`protocol/process.py`)、路径计算(`store/files/layout.py`)、交接读写与校验(`protocol/handoff.py`)、脱敏、时间与编号、重试退避都只有一份实现，各模块调用；
- 出现第二次就提炼：第二处要写相似代码时，先抽成公共函数再用；
- 公共代码放在真正共用它的最近一层：只在一个阶段内共用的放在该阶段文件夹，跨阶段的才放协议层或 store；不设笼统的 `common/`、`utils/`；
- 旧代码中能用的直接搬过来改名归位，不为了「新」而重写。

### 6 补充

- 外部输入一律在边界校验(schema)，内部不重复防御性检查；
- 错误分类与处理只在一处(`protocol/limits.md` 的失败类型表)，各模块只抛出带类型的错误，不各自吞掉或各自重试；
- 不留死代码与注释掉的代码，不留「以后可能用」的参数与开关；
- 依赖从少：标准库能做的不引第三方库；新增依赖要写明理由(写在 `pyproject.toml` 的依赖旁)；
- 测试与代码一起改，最后整体跑一次全部测试。

## 开发环境

需要 macOS、Python 3.12+ 与 git。仓库用 src 布局：代码只在 `src/tightrein/`，测试必须先安装包才能导入，测的是安装后的包。

```sh
git clone https://github.com/pocui2333/tightrein.git
cd tightrein
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

`[dev]` 装上 pytest、mypy、ruff。静态检查：

```sh
.venv/bin/ruff check src tests
.venv/bin/mypy
```

## 代码结构

```
src/tightrein/
  collect/      采集：七个来源与去重
  assess/       评估：取证、评级、去向；issue/ 写成 Issue
  implement/    实施：准备、定位、方案、定案、编码、自检、审查、交付
  release/      发布：提 PR、CI、合并、部署、验收(accept/)、清理
  retro/        复盘：tightrein 自身问题的记录簿
  knowledge/    项目知识库
  onboard/      接入项目
  agents/       调用 AI 的唯一入口与各工具的适配器
  prompts/      所有调用点的提示模板
  protocol/     协议层：边界、交接、时限、资源、恢复、安全、记录、调度、git
  store/        存储：数据库、文件、锁、幂等键
  settings/     配置的读取、合并与校验(取值在仓库根的 settings/)
  cli/          命令行：解析、分发、渲染、文案表
settings/       全局取值
vendor/         锁定版本的第三方 skills
tests/          测试，结构与 src/tightrein/ 一一对应
docs/           文档：教程、操作指南、参考、总体架构
```

每个模块文件夹的 `README.md` 用同一个模板(是什么、流程、输入与输出、配置、设计依据、不做什么)。行为变了就同步改旁边的 README；总体架构见 [docs/explanation/overview.md](docs/explanation/overview.md)。

## 测试

- `tests/` 与包一一对应：每个阶段、模块、小步骤一个文件夹，测试文件与源文件同名加 `test_` 前缀(`src/tightrein/collect/dedup/group.py` 的测试在 `tests/collect/dedup/test_group.py`)；找某段代码的测试，按同一路径去 `tests/` 下找；
- 用 pytest 的 importlib 导入模式(`pyproject.toml` 的 `--import-mode=importlib`)：不同文件夹里可以有同名的测试文件，测试文件夹一般不需要 `__init__.py`；
- 模块测试不联网、不调用模型：模型输出用录制的样例回放(`agents/tools/replay.py`)，外部命令与 HTTP 经注入的假实现替代；
- 依赖外部工具的(Semgrep、Schemathesis、Playwright)放在对应模块文件夹，标记 `@pytest.mark.external`，本机没装时跳过；
- 整体测试在 `tests/whole/`：用示例工作区与录制的模型输出，从采集跑到发布；共用的样例在 `tests/fixtures/`；
- 修复缺陷时先写一个能复现它的失败测试。

```sh
.venv/bin/python -m pytest                     # 全部
.venv/bin/python -m pytest -m "not external"   # 不跑依赖外部工具的
.venv/bin/python -m pytest tests/collect       # 只跑一个阶段
```

改动做完后整体跑一次全部测试，不一小块一小块地测。

## 生成的文档

下面这些由程序生成，不要手改；改了对应的来源后重新生成：

| 文件 | 来源 | 命令 |
|---|---|---|
| `docs/reference/commands.md`、`configuration.md`、`handoff.md`、`methods.md` | 命令树与文案表、`settings/defaults.json`、各步骤的 `*.schema.json`、方法清单 `<方法>.yaml` | `.venv/bin/python -m tightrein.cli.reference` |
| `src/tightrein/protocol/README.md` | `settings/defaults.json` | `.venv/bin/python -m tightrein.protocol.overview` |

## 约定与提交

- 代码注释与文档用中文，标识符用英文；中文与英文、数字相邻时加一个空格；风格与周围代码保持一致；
- 改动保持聚焦，不把重构与行为变更混在一起；每个行为变更都要新增或更新测试；
- 提交信息遵循 [Conventional Commits](https://www.conventionalcommits.org/zh-hans/)，例如 `fix(collect): 读满上限时读取位置停在最后一条`；
- 提交信息与 PR 描述不带任何 AI 署名(不加 `Co-Authored-By` 之类的 AI 协作者行，也不加「由某某工具生成」)：与 tightrein 给被管理项目提交时的规则一致(`src/tightrein/protocol/git.md`)；
- PR 按模板说明：解决什么问题、为什么这样改、如何验证、有哪些局限；CI 通过且评审没有问题后合并。

## 许可

提交贡献即表示你同意以 [MIT 许可证](LICENSE)发布你的贡献。
