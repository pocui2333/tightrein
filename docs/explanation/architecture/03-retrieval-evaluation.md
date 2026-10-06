# 能力层：retrieval、evaluation

本篇展开能力层中的两个组件：`retrieval` 负责知识的索引、检索与写入去重，为 agent 任务组装上下文；`evaluation` 负责在固定评测集上以沙箱模式运行模块、打分并比较版本、工具与模型。两者都不含项目知识，编号、枚举、表、路径与配置沿用 01 的定义。

业务规则见 `docs/explanation/design/16-retrieval.md`(检索)、`12-agent-scoring.md`(评分只作衡量)、`14-improve.md`(改进建议的评测对比)、`15-standalone-run.md`(沙箱模式与回放)。

## 1. retrieval

### 1.1 职责

| 负责 | 不负责 |
|---|---|
| 扫描知识目录与各类人读文档的 frontmatter，校验后同步进 `knowledge_meta` 与 `knowledge_fts` | 被测项目代码的检索(由 agent 用 `rg`、glob 按需探索，16.2) |
| 生成 `INDEX.md` | 决定一条知识是否需要用户确认才能写入(由调用方 `learn` 决定，8.5) |
| `search`、`get`、`related`、`stale` 四个操作与命中计数 | 判断条目之间是否矛盾(由 `learn` 经执行器完成，本组件只给出候选组) |
| 按任务类型预取条目，组装上下文(16.6) | 调用 LLM 做写入判断以外的任何事 |
| 写入去重：检索相似条目、经执行器取得判断、校验后执行写入 | |
| 命令行 `tightrein admin kb` 与本地 MCP 服务共用的实现 | |
| 检索评测(recall@5、recall@10、MRR) | |

### 1.2 索引范围

| 来源 | 路径(均由 `store/files/layout.py` 给出) | `knowledge_meta.type` | frontmatter 校验 |
|---|---|---|---|
| 知识条目 | `knowledge/<类型>/<编号>-<简称>.md` | `KnowledgeType` 的六个取值 | `data/knowledge.schema.json` |
| Issue | `issues/<编号>-<简称>.md` | `issue` | `handoff/frontmatter/issue.schema.json` |
| 发现报告 | `data/findings/<问题编号>.md` | `finding` | `handoff/frontmatter/report.schema.json` |
| 修复报告 | `data/fixes/<Issue 编号>/report.md` | `fix-report` | `handoff/frontmatter/report.schema.json` |

- 前一类称为「条目」，参与命中淘汰、复核、去重与 `INDEX.md`；后三类称为「文档」，只参与检索与预取，它们的业务状态仍以各自的表(`issues`、`triage_results`)为准，在 `knowledge_meta` 中的 `status` 固定为 `active`。
- 运行摘要、周报、验证报告不建索引：前两者是其他文档的汇总，后者的结论已写入修复与 Issue 的状态。
- `INDEX.md` 自身不建索引。

**各字段的取值来源**

| 索引字段 | 条目 | Issue | 报告 |
|---|---|---|---|
| `id` | frontmatter `id` | frontmatter `id` | frontmatter `id` |
| 标题 | 正文第一个一级标题 | frontmatter `title` | 正文第一个一级标题 |
| 摘要 | `summary` | `title` | `summary` |
| 标签 | `tags` | 由 `rootCause` 生成 `path:` 标签，由关联问题的路由生成 `route:` 标签 | `tags` |
| 正文 | frontmatter 之后的全文 | 同左 | 同左 |

**标签约定**：标签是自由文本，其中以下前缀有固定含义，由校验规则检查格式，供预取按位置匹配：

| 前缀 | 含义 | 例子 |
|---|---|---|
| `path:` | 仓库内的相对路径或目录前缀 | `path:src/Services/Material/` |
| `route:` | HTTP 方法与路由模板 | `route:POST /api/Material/Query` |
| `page:` | 前端页面路由 | `page:/material/list` |
| `stage:` | 本工具的环节 | `stage:triage` |

1.6.4 中缺陷模式的「路由表」即由各缺陷模式条目的 `path:` 标签汇总而成，不另外维护。

### 1.3 文件划分

```
retrieval/
  service.py        KnowledgeService：四个操作、同步、写入、预取的统一入口；命令行与 MCP 都只调用它
  sources.py        索引来源表(1.2)：扫描哪些路径、按目录选择 frontmatter schema、字段取值规则
  frontmatter.py    读取 frontmatter(经 store/files/markdown.py)、按 schema 校验、标签前缀格式检查
  sync.py           全量与增量同步、引用完整性检查、索引器钩子的调用
  indexers.py       Indexer 接口与 FtsIndexer(写 knowledge_fts)
  text.py           索引与查询共用的文本预处理：CJK 逐字切分、空白规整
  query.py          查询串解析、FTS5 MATCH 表达式生成、列权重常量
  ranking.py        CandidateSource 接口、FtsSource、RRF 融合
  search.py         search、get、related 的实现与命中计数
  stale.py          待复核：过期、未命中、矛盾候选组
  index_md.py       INDEX.md 的生成与分页
  context.py        context_for(task)：按任务类型的预取规则
  dedup.py          写入去重：相似条目检索、组装判断任务、校验判断结果
  writer.py         知识文件的新增、改写、合并、状态变更；原子写入
  benchmark.py      检索评测：加载用例、计算 recall@k 与 MRR、与基线比较
  errors.py         本组件的异常类型
```

命令行与 MCP 两个入口属于入口层，放在 `cli/` 下，只做参数转换与结果展示：

```
cli/
  kb.py             tightrein admin kb 的各子命令
  kb_mcp.py         MCP 服务：把四个操作注册为 MCP 工具，stdio 方式运行
```

### 1.4 接口

**数据结构**

```python
@dataclass(frozen=True)
class SearchFilters:
    types: tuple[str, ...] = ()            # knowledge_meta.type 取值
    tags: tuple[str, ...] = ()             # 必须全部包含
    status: KnowledgeStatus | None = KnowledgeStatus.ACTIVE   # None 表示不限
    limit: int = 10                        # 1 到 50

@dataclass(frozen=True)
class SearchHit:
    id: str
    type: str
    summary: str
    path: str                              # 相对工作区的路径
    score: float                           # 越大越相关；单一来源时为 -bm25，融合时为 RRF 分

@dataclass(frozen=True)
class SearchResult:
    hits: list[SearchHit]
    index_warnings: list[str]              # 同步失败时非空，说明检索基于上一次成功的索引

@dataclass(frozen=True)
class EntryDocument:
    id: str
    frontmatter: dict[str, Any]
    body: str
    path: str

@dataclass(frozen=True)
class RelatedEntry:
    hit: SearchHit
    relation: Literal["related", "related-by", "superseded-by", "supersedes"]
    status: KnowledgeStatus

@dataclass(frozen=True)
class StaleReport:
    overdue: list[SearchHit]               # 过了 reviewBy
    unused: list[SearchHit]                # 超过 unusedDays 未被命中
    contradiction_groups: list[list[SearchHit]]   # 同类型且标签重叠、需要 agent 比对的条目组

@dataclass(frozen=True)
class SyncReport:
    added: list[str]
    updated: list[str]
    removed: list[str]
    errors: list[FrontmatterIssue]         # 非空时本次同步未写入任何变化
```

**KnowledgeService**

```python
class KnowledgeService:
    def __init__(self, workspace: Workspace, db: Database, clock: Clock,
                 events: EventWriter, sandbox: bool = False) -> None: ...

    def sync(self, full: bool = False) -> SyncReport: ...
    def search(self, query: str, filters: SearchFilters = SearchFilters()) -> SearchResult: ...
    def get(self, entry_id: str, record_hit: bool = True) -> EntryDocument: ...
    def related(self, entry_id: str) -> list[RelatedEntry]: ...
    def stale(self) -> StaleReport: ...
    def regenerate_index_files(self) -> list[str]: ...          # 返回实际改写的 INDEX 文件路径
    def context_for(self, task: ContextRequest) -> ContextBundle: ...
    def write(self, draft: KnowledgeDraft, runner: Runner) -> WriteOutcome: ...
    def set_status(self, entry_id: str, status: KnowledgeStatus,
                   superseded_by: str | None = None) -> None: ...
```

- `sandbox=True` 时(`--output` 模式，见 1.8)不同步、不记命中、不写入，只读已有索引。
- `write` 与 `set_status` 只由流水线模块调用，不对 agent 开放(命令行与 MCP 都不提供)。

