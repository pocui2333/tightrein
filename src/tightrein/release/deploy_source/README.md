# 部署来源(release/deploy_source)

## 是什么

发布跟踪部署时读取「最近的部署」的方法库。每种来源一个程序加一个同名清单(`<方法>.yaml`)；接入清单 `setup.json` 的 `release.deploy` 的 `method` 写方法名。

| 方法 | 读什么 | 凭据 |
|---|---|---|
| `github_actions` | 部署工作流的运行(`gh run list`) | gh 的登录 |
| `github_deployments` | GitHub Deployments 与各自最新的状态(`gh api`) | gh 的登录 |
| `vercel` | Vercel 的部署列表(REST API `/v6/deployments`) | secrets.json 的 `vercel.token` |

## 流程

`release/deploy.py:recent` 按 `method` 经 `collect/common/methods` 加载方法 → 合并 `controls."release.deploy".<方法>` 的参数并按清单的 `optionsSchema` 校验、取清单写明的凭据 → 调方法的 `read(configured, context)` → 得到按 (时间, 编号) 排序的 `DeployRecord`。

## 输入与输出

- 输入：`Configured`(参数与令牌)、`SourceContext`(只读 gh、HTTP、项目主干分支、HTTP 时限)；
- 输出：`list[DeployRecord]`，状态为 running、succeeded、failed、skipped；读取失败抛 `DeployError`(tool_missing、unavailable、invalid、misconfigured)。

## 配置

参数在 `settings/defaults.json` 的 `controls."release.deploy".<方法>`，各项含义见同名清单；清单只声明参数、适用条件、凭据条目名与限制，不写取值。

## 设计依据

- 与采集的平台方法同一写法(`collect/platform_errors/error_tracking/` 等)：接新平台只加一对文件，不改调用方；参数在清单里一眼看全，必填参数没写(null)时按清单报缺少，不把 None 传给 gh 或平台。
- gh 只读、以参数数组启动；错误只带标准错误的最后 3 行；令牌只放进请求头。
- GitHub Actions 中 skipped 的运行不是失败：同一窗口内多个提交由之后的一次运行统一上线，由发布找之后包含该提交的运行。

## 不做什么

- 不写平台(不触发、不回滚部署)；
- 不判断部署失败是否由修复引起(交人判断，见 release/README.md)。
