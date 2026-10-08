# 访问日志的项目数据源(方法文档)

访问日志不在日志平台里、而在项目自己的日志文件或数据库里时，项目按这里的方法文档写一个取数据的脚本，tightrein 定时
调用它，后面的解析、统计、基线比较与日志平台取法是同一套程序。方法文档的格式同 `collect/project_probes/TEMPLATE.md`。

| 方法 | 数据源 | 脚本交出 |
|---|---|---|
| [01 读项目自己的日志文件](01-log-file.md) | nginx 等写的 access.log | 访问日志原文的行(`lines`) |
| [02 查项目自己的数据库](02-database.md) | 访问记录表 | 已解析的请求(`requests`) |

## 共同的约定

- 接入清单 `setup.json` 中 `collect.access_log` 写 `custom`，`script` 为脚本路径(相对工作区，`.py` 用 tightrein 的解释器
  运行，其他直接执行)，`guide` 指向所用的方法文档，需要凭据时 `secrets` 写 secrets.json 中的条目名；
- 输入(标准输入的 JSON)：`window.since`、`window.until`(UTC，`[since, until)`)、`limit`(条数上限)、`workspace`；
- 输出(标准输出的 JSON)：按 `output.schema.json`，`lines` 与 `requests` 二选一，加 `truncated`(是否因上限只交回一部分)
  与可选的 `notes`；不合格时本次作废、读取位置与基线都不前进；
- 只读：只读文件、只用只读账号查库；脚本失败(退出码非 0、超时)同样作废，下次从原位置重读。