**预取**

```python
class ContextKind(StrEnum):
    STATIC_REVIEW = "static-review"
    TRIAGE = "triage"
    FIX = "fix"

@dataclass(frozen=True)
class ContextRequest:
    kind: ContextKind
    paths: tuple[str, ...] = ()            # 改动文件、根因文件
    routes: tuple[str, ...] = ()           # 路由模板，含方法
    pages: tuple[str, ...] = ()
    keywords: str = ""                     # 问题或 Issue 的标题，用于补足检索
    exclude_ids: tuple[str, ...] = ()      # 任务对象自身，例如正在分诊的问题的发现报告

@dataclass(frozen=True)
class ContextItem:
    id: str
    type: str
    summary: str
    path: str
    reason: str                            # 命中依据，例如「path:src/.../Material/ 前缀匹配」

@dataclass(frozen=True)
class ContextBundle:
    items: list[ContextItem]               # 只含编号与摘要
    inline_documents: list[EntryDocument]  # 总量小而直接放全文的类别(16.6)
    estimated_tokens: int

    def render(self) -> str: ...           # 生成放进执行器任务说明的一段文本
```

**写入去重**

```python
@dataclass(frozen=True)
class KnowledgeDraft:
    type: KnowledgeType
    slug: str                              # 文件名中的简称，小写字母、数字与连字符
    title: str
    summary: str
    tags: tuple[str, ...]
    body: str
    review_by: date
    related: tuple[str, ...] = ()
    source_run_id: str | None = None

@dataclass(frozen=True)
class WriteOutcome:
    decision: KnowledgeWriteDecision       # add、update、merge、noop
    written_id: str | None                 # 新建或改写的条目编号；noop 时为空
    superseded_ids: list[str]
    reason: str
```

**检索评测**

```python
@dataclass(frozen=True)
class RetrievalMetrics:
    recall_at_5: float
    recall_at_10: float
    mrr: float
    case_count: int
    skipped_cases: list[tuple[str, str]]   # 用例编号与原因，例如期望条目已不存在

def run_benchmark(service: KnowledgeService, cases: list[RetrievalCase]) -> RetrievalBenchmarkReport: ...
def compare_benchmarks(baseline: RetrievalBenchmarkReport,
                       current: RetrievalBenchmarkReport) -> BenchmarkComparison: ...
```

**向量检索的接入点**

```python
class CandidateSource(Protocol):
    name: str
    def candidates(self, query: str, filters: SearchFilters, limit: int) -> list[RankedId]: ...

class Indexer(Protocol):
    def upsert(self, entry: IndexedEntry, conn: Connection) -> None: ...
    def remove(self, entry_id: str, conn: Connection) -> None: ...

def rrf_fuse(rankings: list[list[RankedId]], k: int = 60) -> list[RankedId]: ...
```

### 1.5 读写的数据

| 数据 | 读 | 写 |
|---|---|---|
| `knowledge_meta` | 全部操作 | `sync`(增删改行)、`get`(命中次数与时间) |
| `knowledge_fts` | `search`、`context_for`、`dedup` | `sync`(经 `FtsIndexer`) |
| `problems`、`problem_signals`、`signals` | `context_for`(取问题的路由与位置) | — |
| `issues`、`triage_results` | `context_for` | — |
| `sequences` | — | `writer` 分配新条目编号，名称为 `knowledge-<前缀>`，例如 `knowledge-DP` |
| `locks` | — | 同步与写入时持有对象锁 `knowledge` |
| `knowledge/**`、`issues/`、`data/findings/`、`data/fixes/*/report.md` | `sync`、`get` | 只有 `writer` 写 `knowledge/<类型>/` 下的条目 |
| `knowledge/INDEX.md`、`knowledge/<类型>/INDEX.md` | — | `index_md` |
| `evals/retrieval/cases.jsonl`、`evals/manifest.json` | `benchmark`(经 `evaluation.cases` 校验哈希) | — |
| `data/evals/<评测编号>/` | 基线比较 | `benchmark` 写报告 |
| `data/logs/events-<日期>.jsonl` | `admin kb queries` 汇总检索记录 | 每次 `search`、`get`、`sync`、写入一个事件 |

**knowledge_meta 的列**

| 列 | 说明 |
|---|---|
| `id` | 主键，条目或文档的编号 |
| `type` | 1.2 表中的类型 |
| `status` | `KnowledgeStatus`；文档固定为 `active` |
| `title`、`summary` | 1.2 的取值规则 |
| `tags`、`related` | JSON 数组 |
| `superseded_by` | 取代它的条目编号 |
| `updated`、`review_by` | 日期；文档的 `review_by` 为空 |
| `path` | 相对工作区的路径，唯一 |
| `content_sha256`、`file_mtime`、`file_size` | 增量同步用 |
| `hits`、`last_hit_at` | 命中次数与最近命中时间 |
| `indexed_at` | 最近一次写入索引的时间 |

索引按 `type`、`status` 建立(01 4.2)，另按 `path` 建唯一索引。

**knowledge_fts 的定义**

```sql
CREATE VIRTUAL TABLE knowledge_fts USING fts5(
    id UNINDEXED, title, summary, tags, body,
    tokenize = "unicode61 remove_diacritics 2"
);
```

- 写入 `knowledge_fts` 的文本先经过 `text.py` 预处理，查询串经过同一个函数，保证两边切分一致。
- 排序使用 `bm25(knowledge_fts, 0.0, 10.0, 5.0, 5.0, 1.0)`：五个权重依次对应 `id`、标题、摘要、标签、正文，与 16.4 的列权重一致；`id` 列不参与匹配，权重为 0。`bm25()` 的值越小越相关，结果中的 `score` 取其相反数。
- 权重只定义在 `query.py` 的常量中；修改后必须运行检索评测(1.9)。

### 1.6 内部流程

#### 1.6.1 文本预处理

FTS5 的 `unicode61` 分词器把 Unicode 类别为字母(L*)、数字(N*)与私用区(Co)的字符视为词字符，其余视为分隔符，连续的词字符组成一个词。中日文字符属于字母类，连续的中文会被当作一个词，导致「权限」无法命中「数据权限校验」。因此：

1. 索引前，`text.py` 在每个汉字、平假名、片假名与韩文字符两侧插入空格，使其各自成为一个词；拉丁字母、数字与下划线之外的标点保持原样，由分词器按分隔符处理。
2. 查询时，查询串中的每个检索词经过同样的处理后作为一个带引号的短语(FTS5 的 phrase)，要求这些字在正文中相邻且顺序一致。「权限」变成短语 `"权 限"`，接口路由 `/api/Material/Query` 变成短语 `"api Material Query"`。
3. 大小写不敏感，拉丁字母的变音符号去除，由分词器完成。

#### 1.6.2 查询串解析

1. 按空白拆分查询串；双引号括起的部分作为一个检索词整体保留。
2. 每个检索词经 1.6.1 处理；处理后为空的丢弃。全部为空时抛出 `InvalidQuery`。
3. 每个检索词中的双引号加倍转义后包进双引号，成为一个 FTS5 短语；多个短语以 `OR` 连接。检索词中的 FTS5 语法字符(`*`、`^`、`:`、括号、`AND` 等)全部处于引号内，不会被解释为运算符。
4. `OR` 连接使部分命中的条目也能出现，命中的短语越多、越集中在高权重列，`bm25` 排序越靠前。

#### 1.6.3 同步

`sync(full=False)` 的步骤：

1. 获取对象锁 `knowledge`；以 `BEGIN IMMEDIATE` 开启事务，与其他进程(MCP 服务、定时运行、终端命令)的同步互斥。
2. 按 1.2 的来源表列出全部文件；与 `knowledge_meta` 中的 `path`、`file_mtime`、`file_size` 比较，找出新增、可能变化、已删除的文件。`full=True` 时把全部文件视为可能变化。
3. 对可能变化的文件计算 `content_sha256`，与记录一致的只更新 `file_mtime`，其余重新解析。
4. 解析：读取 frontmatter，按目录选择 schema 校验，检查标签前缀格式，取出 1.2 中的各字段。
5. 全库一致性检查(在内存中合并「未变化的行 + 本次解析结果」后执行)：
   - `id` 不重复；同一 `path` 的 `id` 发生变化，视为旧 `id` 删除、新 `id` 新增。
   - `related` 与 `supersededBy` 引用的编号存在。
   - `status` 为 `superseded` 的条目必须有 `supersededBy`，且不构成循环。
