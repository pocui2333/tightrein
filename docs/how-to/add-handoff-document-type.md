# 如何新增一种交接文档类型

## 目标

新增一种交接文档类型(例如 `incident`)：程序能按它的模板渲染、`tightrein admin doc check` 能校验它，契约参考中列出它的小节与数据块。

## 前提

- 已读过 [交接文档](../explanation/redesign/00-handoff-documents.md)，确认现有八种类型(task、result、finding、decision、plan、review、progress、issue)都不合用。
- 已确定新类型「内容」部分的固定小节(按顺序，全部必需)，以及其中哪些数据需要程序精确读取(放进数据块)。
- 本工具仓库的虚拟环境可用：在仓库根目录 `cd core && .venv/bin/python -V` 输出 Python 3.12。

## 步骤

1. **给新小节补三语标题。** 在 `core/tightrein/domain/handoff/types.py` 的 `HEADINGS` 中，为每个尚未出现的小节键加上 zh、en、ja 三语标题。已有的键(例如 `evidence`、`acceptance`)直接复用，不另起同义的键。

   ```python
   # incident
   "timeline": {"zh": "时间线", "en": "Timeline", "ja": "時系列"},
   ```

   预期结果：同一类型内不同的键没有相同的标题(与基础小节同名时只能用于三级标题，例如 review 的「结论」)。

2. **登记类型。** 在同一文件的 `TYPES` 中加一项：`DocumentType(<类型>, <用途>, <小节键元组>, <数据块标签到所在小节键>, <必需的数据块>)`。

   ```python
   DocumentType("incident", "线上事故的记录", ("symptom", "timeline", "impact"),
                {"events": "timeline"}, frozenset({"events"})),
   ```

3. **定义数据块的 schema。** 新建 `core/tightrein/contracts/schemas/handoff/types/<类型>.schema.json`，`$id` 为 `https://tightrein.local/schemas/handoff/types/<类型>.schema.json`，每个数据块标签是 `$defs` 中的一项；没有数据块时 `$defs` 为 `{}`。每个字段写 `description` 与 `examples`，契约参考由它们生成。

4. **登记 schema 的版本。** 在 `core/tightrein/contracts/versions.py` 末尾加 `REGISTRY.declare("handoff/types/<类型>.schema.json", 1)`。

5. **重新生成契约参考。**

   ```
   cd core
   .venv/bin/python dev/contract_reference.py handoff
   ```

   预期结果：输出 `已写入 .../docs/reference/handoff-documents.md`，文件中出现新类型的小节表与数据块字段表。

## 验证

```
cd core
.venv/bin/python -m pytest tests/unit/domain/test_handoff.py tests/unit/store/test_documents.py tests/unit/test_contract_reference.py -p no:cacheprovider
```

预期结果：全部通过。这几个测试核对每个小节键都有三语标题、注册表与 schema 文件中的数据块一一对应、契约参考与 schema 一致。

再用程序渲染一份新类型的样例(`store/files/documents.write`)，执行 `tightrein admin doc check <文件>`，预期输出「通过」。

## 常见问题

- **测试提示注册表与 schema 不一致**：`TYPES` 中的数据块标签与 schema 的 `$defs` 键要完全相同，没有数据块的类型也要有 schema 文件。
- **`test_contract_reference.py` 失败，提示文件已过期**：改了类型注册或 schema 之后没有重新生成，执行第 5 步。
- **`admin doc check` 报「缺少「内容」小节」**：标题必须与 `HEADINGS` 中的某种语言完全相同，且使用三级标题(`### `)。
- **数据块没有被识别**：代码块的信息串必须是 `yaml data:<标签>`，标签与注册时相同。
