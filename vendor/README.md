# vendor：引用的第三方 skills

## 是什么

tightrein 在个别调用点引用的第三方 skills，统一放在这里，不分散到各模块：

| 路径 | 内容 |
|---|---|
| `lock.json` | 锁定清单：每个 skill 的来源仓库、完整 commit、在来源仓库中的路径、许可证、目录哈希、逐文件 sha256、锁定日期 |
| `skills/<名字>/` | 锁定版本的原样副本，不做任何修改 |
| `licenses/` | 来源仓库的许可证原文(副本只取 skill 目录，许可证在仓库根，单独保留) |

校验与更新的程序在 `src/tightrein/protocol/vendor.py`。

## 当前引入的

| 名字 | 来源 | commit | 许可证 | 用在 |
|---|---|---|---|---|
| differential-review | https://github.com/trailofbits/skills | `123037ec8aed` | CC-BY-SA-4.0 | 静态巡检(初筛)、审查 |
| variant-analysis | 同上 | 同上 | CC-BY-SA-4.0 | 静态巡检(同类问题) |
| sharp-edges | 同上 | 同上 | CC-BY-SA-4.0 | 静态巡检(初筛、基线) |

「用在」取自旧代码的引用处(`runner/roles.py`、`pipeline/collect/prompts/tasks.py`、`pipeline/triage/prompts/claim_verifier.py`)，
以各模块方法清单中实际写的为准。

许可证原文：`licenses/trailofbits-skills.LICENSE`(CC-BY-SA-4.0：署名、相同方式共享；副本原样保留，不改写内容)。

## 流程

- **加载**：模块在自己的方法清单中按名字写用到哪个 skill；程序到了那个调用点才调用 `vendor.load(tool, 名字)`：
  先逐文件核对 `lock.json`，任一文件不符(内容改动、多出、缺少)即不加载，报出文件与期望、实际哈希。
- **补齐副本**：`ensure_present`：副本缺失时按锁定的 commit 下载源码归档、只解出该 skill 目录并核对；
  刚下载的与清单不符立即删除；已有副本被改过时不重新下载覆盖，报出差异等人来看。
- **更新**：`update(skill, commit, …)`：先核对现有副本没被改过，再把新 commit 的内容解到临时目录、记下逐文件哈希后替换副本，
  返回文件的新增、删除与改动，确认后由调用方写回 `lock.json`。

## 引入与更新的规则

1. 只引入来源清楚、许可证允许再分发的 skill；在上表登记名字、来源、许可证与用途。
2. 锁定到完整的 40 位 commit，不锁分支或标签；副本只取该 skill 的目录。
3. 副本原样保留，不在这里改；需要不同的写法时，在 tightrein 自己的提示模板(`src/tightrein/prompts/`)里写。
4. 更新时先看 `update` 列出的文件变化，读过改动的内容(安全审核：有没有新增脚本、外部地址、要求执行命令的内容)，
   确认后才写回 `lock.json` 并提交。
5. 不再使用的 skill 从 `lock.json` 与 `skills/` 中一并删除。

## 设计依据

- **放在一处**：同一个 skill 常被多个模块用(例如 differential-review 评估与审查都用)，分散放要各存一份；
  锁定、许可证、哈希、安全审核与更新放在一处只做一次(44 号计划「外部 skills(vendor)」)。
- **逐文件哈希，目录哈希不计权限与 .git**：只关心内容，拷贝、检出带来的权限与时间变化不应让校验失败；
  按相对路径排序后拼接求哈希，与文件遍历顺序无关(旧 `packaging/third_party.py:file_hashes`、`tree_hash`)。
- **下载不执行 git、只解出 skill 目录、先解到 `.partial`**：源码归档按 commit 取，内容确定；解包时逐段校验路径，
  防止归档里的 `..` 写到目录之外；中途失败不留半个目录(旧 `packaging/third_party.py:extract`、`ensure_cached`)。
- **被改过的副本不覆盖**：副本与清单不符可能是有人有意改过，自动覆盖会把问题藏起来。
- **JSON 而不是 YAML**：与 settings 的格式一致(原 `third_party/skills.lock.yaml`)。

## 不做什么

- 不整体加载全部 skills，只在调用点按名字加载一个；
- 不自动跟进来源仓库的新版本，更新必须由人确认；
- tightrein 自己的 skills 装进了哪些 agent 工具不在这里，记在本机状态 `local/installed.json`。