6. 步骤 4、5 出现任何错误：回滚事务，返回全部错误(文件路径、JSON 路径、原因)，索引保持上一次成功同步后的状态。
7. 无错误：对新增与变化的行执行 `upsert`，保留已有的 `hits` 与 `last_hit_at`；对删除的行执行删除；每一行都依次调用所有 `Indexer` 的 `upsert` 或 `remove`；提交事务。
8. 有条目变化时调用 `regenerate_index_files()`。
9. 写一个事件：`operation` 为 `run_script`，`attributes` 中记录新增、改动、删除的数量与错误数。

**触发时机**

- 每次 `KnowledgeService` 执行 `search`、`get`、`related`、`stale`、`context_for` 前先执行一次增量同步。文件未变化时只做目录遍历与 `stat` 比较，不读文件内容。
- 读取操作前的同步失败时，读取照常基于上一次的索引进行，结果的 `index_warnings` 写明「知识文件有格式错误，本次检索未包含最新改动」及出错文件列表；命令行在输出末尾提示运行 `tightrein admin kb sync` 查看详情。
- `tightrein admin kb sync [--full]` 显式同步，出错时列出全部错误并以非零状态退出。
- `writer` 写入文件后只同步被写的文件。

#### 1.6.4 INDEX.md 的生成

| 文件 | 内容 |
|---|---|
| `knowledge/<类型>/INDEX.md` | 该类型下 `active` 的条目，每行一条：`- <编号> <摘要> (<文件名>)`，按编号排序 |
| `knowledge/INDEX.md` | 总索引：每个类型一节，列出条目数与该类型 `INDEX.md` 的相对链接；类型条目总数不超过 60 时直接列出各条目，否则只给链接 |

- 内容只取自 `knowledge_meta`，不读文件；文件第一行写明「本文件由 tightrein admin kb sync 生成，不要手工编辑」。
- 每个文件不超过 200 行。某一类型超出时，该类型的 `INDEX.md` 只列出分页文件，条目按编号每 180 条一页写入 `INDEX-<起始编号>-<结束编号>.md`。
- 生成结果与磁盘上的内容相同时不写文件，避免在 git 中产生无意义的改动。
- `superseded` 与 `archived` 的条目不进入 `INDEX.md`，仍可通过 `admin kb search --status` 与 `admin kb get` 查到。

#### 1.6.5 四个操作

**search**

1. 按 1.6.2 生成 MATCH 表达式。
2. 每个 `CandidateSource` 各取 `limit × 3` 个候选；过滤条件(`types`、`tags`、`status`)在各来源内部通过与 `knowledge_meta` 连接实现，`tags` 使用 `json_each` 判断全部包含。
3. 只有一个来源时直接按其顺序截取 `limit` 条；有多个来源时用 `rrf_fuse` 合并后截取。
4. 返回编号、摘要、路径、得分，不返回正文。
5. 写一个事件：`operation` 为 `execute_tool`，`agent` 为 `kb`，`attributes` 中记录查询串、过滤条件与返回的编号列表。检索评测的用例从这些事件中收集(1.9)。

**get**

1. 按编号查 `knowledge_meta`，不存在时抛出 `EntryNotFound`，错误信息附上 `admin kb search` 的用法提示。
2. 从文件读取 frontmatter 与正文。文件的哈希与索引不一致时，先同步该文件再返回。
3. `record_hit=True` 且非沙箱模式时，以一条 `UPDATE knowledge_meta SET hits = hits + 1, last_hit_at = ? WHERE id = ?` 记录命中；时间取自 `Clock`。
4. 写一个事件，`attributes` 中记录编号。

以下读取不计命中：`context_for` 组装摘要、`INDEX.md` 生成、`dedup` 读取候选条目、检索评测、`evaluation` 的沙箱运行。

**related**

返回四种关系的条目，包含非 `active` 的条目，便于查看历史：

| 关系 | 含义 |
|---|---|
| `related` | 本条目 `related` 中列出的条目 |
| `related-by` | `related` 中列出了本条目的条目(通过 `json_each` 反查) |
| `superseded-by` | 沿 `supersededBy` 向后追到当前有效的条目，链上每一条都列出 |
| `supersedes` | `supersededBy` 指向本条目的条目 |

**stale**

只看 `active` 的条目(不含文档)：

| 类别 | 判定 |
|---|---|
| 过期 | `review_by` 早于 `Clock` 的当天 |
| 未命中 | `last_hit_at` 早于当前时间减去 `unusedDays`(默认 90 天)；从未命中的，以 `updated` 代替 `last_hit_at` 判断 |
| 矛盾候选组 | 同一类型内，标签交集不少于 2 个的条目按连通关系分组，每组 2 到 8 条 |

矛盾候选组交给 `learn`，由它经执行器比对后决定是否写进周报(16.7)。

#### 1.6.6 为 agent 任务组装上下文

`context_for(task)` 的规则与 16.6 一一对应：

| 任务 | 按位置匹配的条目 | 按位置匹配的文档 | 直接放全文的类别 |
|---|---|---|---|
| `static-review` | `defect-pattern`：`path:` 标签是任一改动文件的前缀 | — | — |
| `triage` | `defect-pattern`、`tradeoff`、`triage-lesson`：`route:`、`page:`、`path:` 任一匹配 | 同一路由或同一文件的 `finding`、`issue` | `tradeoff` |
| `fix` | `fix-lesson`、`contract`：`path:` 是任一根因文件的前缀 | 同一文件的 `fix-report` | — |

步骤：

1. **按位置匹配**：`path:` 前缀匹配，`route:` 与 `page:` 精确匹配。前缀越长排序越靠前；同等长度按 `updated` 从新到旧。
2. **补足**：按位置匹配的条目少于该任务的上限时，以 `keywords` 执行一次 `search`，按同样的类型过滤，补足到上限，`reason` 写为「关键词检索」。
3. **去除**：排除 `exclude_ids` 与非 `active` 的条目。
4. **直接放全文**：表中最后一列的类别，若过滤后全部 `active` 条目的估算 token 数不超过 `inlineFullTokens`，把全文放进 `inline_documents`，这些条目不再出现在 `items` 中；超过时按普通条目处理，只给摘要。
5. **估算 token**：汉字、假名、韩文每字计 1，其余字符每 4 个计 1。只用于上限判断与报告，不要求精确。
6. `render()` 输出一段固定格式的文本：先说明「以下是与本任务相关的知识条目摘要，需要正文时用 `admin kb get <编号>` 读取」，再逐行列出编号、类型、摘要、命中依据，最后附上直接放入的全文。

每类任务的条目上限在 `thresholds.retrieval.contextLimits` 中配置(01 篇 5.2)。任务对象的路由、页面与文件由调用方从数据库取出后填入 `ContextRequest`；`context_for` 只在 `routes` 为空而给出了问题编号的情况下，才从 `problems` 与 `signals` 中补取位置。

#### 1.6.7 写入、去重与淘汰

`write(draft, runner)` 的流程：

1. 校验草稿：字段齐全、`slug` 格式、标签前缀格式、`related` 中的编号存在。
2. **检索相似条目**：以草稿的标题、摘要与标签拼成查询串，过滤同类型、`active`，取前 5 条。
3. **没有相似条目**：直接按 `add` 执行，不调用执行器。
4. **有相似条目**：组装执行器任务(见下)，由 agent 返回判断。
5. **校验判断**：先按 schema 校验，再做语义校验：

| 决定 | 语义校验 |
|---|---|
| `add` | `supersedes` 中的编号都在候选中 |
| `update` | `targetIds` 恰好 1 个且在候选中；给出了改写后的标题、摘要、标签、正文 |
| `merge` | `targetIds` 至少 1 个且都在候选中；给出了合并后的标题、摘要、标签、正文 |
| `noop` | `targetIds` 至少 1 个，指出已有的等价条目 |

   语义校验不通过时，把逐条原因交回执行器重试一次；仍不通过则不写入，抛出 `WriteDecisionInvalid`，调用方把草稿与原因记入运行摘要。

6. **执行**：

| 决定 | 执行 |
|---|---|
| `add` | 由 `sequences` 分配编号，写新文件；`supersedes` 中的条目改为 `superseded`，`supersededBy` 填新编号 |
| `update` | 改写目标条目的标题、摘要、标签、正文与 `updated`，编号与文件名不变 |
| `merge` | 分配新编号写入合并后的条目，`related` 取各条目 `related` 的并集；被合并的条目改为 `superseded`，`supersededBy` 填新编号 |
| `noop` | 不写文件 |

