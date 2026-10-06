# 参考

查字段、参数与命令。契约参考(扩展点、探针、交接文档等)由 JSON schema 自动生成，不手写；改了 schema 后重新生成：

```
cd core
.venv/bin/python dev/contract_reference.py handoff   # 或 probe、methods
```

| 参考 | 内容 |
|---|---|
| [cli.md](cli.md) | 全部命令：日常、分组与单步执行 |
| [handoff-documents.md](handoff-documents.md) | 交接文档：头信息字段、各类型的「内容」小节与数据块字段(生成) |
| [project-probe.md](project-probe.md) | 项目探针的输入与输出 JSON(生成) |
| [configuration.md](configuration.md) | 配置与运行说明：各功能的启用条件、配置写法与运行行为 |
| [methods.md](methods.md) | 方法目录：各扩展点的核心方法、参数、输出、限制与示例配置(由方法清单生成)，以及后续平台 |
