# 写项目探针

项目探针是采集阶段留给项目的扩展口：只有项目自己知道的业务异常(定时任务没按时跑完、处理量为零、按项目日志规范才看得出的问题)，由项目写一个只读的检查脚本，tightrein 按登记定时调用、校验输出、转成信号，之后与内置来源走同一套去重与评估。

怎么写不在这里重复：tightrein 为每一种探针方法备了一份统一格式的方法文档，都在方法库 `src/tightrein/collect/project_probes/` 里。本文只说按什么顺序用它。

## 1. 选方法

打开 `src/tightrein/collect/project_probes/README.md` 的「选用指南」，逐行对照项目现状，判断需不需要、能不能实现：

| 方法 | 发现什么 | 项目要具备 |
|---|---|---|
| `01-run-records.md` 读取运行记录 | 定时流程跑了但失败或带警告 | 流程在本机写 JSON Lines 运行记录 |
| `02-job-status.md` 检查定时任务与业务状态 | 任务没按时完成、处理量为零 | 只读状态接口(或只读数据库账号)与只读凭据 |
| `03-log-rules.md` 按项目日志规范统计 | 权限校验失败突增、catch 后只记日志的异常 | 日志进了 Loki 且有规范字段 |

都不适用时，按 `TEMPLATE.md` 写一份新方法，加进方法库，编号递增(见下面「加一份新方法」)。

## 2. 照方法文档写脚本

每份方法文档都有同样的十一个小节(介绍、实现方式、运行模式、架构设计、代码排版、输入格式、输出格式、输出文档、交接内容、选用条件、去重)，照着写：

- 脚本放在工作区 `scripts/` 下，一个探针一个文件；
- 输入、输出的契约在方法库 README 的「契约」，输出按 `output.schema.json` 校验(字段表也在 [交接格式](../reference/handoff.md))；
- 用 tightrein 的解释器运行时可以直接用辅助函数：

  ```python
  from tightrein.collect.project_probes import helpers

  found = helpers.read_input()                 # 标准输入的 JSON：name、lastRunAt、state、window、workspace、environment、baseUrl
  token = helpers.secret("demo.readonly")      # 只读得到登记过的凭据，读到即登记进脱敏
  signals = [helpers.signal("job:daily-import", "昨天的导入没有在 02:00 前完成",
                            ["最近一次完成 2026-09-30T01:58:00Z"], "missed-run:daily-import", severity_hint="P1")]
  helpers.emit(signals, state={"lastBatch": "2026-10-01"})   # 校验后写到标准输出，不合格抛 ValueError
  ```

要点(`TEMPLATE.md`「写法要点」)：

- 只读：调只读接口、读日志平台、读项目自己写的记录文件；不改数据、不登录服务器；
- `fingerprint` 只由「检查项:对象」组成，不带时间、数量、随机值；
- `evidence` 写可核对的事实，不写凭据与个人信息，写出前经 `helpers.redact`；
- 失败就让脚本失败(退出码非 0)：本次作废、状态不保存、下次到期重试；不要吞掉错误输出空结果。

## 3. 登记

在工作区 `settings.json` 的 `overrides` 下登记：

```json
{
  "overrides": {
    "controls": {
      "collect.project_probes": {
        "probes": [
          {"name": "daily-import", "command": ["{python}", "scripts/daily_import.py"], "every": "1h",
           "secrets": ["demo.readonly"], "timeout": "2m"}
        ]
      }
    }
  }
}
```

- `{python}` 是 tightrein 自己的解释器；
- `secrets` 是 `secrets.json` 中的条目名，只有登记的能读到(经环境变量 `TIGHTREIN_SECRET_<条目名>`)；
- `timeout` 省略时取 `controls."collect.project_probes".probeTimeout`；第一次运行回看 `lookback`；
- 被测地址取 `sites.json` 的 `target.baseUrl`、`target.environment`，作为输入交给探针。

`setup.json` 中 `collect.project_probes` 写 `enabled`，`method` 为 null。

## 4. 试跑

```sh
tightrein project check collect.project_probes
```

试跑调用每个登记的探针一次，只校验输出、列出会产出的信号，不保存任何东西。不合格时列出每条错误的路径。

## 加一份新方法

现有方法都不适用、而这类探针以后别的项目也会用到时，把它写成方法库里的一份新方法：

1. 复制 `TEMPLATE.md` 的十一个小节，写成 `src/tightrein/collect/project_probes/NN-<名称>.md`(编号递增)；每个小节以模板表中列出的那处约定为准，不自行发明字段；
2. 在方法库 README 的「对照表」加一行：发现什么、项目要具备什么、不具备时怎么办；
3. 参考代码写在文档的「代码排版」里，由项目照着写；方法库不放可执行的通用实现(每个项目的数据源、字段、阈值都不同)。