7. 写文件一律先写同目录临时文件再改名，保证不会留下半截内容；多个文件的改动在持有对象锁 `knowledge` 期间完成。
8. 同步被改动的文件，重新生成 `INDEX.md`，写一个事件，`decision` 为本次决定，`reason` 为 agent 给出的理由摘要。

**判断任务**

| 任务字段 | 取值 |
|---|---|
| `instructions` | `skills/learn/references/knowledge-curator.md` 中的判断规则，附草稿全文与候选条目全文 |
| `workdir` | 工作区 |
| `outputSchema` | `runner/roles/knowledge-curator.schema.json` |
| `access` | `read-only` |
| `allowedCommands` | 无 |
| 模型 | 调用点 `learn.knowledge-curator` 的路由(无论哪个环节发起写入) |

**判断结果的 schema**(`runner/roles/knowledge-curator.schema.json`)

| 字段 | 类型 | 说明 |
|---|---|---|
| `decision` | `add`、`update`、`merge`、`noop` | 判断结果 |
| `targetIds` | 字符串数组 | `update`、`merge`、`noop` 针对的已有条目 |
| `supersedes` | 字符串数组 | `add` 时被新条目推翻的旧条目，可为空 |
| `result` | 对象：`title`、`summary`、`tags`、`body` | `update` 与 `merge` 时的最终内容；`add` 与 `noop` 时不出现 |
| `reason` | 字符串 | 判断理由，一到三句 |

`decision` 为 `update` 或 `merge` 时 `result` 必填，由 schema 的条件约束表达。

**状态变更**：`set_status` 供 `learn` 在用户于周报中确认后执行续期(改 `reviewBy`)、归档(改为 `archived`)。条目从不删除文件，历史通过 `superseded`、`archived` 与 git 记录保留。

### 1.7 两种入口

两种入口都只把参数转换成 `KnowledgeService` 的调用，展示结果；检索、过滤、命中计数与事件写入只在 `KnowledgeService` 中实现一次。两者只提供读取操作，写入只能由流水线模块调用。

**命令行**

| 命令 | 对应 |
|---|---|
| `tightrein admin kb search <关键词> [--type] [--tags] [--status active] [--limit] [--json]` | `search` |
| `tightrein admin kb get <编号> [--json]` | `get` |
| `tightrein admin kb related <编号> [--json]` | `related` |
| `tightrein admin kb stale [--json]` | `stale` |
| `tightrein admin kb sync [--full]` | `sync` |
| `tightrein admin kb eval [--baseline <评测编号>]` | 检索评测(1.9) |
| `tightrein admin kb queries [--since <日期>]` | 从事件日志汇总检索记录，供挑选评测用例 |
| `tightrein admin kb mcp` | 以 stdio 方式运行 MCP 服务 |

- `--status` 可以取 `active`、`superseded`、`archived`、`any`；`--type`、`--tags` 可重复。
- 默认输出适合终端阅读的表格；agent 调用时使用 `--json`，输出结构与 MCP 工具的结构化结果相同。
- 退出码与其他命令相同(09 篇 4.5)：0 成功；2 参数、查询串不合法或编号不存在；1 知识文件格式错误(`admin kb sync`)；3 数据库不可用(先执行初始化)。

**MCP 服务**

- 使用 MCP 官方 Python SDK(PyPI 包 `mcp`，2.x 版本，要求 Python 3.10 及以上)的高层接口 `MCPServer`(`from mcp.server import MCPServer`)。工具以带类型注解与文档字符串的函数定义，SDK 据此生成参数的 JSON schema。
- 传输方式为 stdio：`kb_mcp.py` 在 `if __name__ == "__main__":` 与 `tightrein admin kb mcp` 中调用不带参数的 `run()`，进程从 stdin 读取协议消息、向 stdout 写出；不监听任何端口。agent 工具以子进程方式启动它，例如 Claude Code 使用 `claude mcp add tightrein-kb -- tightrein admin kb mcp --workspace <工作区绝对路径>` 注册；各工具的注册由 `packaging/` 的安装脚本完成。
- stdio 模式下 stdout 就是协议通道：服务进程内不使用 `print`，日志一律经 `logging` 写到 stderr；启动前的包装脚本也不向 stdout 输出任何内容。
- 注册四个工具：`kb_search(query, types, tags, status, limit)`、`kb_get(id)`、`kb_related(id)`、`kb_stale()`，返回值与命令行的 `--json` 输出相同。
- 工具描述写明「只返回摘要，需要正文时用 `kb_get`」，与 16.4 的三级展开一致。
- 服务进程是长驻的，每次调用前按 1.6.3 做增量同步，因此知识文件的改动无需重启即可生效。

**MCP 中的错误**

| 情况 | 抛出 | agent 看到 |
|---|---|---|
| 查询串不合法、编号不存在、过滤条件取值不合法 | `ToolError`(`mcp.server.mcpserver.exceptions`) | 带 `is_error` 标记的结果与原因说明，可以修正参数后重试 |
| 数据库不可用、工作区路径无效 | `MCPError`，错误码为内部错误 | 整个调用失败；该情况下 agent 无法通过修改参数解决 |

工具函数不以返回字符串的方式报告错误，避免调用方把错误当作正常结果。

### 1.8 沙箱模式下的行为

执行器在 `--output` 模式下启动 agent 进程时设置环境变量 `TIGHTREIN_SANDBOX=1`(01 篇 5.5)。命令行与 MCP 服务读到该变量时以 `sandbox=True` 创建 `KnowledgeService`：

- 不执行同步，只读现有索引；
- `get` 不记命中；
- 事件写入沙箱输出目录中的事件文件，而不是 `data/logs/`(01 篇 6.2)。

这保证评测运行(第 2 章)不改变真实的命中统计，也不会因为评测期间的读取让条目看起来「仍在使用」。

### 1.9 检索评测

**用例**：`evals/retrieval/cases.jsonl`，一行一个用例，符合 `data/eval-case.schema.json` 中 `kind` 为 `retrieval` 的分支：

| 字段 | 说明 |
|---|---|
| `id` | `E-<四位序号>` |
| `kind` | `retrieval` |
| `query` | 真实的查询串，取自 `admin kb queries` 的输出 |
| `filters` | 与原查询相同的过滤条件 |
| `expected` | 期望命中的编号列表，至少 1 个 |
| `source` | 来源的运行编号，或「漏检补充」 |

**计算**

1. 用 `evaluation.cases` 校验 `cases.jsonl` 与 `evals/manifest.json` 中的哈希一致(2.6.1)；不一致时报错退出。
2. 对每个用例以 `limit=10`、`record_hit=False` 执行 `search`。
3. 期望编号若已被取代，沿 `supersededBy` 替换为当前有效的编号；期望编号在索引中不存在的，该用例记入 `skipped_cases`，不计入指标。
4. recall@k = 前 k 条结果中命中的期望编号数 ÷ 期望编号数，k 取 5 与 10，对全部用例取平均。
5. MRR = 第一个命中的期望编号的排名的倒数，前 10 条中没有命中时记 0，对全部用例取平均。
6. 报告写入 `data/evals/<评测编号>/`：`report.json` 含三项指标、逐用例的排名与结果、用例集哈希；`report.md` 先写三项指标与结论。

**比较**：`--baseline <评测编号>` 与指定的历史报告比较。两份报告的用例集哈希不同时拒绝比较并说明原因；相同时逐项比较三项指标，任何一项下降即判为「不采纳」，并列出排名变差的用例。用例集只能由用户扩充(2.6.1)，扩充后以新报告作为此后的基线。

### 1.10 向量检索的引入点

按 16.9，只有检索评测显示同义与概念类查询的 recall@10 不达标、扩充标签与同义词后仍不达标时才引入。组件已为此留出两个接入点，引入时不改动 `search`、`context_for` 与两个入口：

| 接入点 | 位置 | 引入时新增 |
|---|---|---|
| 候选来源 | `ranking.py` 的 `CandidateSource`；`search` 遍历已配置的全部来源，多于一个时用 `rrf_fuse` 合并 | `VectorSource`：在同一数据库文件中用 `sqlite-vec` 的 `vec0` 虚拟表做最近邻查询 |
| 索引器 | `indexers.py` 的 `Indexer`；`sync` 对每个变化的行调用全部索引器 | `VectorIndexer`：为条目加上说明来源与上下文的前缀后生成向量并写入 |

