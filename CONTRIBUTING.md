# 参与贡献

感谢你对 tightrein 的关注。欢迎提交缺陷报告、功能建议、文档改进与代码。

## 先讨论，再动手

- **缺陷**：先搜索[已有 Issue](https://github.com/pocui2333/tightrein/issues)，没有重复时用「缺陷报告」模板新建，附上复现步骤与 `tightrein --version` 的输出。
- **新功能或较大的改动**：先开 Issue 说明动机与设想，达成一致后再提交 PR，避免白费功夫。
- **小改动**(错别字、文档措辞、明显的小缺陷)：可以直接提交 PR。
- **安全漏洞**：不要公开提交，按 [SECURITY.md](SECURITY.md) 私下报告。

## 开发环境

需要 macOS、Python 3.12+ 与 git。

```sh
git clone https://github.com/pocui2333/tightrein.git
cd tightrein
python3 -m venv core/.venv
core/.venv/bin/pip install -e 'core[dev]'
```

## 测试

```sh
cd core
.venv/bin/python -m pytest -m "not slow"        # 快速测试，每次提交前运行
.venv/bin/python -m pytest                      # 全部测试，含真实 git 仓库与子进程
.venv/bin/python dev/affected_tests.py --run    # 只运行受改动影响的测试
```

- 测试不调用真实的 agent 工具、钥匙串或外部服务。agent 的输出用 `core/tests/` 下录制的夹具回放，外部命令通过注入的假实现替代。
- 依赖真实 git 仓库、子进程或耗时较长的测试标记为 `slow`。
- 修复缺陷时先写一个能复现它的失败测试，这也是 tightrein 自身对 agent 的要求。

## 代码结构

```
core/tightrein/
  cli/            命令行入口与本地 MCP 服务，只做参数转换与结果展示
  orchestrator/   编排：时间表、一次运行的步骤、中断恢复、运行摘要与收件箱
  pipeline/       流水线各环节(collect、aggregate、triage、issue、fix、verify、release、learn)
  sources/        各采集来源的执行，把结果转换为信号
  runner/         以统一的任务与结果调用 agent 工具，保证输出格式、时间、轮数与预算
  guards/         agent 运行前后的边界检查：凭证、只读锁定、git 状态、文件快照、改动规则
  extensions/     扩展点宿主：技术栈与项目扩展的解析、调用与校验
  retrieval/      知识检索：经验与规则的索引、检索与预取
  evaluation/     评测：用例、沙箱运行、评分与版本比较
  store/          SQLite 数据库、迁移、仓储与工作区文件
  vcs/            git 与 gh 的只读查询与经确认的写操作
  config/         配置的读取、合并与校验
  contracts/      交接文档与数据文件的 JSON schema
  domain/         实体、状态机与纯函数，不做 IO
  observability/  事件日志、trace、脱敏与通知
  monitor/        终端实时界面(tightrein watch)
  packaging/      skills 的安装、第三方 skill 锁定与定时任务
skills/           各环节交给 agent 的指令(Agent Skills 格式)
docs/             文档，按 Diátaxis 分为 tutorials、how-to、reference、explanation
```

设计决定与取舍记录在 [docs/explanation/](docs/explanation/README.md)，`redesign/` 为当前设计。代码中的注释常以「architecture/02 3.4」这样的形式引用对应章节。

## 约定

- 改动保持聚焦，不把重构与行为变更混在一起。
- 每个行为变更都要新增或更新测试。
- 行为变化时同步更新 `docs/explanation/` 中对应的设计文档。
- 修改 `core/tightrein/contracts/schemas/` 下的 JSON schema 后，重新生成参考文档：

  ```sh
  cd core && .venv/bin/python dev/contract_reference.py handoff    # 或 probe、methods
  ```

- 代码注释与文档使用中文，标识符使用英文；风格与周围代码保持一致。

## 提交 PR

1. 从 `main` 新建分支，提交信息遵循 [Conventional Commits](https://www.conventionalcommits.org/zh-hans/)，例如 `fix: 修复计划超出上限时未拆分`。
2. 确认 `pytest -m "not slow"` 通过；改动涉及 git、子进程或文件系统时运行全部测试。
3. 按 PR 模板说明：解决什么问题、为什么这样改、如何验证、有哪些局限。
4. CI 通过且评审没有问题后合并。

## 许可

提交贡献即表示你同意以 [MIT 许可证](LICENSE)发布你的贡献。
