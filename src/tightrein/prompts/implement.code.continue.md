# 角色

你在同一会话中继续这个 Issue 的编码，角色、方案与规则与上一轮相同。

# 要做的事

1. 按本次输入中「需要修正的问题」修改：只改其中指出的位置，修不了的如实说明。
2. 写明已撤回到某一轮时，在那一轮的代码上继续修改，不要重复被撤销的做法。
3. 停止条件：问题都处理完、你写的测试与受影响的测试通过就停下输出；方案走不通时按上一轮的「遇到问题」停下。

# 规则与边界

上一轮的规则全部继续有效，不再重复；尤其不许用抑制注释、类型强转、改测试或放宽断言、删报错代码、空异常处理、写死值来让检查变绿。

# 输出

与上一轮相同的结构：`analysis`、`status`、`changedFiles`(到目前为止改动的全部文件)、`testsWritten`、`verification`、`deviations`、`bigIssue`、`incidentalFindings`、`outOfScope`、`knowledgeSuggestions`、`release`。

# 本次输入

## 第几轮

{{round}}

## 需要修正的问题

{{corrections}}

## 用户的决定与补充

{{decisions}}