- `rrf_fuse` 的得分为各来源中 `1 / (k + 排名)` 之和，`k` 取 60。
- 引入时需要的新表通过 `store/migrations/` 新增一个迁移建立。
- `sqlite-vec` 以可加载扩展的形式提供，需要 `sqlite3` 连接支持 `enable_load_extension`。macOS 系统自带的 Python 不支持加载扩展，需要使用 Homebrew 等方式安装的 Python。

### 1.11 错误处理

| 错误 | 发生在 | 处理 |
|---|---|---|
| `FrontmatterInvalid` | 同步：缺字段、格式错误、标签前缀格式错误 | 同步整体回滚，逐条列出文件、JSON 路径与原因；读取操作照常进行并附带警告(1.6.3) |
| `DuplicateEntryId`、`DanglingReference`、`SupersedeCycle` | 同步的一致性检查 | 同上 |
| `InvalidQuery` | 查询串处理后为空、过滤条件取值不合法、`limit` 越界 | 命令行退出码 2；MCP 为 `ToolError` |
| `EntryNotFound` | `get`、`related` | 命令行退出码 2；MCP 为 `ToolError` |
| `WriteDecisionInvalid` | 去重判断重试后仍不合格 | 不写入；调用方记入运行摘要，草稿保留在调用方的交接文档中 |
| `LockTimeout` | 获取对象锁 `knowledge` 超时 | 读取操作跳过同步、基于现有索引返回并附警告；写入操作失败，由调用方下次运行重试 |
| `IndexUnavailable` | 数据库文件不存在或迁移未执行 | 命令行退出码 3，提示先执行初始化；MCP 为 `MCPError` |
| 文件与索引不一致 | `get` 时文件已被删除 | 同步该文件后抛出 `EntryNotFound` |
| 事件写入失败 | 任意操作 | 按 01 6.2 处理，不影响操作结果 |

### 1.12 测试方式

| 对象 | 测试 |
|---|---|
| `text.py`、`query.py` | 中文、日文、英文混排的切分；两字中文词能命中长句；路由与带下划线的标识符按短语匹配；包含 `"`、`*`、`:`、`AND`、括号的查询不被解释为运算符；全为标点的查询抛出 `InvalidQuery` |
| `sync.py` | 临时工作区夹具：合法文件全量同步；缺字段、重复编号、悬空引用、取代循环各一例，断言回滚且错误完整；改动 `mtime` 而内容不变时不重新解析；删除文件后行被删除；重新同步后 `hits` 保留 |
| `search.py` | 同一关键词出现在标题与正文的两个条目，标题中出现的排在前面；类型、标签、状态过滤；默认不返回 `superseded` 与 `archived`；`get` 计命中，沙箱模式与 `record_hit=False` 不计 |
| `related`、`stale` | 取代链与反向引用；用固定时间的 `Clock` 构造过期、未命中与矛盾候选组 |
| `index_md.py` | 与期望文件逐字比较；超过 200 行时分页；内容不变时不写文件 |
| `context.py` | 每类任务一组夹具，断言选中的编号、顺序、`reason` 与全文放入的阈值判断 |
| `dedup.py`、`writer.py` | 用 `replay` 执行器分别录制 `add`、`update`、`merge`、`noop` 与一次语义不合格的判断，断言文件、`knowledge_meta` 与 `supersededBy` 的变化；写入中途抛出异常时磁盘上不存在半截文件 |
| `benchmark.py` | 小型用例集上手算 recall@k 与 MRR 并比对；期望编号被取代与不存在的处理；用例集哈希不同时拒绝比较 |
| 两个入口 | 同一组查询分别经命令行 `--json` 与 MCP 工具执行，结果一致；MCP 用 SDK 的进程内客户端 `Client(mcp, raise_exceptions=True)` 测试，断言工具列表、结构化结果、错误时的 `is_error` 标记；stdio 模式下 stdout 只包含协议消息 |

## 2. evaluation

### 2.1 职责

| 负责 | 不负责 |
|---|---|
| 加载评测用例，校验 schema、哈希与未提交改动 | 编写与修改封存的评测用例(由用户维护) |
| 构建待比较的版本快照 | 决定是否采纳改进建议(由用户决定并自己应用，14.5) |
| 以 `--output` 沙箱模式运行被测模块，每个用例每个变体至少 3 次 | 归纳改进建议(由 `learn improve` 负责，14.4) |
| 条目表的存放与加载；代码评分器与模型评审 | 生产运行中的重做与转人工(由各流水线模块按 12.3 处理) |
| 统计均值、方差、通过率与变体间差值，判定是否有用例变差 | |
| 生成评测报告 | |

条目表与评分器在生产运行与评测中是同一份：`triage`、`fix`、`issue` 在生产中按 12.3 打分时调用本组件的 `score_output`，评测时调用的也是它。生产中 `score_output` 只执行 `code` 项；`judge` 项只有修复条目表中的评审项，其结果取自 `fix-reviewer` 的那一次评审，不另调用模型评审。

**运行时机**：评测只在以下改动时运行，不对生产中的每个任务运行：

| 改动 | 发起方 |
|---|---|
| skill 的提示词与角色说明 | `learn improve` 的 prompt 类建议(2.9)，或用户修改后手动 `admin eval run` |
| 条目表与评分器 | 用户修改后执行 `admin eval verify --scorers` 与一次 `admin eval run` |
| 流程：模块的步骤代码与阈值 | 用户修改后手动 `admin eval run` |
| 各角色的工具、模型与档位 | `learn improve` 的 model 类建议(2.9)，或用户以 `admin eval run --runner`、`--model` 比较后调整配置 |

检索相关的改动只运行检索评测(1.9)。

### 2.2 文件划分

```
evaluation/
  service.py          evaluate() 门面：组织计划、执行、统计、报告；支持中断后续跑
  cases.py            用例加载、schema 校验、按模块列出用例；fix 另加运行即评测的用例
  run_cases.py        运行即评测用例 data/eval/cases/ 的读取与哈希
  manifest.py         用例哈希的计算与比对；evals/ 目录未提交改动的检查；用户确认后的重新封存
  versions.py         版本快照：取出指定 commit、叠加补丁、复制当前工作区；数据库快照
  variants.py         变体(版本 × 执行器 × 模型)的定义与展开
  sandbox.py          以 --output 模式在子进程中运行被测模块，收集交接文档、会话记录与用量
  rubric.py           条目表的加载与校验；生成写进生成者提示的验收标准与评审者提示的评审项
  rubrics/            条目表数据(12.2)，每个环节一个文件
    triage.json
    fix.json
    issue.json
  scorers/
    base.py           ItemResult、Scorer 接口
    code.py           代码评分器注册表与 12.2 中各代码项的实现
    judge.py          模型评审：组装评审任务、经执行器运行、映射结果
    judge-instructions.md   评审者的说明正文
    assertions.py     用例期望断言的求值
  scoring.py          score_output：对一次输出执行全部适用的评分项
  stats.py            均值、样本方差、通过率、差值
  compare.py          变体间比较与判定
  report.py           report.json 与 report.md 的生成
  errors.py           本组件的异常类型
```

- 评审者的说明放在 `evaluation/` 内而不是 `skills/` 下：`skills/` 属于改进建议可以修改的范围(14.6)，评分器不属于。
- 入口层 `cli/eval.py` 提供 `tightrein admin eval` 子命令(2.7)。

### 2.3 评测用例

#### 2.3.1 目录

```
evals/
  manifest.json                 全部用例的哈希
  <模块>/<用例编号>/
    case.json                   用例定义，符合 data/eval-case.schema.json
    input/                      输入交接文档与其他输入文件
    gates.json                  可选：沙箱运行中各关口的预设决定
    replay/                     可选：录制的执行器结果，用于核对评分器
  retrieval/
    cases.jsonl                 检索评测用例(1.9)
```

用例编号为 `E-<四位序号>`，在每个模块目录内独立递增，由用户新增用例时选定(2.7 的 `admin eval add` 自动取下一个)。

