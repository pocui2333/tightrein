---
name: aggregate
description: 把 collect 采集的信号归并成问题、更新问题状态并挑出需要分诊的问题，也用于忽略、判为误报、合并、重新打开问题。采集完成后、需要知道有哪些新问题或回归时使用；聚合是确定性的，不调用模型。
---

# aggregate

处理全部尚未聚合的 collect 运行：规范化、抑制、按指纹归并、复现确认、状态更新，最后写交接文档。
来自错误追踪与监控平台的信号直接用平台的分组编号(如 `sentry:<组织>/<编号>`)作指纹；其余来源按逻辑位置计算指纹。
问题只有四种状态：新发现、持续、已解决、回归；复现确认之前为待确认，另有用户的处置(已忽略、误报、已并入其他问题)。

## 命令

```
tightrein aggregate [--select run:<运行编号>|probe:<方法>] [--input <collect 交接文档>] [--output <目录>] [--dry-run]
                     [--reproduce live|skip] [--rebuild] [--no-wait] [--target <地址>] [--now <时间>]
tightrein problem ignore <问题> --reason <原因> [--until <条件>]
tightrein problem false-positive <问题> --reason <原因> [--expires <日期>]
tightrein problem merge <问题A> <问题B>
tightrein problem reopen <问题>
```

- 输出「没有新信号」表示没有待聚合的运行，也没有需要重试的待确认问题，这不是错误。
- 复现确认：api-fuzz 的服务器报错重放请求确认，其余首次观察即有效。重放未复现的问题保持待确认并标记间歇，之后再出现时转为新发现。
- `--reproduce skip` 不重放请求，需要重放确认的问题保持「待确认」，下次聚合再确认。
- `--rebuild` 只在指纹规则变化或重新解析了已聚合的运行之后使用；它先备份数据库再重建全部问题，已有的问题编号会被继承。
- 全局锁被占用时缺省排队，`--no-wait` 立即退出。

## 解读结果

运行级交接文档 `data/runs/<本次运行编号>/handoff/aggregate-<本次运行编号>.json`：

- `nextAction` 为「交给 triage」时，对 `outputs.forTriage` 中的每个问题运行分诊，问题级交接文档在各项的 `handoff` 中；为「无需分诊」时到此结束。
- `outputs.counts`：本次转为新发现、回归、已解决、持续的数量，以及仍待确认的问题数。
- `outputs.reopenedIssues`：因问题回归而重新打开的 Issue。
- `outputs.notes`：例如 commit 先后关系未知、某些判定本次没有做。
- `status` 为 `failed` 时看 `blockedReason`：失败运行之前的运行已经落库，修正原因后重跑即可。

问题级交接文档 `aggregate-<问题编号>.json` 是分诊的输入：问题、本次的转换、最近一条信号、样本、复现确认结果；回归时 `issueId` 为已有的 Issue。

## 约束

聚合的结论只能通过上面的命令改变，不直接编辑数据库、`suppressions.yaml` 或问题记录。
