# 代码规范(coding)

## 是什么

tightrein 运行时给目标项目写代码(实施·编码)的通用最低要求。项目自己的规范以项目的规则文件(`CLAUDE.md`、`AGENTS.md`、`CONTRIBUTING.md` 等)与 lint 为准，不写进 tightrein。tightrein 自己的代码规范在仓库的 `CONTRIBUTING.md`，不在这里。

## 怎么做

- **最小改动**：只改根治这个问题所需的代码，不顺带重构、不改格式、不动无关文件；
- **沿用原有写法**：命名、错误处理、日志、测试的写法照项目已有的做；能复用的公共实现直接用，不另写一份；
- **不加新依赖与新框架**：确实需要时停下，走人工关卡(新增依赖属于规范要求用户确认的事项)；
- **不为假想需求加抽象**：不加没有调用方的参数、开关与扩展点；
- **不写特判**：不针对复现输入或测试数据写死分支，修通用逻辑；
- **先写能复现的失败测试**：修缺陷时先有一个失败的测试，再改代码让它通过；不删、不跳过已有测试；
- **不留调试残留**：不留打印、注释掉的代码、临时文件；
- **不带 AI 署名**：提交与 PR 中不写 Co-Authored-By 之类的署名(见 `git.md`)。

确定性的部分由程序检查(改动量、计划外文件、受保护文件、跳过标记、调试残留、疑似写死的字面量，见 `boundaries.md`)，其余由审查判断。

## 在哪配置

规则本身不可配置。项目的检查命令(`test`、`lint`、`build`、`typecheck`)在工作区 `settings.json` 的 `project.commands`；改动量上限在 `boundaries.changeCap`。

## 缺省值

| 项 | 缺省 |
|---|---|
| 改动量硬上限 | 10 个文件、400 行(`boundaries.changeCap`) |

## 设计依据

- **最小改动、沿用原有写法**：改动越小越好审、越不容易引入新问题；评审者读懂一次改动的能力在 200 到 400 行之后明显下降(SmartBear/Cisco：smartbear.com/learn/code-review/best-practices-for-peer-code-review；Google Small CLs：google.github.io/eng-practices/review/developer/small-cls.html)。
- **项目规范以项目为准**：tightrein 管理多个项目，各自的约定不同，只在 tightrein 中定所有项目共有的底线(44 号计划「代码规范」)。
- **不写特判**：旧审查角色专门检查 `hardcode`(`skills/fix/references/roles/fix-reviewer.md`)，这是模型最常见的「让测试通过」的捷径。