评测 `fix` 时另加运行即评测的用例(design 8.7)：修复部署后确认通过时，`verify` 保存 `data/eval/cases/<Issue 编号>.json` 与 `<Issue 编号>.input.json`(该 Issue 的 issue 环节交接文档副本)。`evaluation.cases.verify_cases` 把它们与 `evals/` 中封存的用例一起加载，用例编号为 Issue 编号，以修复前的提交为基准；它们不经 manifest 封存，两个文件的哈希记入 `case_hashes`，续跑时核对。

#### 2.3.2 data/eval-case.schema.json

schema 以 `kind` 区分两类用例，`retrieval` 分支见 1.9。`kind` 为 `module` 的字段：

| 字段 | 必填 | 说明 |
|---|---|---|
| `schemaVersion` | 是 | 整数 |
| `id` | 是 | `E-<四位序号>`，与目录名一致 |
| `kind` | 是 | `module` |
| `module` | 是 | `Stage` 的取值，限 `triage`、`fix`、`issue` 等产生 LLM 结果的环节 |
| `title` | 是 | 一句话说明这个用例考什么 |
| `category` | 是 | `representative`(有代表性的成功案例)、`corrected`(失败后人工纠正过的案例)、`edge`(边界情况) |
| `source` | 是 | `runId`、`subjectId`；`corrected` 类还要写 `correction`：改判记录或被关闭的 PR |
| `input.handoff` | 是 | `input/` 下的交接文档文件名，作为 `--input` |
| `input.commit` | 是 | 被测项目的代码快照 commit，作为 `--commit` |
| `input.args` | 否 | 附加的命令参数，只允许 schema 列出的参数(例如修复模式) |
| `expected.excludeItems` | 否 | 对本用例不适用的评分项编号及理由 |
| `expected.assertions` | 否 | 期望断言列表，见下表 |
| `replayExpectation` | 否 | `replay/` 中录制结果的期望逐项结果，用于核对评分器(2.6.4) |

**期望断言**：用于检查结果是否与人工确认的正确答案一致，例如 `corrected` 类用例的正确判定。每条断言是一个代码评分项，编号为 `assert-<序号>`。

| 字段 | 说明 |
|---|---|
| `path` | 交接文档 `outputs` 内的点分路径，数组用 `[*]` 表示任一元素 |
| `op` | `equals`、`in`、`contains`、`matches`(正则)、`exists`、`absent` |
| `value` | 比较值；`exists`、`absent` 不需要 |
| `description` | 断言的含义，写进报告 |

#### 2.3.3 条目表

`rubrics/<环节>.json` 是 12.2 条目表的机读形式：

| 字段 | 说明 |
|---|---|
| `id` | 评分项编号，例如 `triage.evidence-location` |
| `text` | 条目原文；生成者的提示中作为验收标准，评审者的提示中作为评审项 |
| `method` | `code` 或 `judge`；12.2 中写「代码初筛加评审」的项拆成两个评分项 |
| `scorer` | `code` 项在注册表中的名称 |
| `appliesWhen` | 可选，适用条件，例如 `outputs.verdict == refuted`、深度评审项的 `outputs.risk.level == high`；不满足时该项记为不适用 |
| `params` | 可选，评分器参数，例如含糊措辞的词表 |

`rubric.py` 在加载时检查：每个 `code` 项的 `scorer` 已注册；`id` 不重复；环节的全部评分项都有来源。写进提示的条目由 `rubric.render(rubric, audience)` 生成：生成者(`generator`)看到标题为「验收标准」的条目文字，不带编号与判定方式；评审者(`judge`)看到带编号与判定方式的「评审项」(12.2)。

### 2.4 接口

```python
@dataclass(frozen=True)
class VersionSpec:
    label: str                           # 报告中显示的名称，例如 "baseline"、"candidate"
    commit: str = "HEAD"                 # tightrein 仓库的 commit
    patch: Path | None = None            # 叠加在 commit 上的 diff，例如改进建议的补丁
    use_worktree: bool = False           # 使用当前工作区中的文件，而不是某个 commit

@dataclass(frozen=True)
class Variant:
    label: str
    version: VersionSpec
    runner: str                          # claude、codex、agy、replay
    model: str | None                    # None 表示使用该版本 project.yaml 中该环节的配置

@dataclass(frozen=True)
class EvaluationPlan:
    module: Stage
    case_ids: tuple[str, ...]            # 为空表示该模块的全部用例
    variants: tuple[Variant, ...]        # 第一个为基线
    repeats: int = 3                     # 不小于 3
    purpose: Literal["version", "tool-model"] = "version"

@dataclass(frozen=True)
class ItemResult:
    item_id: str
    method: Literal["code", "judge"]
    result: ScoreResult                  # pass、fail、unknown、not-applicable
    reason: str
    evidence: list[str]                  # 文件路径:行号、交接文档内路径

@dataclass(frozen=True)
class RunScore:
    case_id: str
    variant: str
    attempt: int
    status: RunnerStatus                 # 被测模块中执行器的最终状态
    items: list[ItemResult]
    score: float                         # 通过项 ÷ (适用项 - unknown 项)
    passed: bool                         # 全部适用项都为 pass
    usage: Usage
    duration_ms: int
    output_dir: Path

@dataclass(frozen=True)
class CaseStats:
    case_id: str
    variant: str
    scores: list[float]
    mean: float
    variance: float                      # 样本方差，n - 1 为分母
    pass_rate: float                     # passed 的次数 ÷ 运行次数
    item_pass_counts: dict[str, tuple[int, int]]   # 评分项 -> (通过次数, 适用次数)
    unstable: bool                       # 同一变体的多次运行中既有通过也有不通过

@dataclass(frozen=True)
class EvaluationReport:
    evaluation_id: str                   # EV-<日期>-<时分秒>
    plan: EvaluationPlan
    manifest_sha256: str
    evals_tree: str                      # evals/ 目录在 tightrein 仓库中的 git 树对象编号
    case_stats: list[CaseStats]
    comparisons: list[CaseComparison]
    verdict: EvalVerdict                 # pass、reject、needs-review、incomplete
    report_path: Path

def evaluate(plan: EvaluationPlan, *, workspace: Workspace, runner_factory: RunnerFactory,
             clock: Clock, events: EventWriter) -> EvaluationReport: ...
def resume(evaluation_id: str, **deps) -> EvaluationReport: ...

def score_output(stage: Stage, handoff: dict, context: ScoringContext,
                 runner: Runner | None) -> list[ItemResult]: ...

def verify_cases(workspace: Workspace, module: Stage | None = None) -> list[CaseProblem]: ...
def seal_cases(workspace: Workspace, confirmed_by_user: UserConfirmation) -> str: ...
```

- 00 中的 `evaluate(module, version, runner)` 即以上 `EvaluationPlan` 的一种简写：单个版本、单个执行器。
- `ScoringContext` 包含用例的期望、代码快照目录、被测项目 commit、评审者的执行器与模型；生产运行中由调用模块构造，期望为空。
- `runner=None` 时只执行 `code` 项，`judge` 项记为 `unknown`。

### 2.5 读写的数据

| 数据 | 读 | 写 |
|---|---|---|
| `evals/<模块>/<用例编号>/`、`evals/manifest.json` | 全部 | 只有 `seal_cases` 与 `admin eval add`，均需用户在终端中确认 |
| `evals/retrieval/cases.jsonl` | 由 `retrieval.benchmark` 经 `cases.py` 读取 | 同上 |
| tightrein 仓库的 git 历史 | `versions.py` 经 `vcs` 只读命令取出快照 | — |
| 被测项目仓库 | `versions.py` 经 `vcs` 只读命令导出代码快照 | — |
| `data/tightrein.db` | 以 SQLite 在线备份复制一份给版本快照 | `budget_usage`(按 `improve` 环节累计评测费用) |
| `data/evals/<评测编号>/` | 续跑、报告、检索评测的基线 | 全部评测产物 |
| `data/logs/events-<日期>.jsonl` | — | 评测本身的 span(`stage` 为 `improve`)；被测模块的事件不写入这里 |
| `scores` 表 | — | 不写：评测分数只留在评测目录，避免混入生产的评分统计(12.3) |

**评测目录**

```
data/evals/<评测编号>/
  plan.json                       评测计划、用例清单与各用例哈希、manifest 哈希、随机种子
  versions/<版本标签>/            版本快照(tightrein 仓库的文件 + 数据库副本)
  snapshots/<被测项目 commit>/    被测项目的代码快照，只读，供评审与证据核对
  outputs/<变体标签>/<用例编号>/<第几次>/   --output 沙箱目录：交接文档、会话记录、事件、原始输出
  scores.jsonl                    每次运行一行 RunScore
  report.json                     机读报告，符合 data/eval-report.schema.json
  report.md                       人读报告
```

