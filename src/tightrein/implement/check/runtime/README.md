# 本机运行检查(implement.check.runtime)

## 是什么

原验证阶段的 PR 前检查并入自检：本机启动改动后的服务，只对改动涉及的接口做 5xx 模糊测试的浅跑，只对改动涉及的页面做巡检与截图审查。接入清单(`setup.json`)启用了 `implement.check.runtime` 且改动涉及接口或页面时才做。

## 流程

1. 受影响的接口与页面(`api.py`、`pages.py`)：方案交接的 `affectedEndpoints`、`affectedPages`，加上项目端点清单与路由清单中源文件属于改动文件的项；都没有就不启动。
2. 启动计划(`local_run.py`)：项目启动脚本(`setup.json` 的 `script`)经标准输入收到 `{"worktree","mode","ports"}`，输出 `launch.schema.json` 格式的计划。
3. 迁移(`migration.py`)：改动匹配计划中的 `migrationPaths` 时停下等用户确认，迁移文件哈希为前置条件。
4. 启动(`local_run.py`)：对象锁 → 端口检查(`ports.py`) → 按顺序启动并判断就绪 → 写 services.json；page 模式端口被占时退回 api 模式。
5. 接口浅跑(`api.py`，Schemathesis) → 页面巡检(`page_runner.py`，Playwright，Node 侧在 `playwright/`) → 截图(`screenshots.py`)。
6. 退出时停掉全部服务、释放锁。

## 输入与输出

输出为 `verdict.Item` 列表，三档结论加失败：

| 结果 | 含义 | 阻断 |
|---|---|---|
| passed | 执行了，有证据 | 否 |
| weak | 执行了，但前置条件不满足或用例因数据缺失跳过 | 否，写进报告 |
| unverified | 没有执行，写明本应验证的行为与原因 | 否，写进报告 |
| failed | 执行了但不满足 | 是(局部问题) |

原始输出在 `36-implement.check.r<轮>-raw/runtime/`(api、pages、服务日志)；遗留进程记录在 `data/local_run/services.json`；截图参照在 `data/cache/screenshots/`。

## 配置

`controls.implement.check.runtime`：`ports`(各模式的端口，由项目在 settings.json 的 overrides 中给出)、就绪与停止的时限、`api`(接口描述 `spec`、端点清单 `endpoints`、浅跑参数、凭证请求头)、`pages`(角色、语言、重试、时限、`patrolGrep`、`ignoreRequests`、路由清单 `routes`)、`screenshots`(像素比对的阈值与通道容差)。

凭据在工作区 `secrets.json`：接口令牌 `api.token`；页面账号 `pages.<角色>.account`、`pages.<角色>.password`。启动脚本需要的凭据按 `setup.json` 的 `secrets` 登记，只注入这几项。

Playwright 首次使用前在 `playwright/` 下执行 `npm ci` 与 `npx playwright install chromium`。

## 设计依据

- **同一 commit 只启动一次**：接口、页面、截图共用一次启动(44 号计划「验收与本机检查的优化」)。
- **就绪同时匹配成功与失败信号，成功后再请求 readyUrl**：只等成功信号时进程崩溃会静默等到超时(旧 `local_run.py:_wait`、`skills/verify/references/local-run.md`)。
- **启动失败重试 1 次**：limits.md「测试环境问题(端口、启动失败)就地重试 1 次」，取代旧设计的不重试。
- **端口只终止本工具的遗留进程**：进程号与命令行都与上次记录一致才算；其他程序占用的不动，检查一律用本工具启动的服务(旧 `ports.py`)。
- **迁移只从 diff 判断**：不连数据库、不读配置与凭证；迁移文件变了要重新确认(旧 `migration.py`)。
- **接口浅跑与基准比对**：改动前已存在的问题只列在说明中，只有新出现的计失败(旧 `regression.py:shallow`)。
- **页面只计受影响页面上的失败**；控制台报错与失败请求不论用例是否通过都记，同一用例、页面、原文只记一次(旧 `failures.py`)；用例文件按完整路径段匹配(73f1058)。
- **页面运行的凭据保护**：密码只经环境变量，登录态放 0700 临时目录用完即删，trace 中的凭证改写为已脱敏且每行仍是合法 JSON(旧 `pages/runner.py`、`artifacts.py`)。
- **截图先像素比对**：截图查看是原验证中唯一调用模型的地方；与基准或上一轮看过的截图一致就不再交给模型(44 号计划)。截图必须有人看：模型给不出结论时交用户。
- **弱证据写成通过等同伪造**(`local-run.md` 三档结论)。

页面用例的编写说明(写给项目，放在工作区 `e2e/README`)：路由从路由清单读，不猜地址(猜错往往是一张没报错的空页)；优先点导航进入；同名按钮限定所在容器；另外监听未捕获的页面异常；加载跳变按固定间隔采样中间状态；需要在测试库造真实数据时停下问用户。

## 不做什么

- 不含任何项目或技术栈的启动知识(由项目启动脚本给出)；
- 不跑整站巡检，只查改动涉及的接口与页面；
- 不连数据库、不复用用户正在跑的服务；
- 不做部署后的确认(在 `release/accept/`)。