### 2.6 内部流程

#### 2.6.1 用例校验与防篡改

每次评测(以及检索评测)开始前执行，任何一项不通过都直接报错，不运行任何用例：

1. **manifest 哈希**：对每个用例目录，按相对路径排序，逐个文件把「相对路径 + NUL + 文件内容」送入 SHA-256，得到该用例的哈希；`cases.jsonl` 按整个文件计算。与 `evals/manifest.json` 逐项比较，不一致、多出或缺少的用例全部列出。
2. **未提交改动**：经 `vcs` 执行只读的 `git status --porcelain -- workspaces/<项目>/evals`，有任何输出即报错。用例的任何变更都必须以提交的形式留在 git 历史中。
3. **schema**：每个 `case.json` 按 `data/eval-case.schema.json` 校验；`expected.excludeItems` 中的评分项编号在条目表中存在。
4. 记录 manifest 文件自身的哈希与 `evals/` 的 git 树对象编号，写进 `plan.json` 与报告，供事后核对这次评测用的是哪一版用例集。

**封存**：用户新增或修改用例后执行 `tightrein admin eval seal`。它只在交互终端中运行(标准输入不是终端时拒绝执行)，列出与现有 manifest 的差异，经用户输入确认后重算并写入 `manifest.json`，写一个 `user_action` 事件。agent 执行器启动的进程中设置了 `TIGHTREIN_RUN_ID`(01 篇 5.5)，`seal` 与 `admin eval add` 检测到该变量时同样拒绝执行。

#### 2.6.2 版本快照

| `VersionSpec` | 构建方式 |
|---|---|
| `commit` | 经 `vcs` 执行只读的 `git archive <commit>`，解包到 `versions/<标签>/` |
| `commit` + `patch` | 同上，再在快照目录中应用 diff；不能干净应用时报错 |
| `use_worktree` | 复制 tightrein 仓库中已跟踪与未被忽略的文件 |

之后：

1. **防作弊检查**：计算快照与基线快照之间改动的文件，只要触及以下任一路径即拒绝评测：`core/tightrein/evaluation/`、`core/tightrein/guards/`、`workspaces/*/evals/`、`core/tightrein/pipeline/improve/`、`skills/improve/`。这与 14.6 的不可改范围对应，`learn improve` 在提交评测前已检查补丁只改 `skills/` 下的文件，这里再查一遍。
2. **数据库副本**：用 SQLite 在线备份把 `data/tightrein.db` 复制一次，再分别复制到每个版本快照的 `workspaces/<项目>/data/` 下。所有版本读到完全相同的知识索引与历史数据，真实数据库不会被任何快照写入。
3. **被测项目代码**：对用例涉及的每个 `input.commit`，经 `vcs` 只读的 `git archive` 导出到 `snapshots/<commit>/`，设为只读。评审者与证据核对都在这里读代码。

快照中的核心从自身所在的仓库根目录定位 `skills/`(01 篇 4.3)，因此每个版本的执行器任务加载的就是该版本的 skill。

#### 2.6.3 沙箱运行

每次运行启动一个子进程：

```
<快照>/core 下的 tightrein 入口
  <模块> --input <用例>/input/<交接文档> --output <outputs/变体/用例/次数>
  --commit <input.commit> --ignore-state --runner <执行器> [--model <模型>]
  [--gate-decisions <用例>/gates.json] [用例的附加参数]
  --workspace <快照>/workspaces/<项目>
```

- `--output` 保证不写数据库、不产生对外操作(15.5)；执行器因此以 `TIGHTREIN_SANDBOX=1` 启动 agent 进程，知识检索不记命中(1.8)，事件写入沙箱目录(01 篇 6.2)。
- `--gate-decisions` 只在 `--output` 模式下有效，为需要用户关口的模块(例如修复计划的确认)提供预设决定；用例没有提供而运行中遇到关口时，该次运行以 `blocked` 结束并记为不通过。
- **运行顺序**：按「次数 → 用例 → 变体」三层循环，同一轮内变体的顺序按记录在 `plan.json` 中的随机种子打乱，避免某个变体总在限流或服务波动的时段运行。
- **并发**：默认逐个运行，`evaluation.parallelism` 可以调大。
- **预算**：每次运行结束后累计用量；超过 `evaluation.budgetUsd` 时停止启动新运行，报告标为 `incomplete`。
- **收集**：从输出目录读取该模块的交接文档(恰好一份，否则记为运行错误)、会话记录路径、事件文件中的用量与耗时。
- **续跑**：一次运行在 `scores.jsonl` 中有记录即视为完成。`tightrein admin eval resume <评测编号>` 重新校验用例哈希(与 `plan.json` 一致才继续)，只执行缺少的运行。

**运行结果的归类**

| 情况 | 处理 |
|---|---|
| 交接文档 `status` 为 `ok` | 进入评分 |
| 交接文档 `status` 为 `blocked` 或 `failed`，或执行器结果为 `limit-reached`、`schema-invalid` | 该次运行的全部适用项记为 `fail`，`reason` 写明原因；这是被测版本自身的失败 |
| 执行器无法启动(工具未安装、未登录、找不到可执行文件) | 停止整个评测，报告标为 `incomplete`；这是环境问题，不能算作任何一方的失败 |
| 子进程超时或异常退出且没有交接文档 | 记为不通过，保留标准错误输出路径；同一变体连续 3 次出现时停止评测 |

#### 2.6.4 评分

`score_output` 对一次输出执行：

1. 加载该环节的条目表，按 `appliesWhen` 与用例的 `excludeItems` 确定适用项。
2. **代码评分项**：直接调用注册的评分器。12.2 中各代码项的实现方式：

| 评分项 | 实现 |
|---|---|
| 证据带 `文件路径:行号` 且真实存在 | 解析每条证据，在 `snapshots/<commit>/` 中检查文件存在且行号不超过文件行数 |
| 输出字段齐全、判定属于四档之一 | 按 `handoff/outputs/triage.schema.json` 校验 |
| 结论不含含糊措辞 | 按条目表 `params` 中的词表匹配结论字段 |
| 判不成立时现象来源指向真实位置或主张中的事实；反证检查带入口与上游校验 | 与 `triage/steps/evidence_checks.py` 共用 `refuted_source`、`counter_check` 两个评分器 |
| 复现检查修复前失败、修复后通过；项目检查通过；其他 Issue 的复现检查保持通过 | 读取交接文档中由核心执行的检查结果(这些结果由核心的确定性步骤写入，不是 agent 的输出) |
| 改动量上限、计划外文件、受保护文件、测试与复现检查未被修改、没有新增跳过测试的标记、没有调试残留与临时文件 | 对沙箱输出中的 diff 调用 `guards` 的改动检查函数与 `fix` 的交付规则重新计算，不采用交接文档中的自述 |
| 必需章节齐全、证据带位置、日期为绝对日期 | 解析渲染出的人读文档 |
| 期望断言 | `assertions.py` 求值 |

3. **模型评审项**：同一次输出的全部 `judge` 项合并为一个执行器任务：

| 任务字段 | 取值 |
|---|---|
| `instructions` | `judge-instructions.md`，附条目表中 `judge` 项的原文、用例输入、被评的 `outputs` |
| `workdir` | `snapshots/<commit>/` |
| `outputSchema` | `runner/roles/judge.schema.json`：每个评分项一条 `itemId`、`result`(`pass`、`fail`、`unknown`)、`reason`、`evidence` |
| `access` | `read-only` |
| `allowedCommands` | 只读的检索命令 |

   - 评测中的 `judge` 项一律由本组件的模型评审打分，不采用沙箱输出中 `fix-reviewer` 的结论：改进建议可能改动评审说明本身，用被评对象的评审给自己打分无法发现评审变宽松。
   - 评审者看不到生成者的会话记录与推理过程，只看输出(12.5)。
   - 评审说明要求逐项独立判断、只按评分项原文判断、不报评分项以外的问题、不因篇幅加分、无法判断时给 `unknown`。
   - 评审者的工具与模型取自调用点 `eval.judge` 的路由；与被评变体相同时，报告在该变体上标注「评审与生成者使用同一模型」。
   - 评审输出不合 schema 时由执行器重试一次(9.5)，仍不合格则该次的全部 `judge` 项记为 `unknown`，`reason` 写明「评审输出无效」。

4. 计算 `score` 与 `passed`：`unknown` 不计入分母，也不算通过；存在 `unknown` 的运行 `passed` 为假。

**评分器自检**：带 `replay/` 与 `replayExpectation` 的用例，可用 `tightrein admin eval verify --scorers` 以 `replay` 执行器运行并评分，逐项结果必须与 `replayExpectation` 一致。条目表或评分器改动后必须通过这一步，防止评分器本身出错而评测照常给分。

#### 2.6.5 统计

对每个「用例 × 变体」(n 为运行次数，n ≥ 3)：

| 统计量 | 定义 |
|---|---|
| 均值 | n 次 `score` 的算术平均 |
| 方差 | 样本方差，分母 n - 1 |
| 通过率 | `passed` 为真的次数 ÷ n |
| 逐项通过数 | 每个评分项在适用的运行中通过的次数 |
| 不稳定 | 同一变体的 n 次运行中既有通过也有不通过 |
| 用量 | token、费用、耗时的均值 |

对每个变体另算总体：各用例均值的平均、全部运行的通过率、总费用与平均耗时。

对每个非基线变体与基线逐用例比较：差值 = 该变体均值 − 基线均值；另列出逐项通过数的变化。

#### 2.6.6 判定

**版本对比**(`purpose="version"`，用于 prompt 类改进建议)：

| 判定 | 条件 |
|---|---|
| `reject` | 任一用例的差值小于 0 |
| `needs-review` | 没有用例变差，但候选版本存在 `unknown` 项，或存在基线稳定通过而候选版本不稳定的用例 |
| `pass` | 没有用例变差，也没有上一行的情况 |
| `incomplete` | 因预算或环境原因没有完成全部运行；此时不给出以上三种判定 |

**工具与模型对比**(`purpose="tool-model"`，9.6)：同一版本、不同执行器与模型。不做否决判定，报告按总体均值从高到低列出各变体，并列出通过率、平均费用与平均耗时，由用户据此调整 `project.yaml` 中各环节的选择。

### 2.7 命令

| 命令 | 作用 |
|---|---|
| `tightrein admin eval run --module <模块> [--cases E-0001,...] [--version <commit>] [--worktree] [--runner a,b] [--model x,y] [--repeats N]` | 按参数组成计划并运行；`--runner` 与 `--model` 给出多个值时展开为多个变体，计划的用途为 `tool-model` |
| `tightrein admin eval resume <评测编号>` | 续跑 |
| `tightrein admin eval report <评测编号>` | 重新生成并显示报告 |
| `tightrein admin eval verify [--module] [--scorers]` | 只做用例校验(2.6.1)；带 `--scorers` 时同时做评分器自检 |
| `tightrein admin eval add --module <模块> --from <交接文档> --commit <commit>` | 由用户在终端中执行：建立下一个编号的用例目录，复制输入，生成 `case.json` 骨架供用户填写期望 |
| `tightrein admin eval seal` | 用户确认后重算 manifest(2.6.1) |

`add` 与 `seal` 不出现在任何 skill 的说明中，也不在执行器的 `allowedCommands` 中。

### 2.8 报告

`report.md` 的结构，第一节即结论(10.3)：

1. **结论**：判定；变差的用例数与编号；需要人工判断的项数。
2. **评测对象**：模块、各变体(版本标签、commit、执行器、模型)、运行次数、用例数、用例集哈希与 `evals/` 的 git 树对象编号。
3. **总表**：每个变体一行：总体均值、通过率、平均费用、平均耗时；版本对比时附与基线的差值。
4. **变差的用例**：每个用例列出基线与候选的均值、方差、差值，以及从通过变为不通过的评分项与对应运行的输出目录。
5. **逐用例**：全部用例的均值、方差、通过率、是否不稳定。
6. **逐评分项**：每个评分项在各变体中的通过数，突出变化最大的项。
7. **需要人工判断**：全部 `unknown` 项及评审给出的原因。
8. **失败的运行**：被判为不通过的运行及其会话记录路径；失败时读完整记录，区分 agent 真错还是评分器误判。

`report.json` 包含同样的内容与全部 `CaseStats`，符合 `data/eval-report.schema.json`，供 `learn improve` 读取。

### 2.9 与 learn improve 的衔接

1. `learn improve`(design 14.4)先检查 prompt 类建议的补丁只改 `skills/` 下的文件且不触及 2.6.2 的禁止路径，不符合即不出建议，不调用评测。
2. 该环节的用例按来源对象分为参与改进与未参与改进两组；后者少于 `thresholds.learn.improve.minHeldOutCases` 时不出建议。prompt 类以 `HEAD` 加补丁为候选(`purpose="version"`)，model 类以候选别名的模型与该环节主要调用点当前的模型对比(`purpose="tool-model"`)；次数取 `thresholds.learn.improve.repeats`。
3. `learn improve` 按两组分别汇总确定性(`code`)项的通过率与平均分，写进 decision 文档 `data/improve/<建议编号>.md`，评测编号记在其中；是否采纳由用户决定并自己应用(14.5)。

### 2.10 错误处理

| 错误 | 发生在 | 处理 |
|---|---|---|
| `EvalCaseTampered` | 用例哈希不一致、用例多出或缺少、`evals/` 有未提交改动 | 立即停止，列出全部不一致项，提示用户检查后执行 `admin eval seal`；写一个 `gate` 事件，`decision` 为拒绝 |
| `EvalCaseInvalid` | `case.json` 不合 schema、引用了不存在的评分项或输入文件 | 立即停止，逐条列出 |
| `RubricInvalid` | 条目表引用未注册的评分器、编号重复 | 立即停止 |
| `EvaluationRefused` | 候选版本触及防作弊路径(2.6.2) | 立即停止，列出触及的文件 |
| `SnapshotFailed` | `git archive` 失败、diff 不能干净应用、数据库备份失败 | 立即停止，保留已生成的目录便于排查 |
| `RunnerUnavailable` | 执行器无法启动 | 停止，报告标为 `incomplete` |
| 单次运行失败 | 见 2.6.3 的归类 | 记为不通过，继续其他运行 |
| `JudgeOutputInvalid` | 评审输出重试后仍不合格 | 该次 `judge` 项记为 `unknown`，继续 |
| `BudgetExceeded` | 累计费用超过 `evaluation.budgetUsd` | 停止启动新运行，已有结果照常统计，报告标为 `incomplete`，可在调整预算后续跑 |
| 中断 | 进程退出、电脑休眠 | 用 `admin eval resume` 续跑 |

所有停止都在终端与运行摘要中写明原因与下一步命令，不以部分结果给出 `pass`。

### 2.11 测试方式

| 对象 | 测试 |
|---|---|
| `manifest.py`、`cases.py` | 修改用例中任一文件、增删用例目录、存在未提交改动时分别报错；合法用例集通过；非终端环境与设置了 `TIGHTREIN_RUN_ID` 时 `seal` 拒绝执行 |
| `versions.py` | 在临时 git 仓库中构建 `commit`、`commit + patch`、`worktree` 三种快照；diff 触及防作弊路径时拒绝；数据库副本与原库内容一致，写副本不影响原库 |
| `scorers/code.py` | 12.2 中每个代码项至少一个通过样例与一个不通过样例；证据行号超出文件行数时不通过 |
| `scorers/assertions.py` | 每种 `op` 的正反样例；`[*]` 路径 |
| `scorers/judge.py` | 用 `replay` 执行器回放评审结果：合法输出、`unknown`、输出无效三种情况 |
| `stats.py`、`compare.py` | 手算均值、样本方差、通过率、差值并比对；构造「一个用例变差」「有 unknown」「不稳定」「全部持平」四组数据，断言判定分别为 `reject`、`needs-review`、`needs-review`、`pass` |
| `service.py` | 回放夹具(`tests/replay/`)上端到端运行：基线与候选为同一版本、执行器为 `replay` 时，所有差值为 0、方差为 0、判定为 `pass`；中途终止后 `resume` 只补跑缺少的运行；预算耗尽时报告为 `incomplete` |
| `report.py` | `report.json` 符合 `data/eval-report.schema.json`；`report.md` 与期望文件逐字比较 |
| 评分器自检 | 每个带 `replay/` 的用例在 CI 中运行 `admin eval verify --scorers` |

本篇用到的基础层定义(编号、枚举、表、路径、配置)统一见 01-foundation.md。
