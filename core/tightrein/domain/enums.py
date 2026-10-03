"""全部枚举。代码中用英文取值，展示时用 label 中的中文名称。"""

from __future__ import annotations

from enum import Enum


class LabeledEnum(str, Enum):
    """取值是字符串、另带中文名称的枚举。成员定义为 (取值, 中文名称)。"""

    label: str

    def __new__(cls, value: str, label: str) -> "LabeledEnum":
        obj = str.__new__(cls, value)
        obj._value_ = value
        obj.label = label
        return obj

    def __str__(self) -> str:
        return self.value


# 信号、问题与分诊

class Source(LabeledEnum):
    ERROR = ("error", "错误")
    PERFORMANCE = ("performance", "性能")
    BEHAVIOR = ("behavior", "行为")
    FEEDBACK = ("feedback", "反馈")
    SYNTHETIC = ("synthetic", "合成")


class Probe(LabeledEnum):
    """采集方法(redesign/01-collect.md 第 0 节)。"""

    PLATFORM_ERRORS = ("platform-errors", "内部错误")
    ACCESS_LOG = ("access-log", "访问日志")
    ALERTS = ("alerts", "业务告警")
    PROJECT_PROBE = ("project-probe", "项目探针")
    API_FUZZ = ("api-fuzz", "API 模糊测试")
    STATIC = ("static", "静态巡检")
    INCIDENTAL = ("incidental", "任务外发现")


class ProbeLevel(LabeledEnum):
    SHALLOW = ("shallow", "浅跑")
    DEEP = ("deep", "深跑")
    INCREMENTAL = ("incremental", "增量")
    FULL = ("full", "全量")
    BASELINE = ("baseline", "基线")


class SignalAggregateState(LabeledEnum):
    PENDING = ("pending", "待聚合")
    VOIDED = ("voided", "作废")
    DONE = ("done", "已处理")


class ProblemStatus(LabeledEnum):
    PENDING = ("pending", "待确认")
    NEW = ("new", "新发现")
    ONGOING = ("ongoing", "持续")
    RESOLVED = ("resolved", "已解决")
    REGRESSED = ("regressed", "回归")
    IGNORED = ("ignored", "已忽略")




class Verdict(LabeledEnum):
    CONFIRMED = ("confirmed", "确认成立")
    CONDITIONAL = ("conditional", "条件成立")
    REFUTED = ("refuted", "不成立")
    INSUFFICIENT = ("insufficient", "证据不足")


class Severity(LabeledEnum):
    P0 = ("P0", "P0")
    P1 = ("P1", "P1")
    P2 = ("P2", "P2")
    P3 = ("P3", "P3")


class Complexity(LabeledEnum):
    LOW = ("low", "低")
    MEDIUM = ("medium", "中")
    HIGH = ("high", "高")


class ImpactKind(LabeledEnum):
    AUTHORIZATION = ("authorization", "权限")
    DATA_OWNERSHIP = ("data-ownership", "数据归属")
    DATA_CORRECTNESS = ("data-correctness", "数据正确性")
    CREDENTIAL_LEAK = ("credential-leak", "凭证泄露")
    CORE_FLOW_BROKEN = ("core-flow-broken", "核心流程不可用")
    NON_CORE_ERROR = ("non-core-error", "非核心功能出错")
    CONTRACT_MISMATCH = ("contract-mismatch", "契约不一致")
    EXPERIENCE = ("experience", "体验与规范")
    SLOW_RESPONSE = ("slow-response", "响应偏慢")
    DEPENDENCY_VULNERABILITY = ("dependency-vulnerability", "依赖漏洞")


class WorthRecommendation(LabeledEnum):
    FIX = ("fix", "该修")
    OPTIONAL = ("optional", "可修可不修")
    DEFER = ("defer", "暂不修")
    WONT = ("wont", "不该修")


class TaskType(LabeledEnum):
    """取证给出的任务类型，决定修复走哪条通道(redesign/05-fix.md)。"""

    BUG = ("bug", "缺陷")
    SECURITY = ("security", "安全")
    DATA = ("data", "数据")
    FRONTEND = ("frontend", "前端")
    FEATURE = ("feature", "功能")
    REFACTOR = ("refactor", "重构")
    DEPENDENCY = ("dependency", "依赖")
    DOCS_CONFIG = ("docs-config", "文档配置")


class SizeTier(LabeledEnum):
    """规模档：按改动的文件数与行数(不含测试文件)，门槛在 thresholds.tiers。"""

    MICRO = ("micro", "微")
    SMALL = ("small", "小")
    MEDIUM = ("medium", "中")
    LARGE = ("large", "大")
    OVERSIZE = ("oversize", "超限")


class Treatment(LabeledEnum):
    """分诊的处理标签，由 triage.treatment 的决策树给出(redesign/03-triage.md)。"""

    IMMEDIATE = ("immediate", "立即修")
    SCHEDULED = ("scheduled", "排期修")
    OBSERVE = ("observe", "观察")
    WONT_FIX = ("wont-fix", "不修")


class Lane(LabeledEnum):
    """修复通道，按 fix.lanes 的「类型 × 档」流程表决定。"""

    FAST = ("fast", "A 快速")
    STANDARD = ("standard", "B 标准")
    LARGE = ("large", "C 大任务")


class Disposition(LabeledEnum):
    FALSE_POSITIVE = ("false-positive", "判为误报")
    ACCEPTED_TRADEOFF = ("accepted-tradeoff", "已接受的取舍")
    AWAITING_DEPLOY = ("awaiting-deploy", "等待部署")
    CREATE_ISSUE = ("create-issue", "提 Issue")
    DEFERRED = ("deferred", "暂不修")
    MANUAL_QUEUE = ("manual-queue", "人工队列")


class TriageOutcome(LabeledEnum):
    CORRECT = ("correct", "判对")
    FALSE_CONFIRM = ("false-confirm", "误判为成立")
    FALSE_REFUTE = ("false-refute", "误判为不成立")
    OVERRIDDEN = ("overridden", "用户改判")


# Issue、修复、验证与发布

class IssueStatus(LabeledEnum):
    """Issue 的六种状态(redesign/04-issue.md)；进行中的细分见 IssuePhase。"""

    NEEDS_DECISION = ("needs-decision", "待决定")
    TODO = ("todo", "待修")
    IN_PROGRESS = ("in-progress", "进行中")
    PENDING_MERGE = ("pending-merge", "待合并")
    DONE = ("done", "完成")
    CANCELLED = ("cancelled", "取消")


class IssuePhase(LabeledEnum):
    """进行中的细分(修复、合并前验证、提交)与完成后等待部署后确认；只供程序推进，不作为状态显示。"""

    FIX = ("fix", "修复")
    VERIFY = ("verify", "合并前验证")
    SUBMIT = ("submit", "提交")
    DEPLOY_CHECK = ("deploy-check", "部署后确认")


class IssueOrigin(LabeledEnum):
    """Issue 的来源：分诊结论生成，或用户直接提出的需求(不关联问题与信号)。"""

    TRIAGE = ("triage", "分诊")
    MANUAL = ("manual", "用户需求")


class CloseReason(LabeledEnum):
    FIXED = ("fixed", "已修复")
    FIX_REJECTED = ("fix-rejected", "修复未采纳")
    WONT_FIX = ("wont-fix", "不修")
    DUPLICATE = ("duplicate", "重复")
    NOT_A_BUG = ("not-a-bug", "不是缺陷")


class ReviewCategory(LabeledEnum):
    LOCAL = ("local", "局部问题")
    PLAN_GAP = ("plan-gap", "计划没覆盖")
    NEEDS_USER = ("needs-user", "规范要求用户确认")
    DESIGN = ("design", "设计问题")


class ReviewMode(LabeledEnum):
    LIGHT = ("light", "轻量评审")
    DEEP = ("deep", "深度评审")
    SCREENSHOT = ("screenshot", "截图评审")


class ReviewFindingKind(LabeledEnum):
    ROOT_CAUSE_UNFIXED = ("root-cause-unfixed", "根因没有修掉")
    CALLER_BROKEN = ("caller-broken", "破坏了已有调用方或其他入口")
    HARDCODE = ("hardcode", "针对复现输入写死")
    NEW_ERROR_PATH = ("new-error-path", "新增的错误路径")
    REQUIREMENT_UNMET = ("requirement-unmet", "验收标准没有达成")
    REQUIREMENT_REDUCED = ("requirement-reduced", "缩减了需求")
    AUTHZ = ("authz", "权限与数据归属")
    DATA_STRUCTURE = ("data-structure", "数据结构与存量数据")
    CONTRACT = ("contract", "公共接口或契约")


class FixRiskLevel(LabeledEnum):
    NORMAL = ("normal", "常规")
    HIGH = ("high", "高风险")


class RiskCategory(LabeledEnum):
    SCHEMA = ("schema", "数据库结构")
    AUTHZ = ("authz", "权限与数据归属")
    CONTRACT = ("contract", "公共接口或契约")


class VerifyPhase(LabeledEnum):
    LOCAL = ("local", "PR 阶段检查")
    STAGING = ("staging", "部署后确认")


class CheckResult(LabeledEnum):
    PASS = ("pass", "验证通过")
    WEAK = ("weak", "弱证据")
    UNVERIFIED = ("unverified", "未验证")
    FAIL = ("fail", "失败")


class RegressionKind(LabeledEnum):
    API = ("api", "接口")
    PAGE = ("page", "页面")
    STATIC = ("static", "静态")
    TEST = ("test", "测试")


class RegressionResult(LabeledEnum):
    PASSED = ("passed", "通过")
    FAILED = ("failed", "失败")
    NOT_RUN = ("not-run", "未执行")
    INVALID = ("invalid", "无法执行")


class DeploymentStatus(LabeledEnum):
    PENDING = ("pending", "尚无覆盖该 commit 的部署")
    RUNNING = ("running", "部署中")
    SUCCEEDED = ("succeeded", "成功")
    FAILED = ("failed", "失败")


class OperationKind(LabeledEnum):
    CREATE_FIX_WORKTREE = ("create-fix-worktree", "建修复分支与 worktree")
    INIT_READONLY_WORKTREE = ("init-readonly-worktree", "初始化只读 worktree")
    COMMIT = ("commit", "提交")
    MERGE_MAIN = ("merge-main", "合并 origin/main")
    COMMIT_MERGE = ("commit-merge", "冲突解决后的合并提交")
    ABORT_MERGE = ("abort-merge", "放弃合并")
    PUSH = ("push", "推送")
    PULL_REQUEST = ("pull-request", "创建或更新 PR")
    MERGE_PULL_REQUEST = ("merge-pull-request", "合并 PR")
    PR_COMMENT = ("pr-comment", "在 PR 上发评论")
    REVERT_PULL_REQUEST = ("revert-pull-request", "提撤销合并的 PR")
    CLEANUP = ("cleanup", "删除修复 worktree 与本地分支")
    GITHUB_ISSUE = ("github-issue", "同步 GitHub Issue 镜像")
    FIX_PLAN = ("fix-plan", "确认修复计划")
    LOCAL_MIGRATION = ("local-migration", "把迁移应用到测试库")


class OperationExecutor(LabeledEnum):
    VCS = ("vcs", "确认后由核心执行")


class OperationStatus(LabeledEnum):
    PENDING = ("pending", "待确认")
    CONFIRMED = ("confirmed", "已确认")
    REJECTED = ("rejected", "已拒绝")
    EXECUTED = ("executed", "已执行")
    FAILED = ("failed", "执行失败")
    EXPIRED = ("expired", "已过期")


# 运行、执行器与边界

class Stage(LabeledEnum):
    COLLECT = ("collect", "信号采集")
    AGGREGATE = ("aggregate", "聚合去噪")
    TRIAGE = ("triage", "分诊取证")
    ISSUE = ("issue", "提 Issue")
    FIX = ("fix", "生成修复")
    VERIFY = ("verify", "验证")
    RELEASE = ("release", "合并发布")
    LEARN = ("learn", "观测学习")
    IMPROVE = ("improve", "自我改进")


class RunStage(LabeledEnum):
    COLLECT = ("collect", "信号采集")
    AGGREGATE = ("aggregate", "聚合去噪")
    TRIAGE = ("triage", "分诊取证")
    ISSUE = ("issue", "提 Issue")
    FIX = ("fix", "生成修复")
    VERIFY = ("verify", "验证")
    RELEASE = ("release", "合并发布")
    LEARN = ("learn", "观测学习")
    IMPROVE = ("improve", "自我改进")
    LOOP = ("loop", "编排运行")


class RunStatus(LabeledEnum):
    RUNNING = ("running", "进行中")
    OK = ("ok", "成功")
    PARTIAL = ("partial", "部分完成")
    FAILED = ("failed", "失败")
    SKIPPED = ("skipped", "无需运行")
    BLOCKED = ("blocked", "前置条件不满足或等待用户")
    INTERRUPTED = ("interrupted", "被中断")


class HandoffStatus(LabeledEnum):
    OK = ("ok", "成功")
    BLOCKED = ("blocked", "需要用户")
    FAILED = ("failed", "出错")


class DocumentStatus(LabeledEnum):
    """Markdown 交接文档头信息中的状态(redesign/00-handoff-documents.md)。"""

    PENDING = ("pending", "待处理")
    IN_PROGRESS = ("in-progress", "进行中")
    BLOCKED = ("blocked", "受阻")
    DONE = ("done", "完成")
    FAILED = ("failed", "失败")


class RunnerStatus(LabeledEnum):
    OK = ("ok", "成功")
    FAILED = ("failed", "失败")
    LIMIT_REACHED = ("limit-reached", "达到上限")
    SCHEMA_INVALID = ("schema-invalid", "输出不合 schema")
    GUARD_VIOLATION = ("guard-violation", "边界违规")


class Access(LabeledEnum):
    READ_ONLY = ("read-only", "只读")
    WORKSPACE_WRITE = ("workspace-write", "只能写工作目录")


class ViolationKind(LabeledEnum):
    CREDENTIAL_PRESENT = ("credential-present", "进程中存在凭证")
    GIT_UNREADABLE = ("git-unreadable", "无法读取 git 状态")
    READONLY_MODIFIED = ("readonly-modified", "只读 worktree 被修改")
    FORBIDDEN_PATH_MODIFIED = ("forbidden-path-modified", "工作目录以外被修改")
    GIT_COMMIT_CREATED = ("git-commit-created", "新建了提交")
    GIT_BRANCH_SWITCHED = ("git-branch-switched", "切换了分支")
    GIT_REF_CHANGED = ("git-ref-changed", "引用被修改")
    GIT_REMOTE_CHANGED = ("git-remote-changed", "远程配置被修改")
    GIT_STASH_CHANGED = ("git-stash-changed", "stash 被修改")
    GIT_WORKTREE_CHANGED = ("git-worktree-changed", "worktree 列表被修改")
    GIT_OPERATION_STARTED = ("git-operation-started", "开始了未完成的 git 操作")
    PROTECTED_MODIFIED = ("protected-modified", "受保护文件被修改")
    TEST_MODIFIED = ("test-modified", "测试或复现检查被修改")
    SKIP_MARKER_ADDED = ("skip-marker-added", "新增了跳过标记")
    SIZE_EXCEEDED = ("size-exceeded", "改动量超出上限")
    SUSPECTED_HARDCODE = ("suspected-hardcode", "疑似针对复现输入写死")
    RESIDUE_ADDED = ("residue-added", "新增了调试残留")
    OUTSIDE_PLAN = ("outside-plan", "改动了计划外的文件")
    HIDDEN_PATH_READ = ("hidden-path-read", "读取了隐藏路径")


class AgentSessionStatus(LabeledEnum):
    OPEN = ("open", "进行中")
    CLOSED = ("closed", "已结束")


# 评分与评测

class ScoreResult(LabeledEnum):
    PASS = ("pass", "通过")
    FAIL = ("fail", "不通过")
    UNKNOWN = ("unknown", "无法判断")
    NOT_APPLICABLE = ("not-applicable", "不适用")


class ScoreMethod(LabeledEnum):
    CODE = ("code", "代码")
    JUDGE = ("judge", "评审")
    USER = ("user", "用户")


class EvalVerdict(LabeledEnum):
    PASS = ("pass", "通过")
    REJECT = ("reject", "否决")
    NEEDS_REVIEW = ("needs-review", "需人工判断")
    INCOMPLETE = ("incomplete", "未完成")


class EvalCaseCategory(LabeledEnum):
    REPRESENTATIVE = ("representative", "代表性成功")
    CORRECTED = ("corrected", "人工纠正过")
    EDGE = ("edge", "边界情况")


class SuggestionKind(LabeledEnum):
    PROBE_CONFIG = ("probe-config", "采集配置")
    COVERAGE_GAP = ("coverage-gap", "覆盖缺口")
    KNOWLEDGE_REVIEW = ("knowledge-review", "知识复核")
    IMPROVEMENT = ("improvement", "改进建议")
    CONTROL = ("control", "控制措施")


class SuggestionStatus(LabeledEnum):
    PENDING = ("pending", "待处理")
    ACCEPTED = ("accepted", "已接受")
    REJECTED = ("rejected", "已拒绝")
    EXPIRED = ("expired", "已过期")


class YieldOutcome(LabeledEnum):
    """环节效益中一次 LLM 调用的有效产出(design 14.2)：结果未定的不计入分子也不计入分母。"""

    PENDING = ("pending", "结果未定")
    USEFUL = ("useful", "有效产出")
    NO_YIELD = ("no-yield", "无有效产出")


# 知识

_KNOWLEDGE_PREFIX = {
    "defect-pattern": "DP",
    "tradeoff": "TO",
    "triage-lesson": "TL",
    "fix-lesson": "FL",
    "contract": "CT",
    "reference": "RF",
}


class KnowledgeType(LabeledEnum):
    DEFECT_PATTERN = ("defect-pattern", "缺陷模式")
    TRADEOFF = ("tradeoff", "已接受的取舍")
    TRIAGE_LESSON = ("triage-lesson", "分诊经验")
    FIX_LESSON = ("fix-lesson", "修复经验")
    CONTRACT = ("contract", "项目约定")
    REFERENCE = ("reference", "项目参考资料")

    @property
    def prefix(self) -> str:
        return _KNOWLEDGE_PREFIX[self.value]

    @classmethod
    def from_prefix(cls, prefix: str) -> "KnowledgeType":
        for member in cls:
            if member.prefix == prefix:
                return member
        raise KeyError(prefix)


class KnowledgeStatus(LabeledEnum):
    ACTIVE = ("active", "有效")
    SUPERSEDED = ("superseded", "已被取代")
    ARCHIVED = ("archived", "已归档")


class KnowledgeWriteDecision(LabeledEnum):
    ADD = ("add", "新增")
    UPDATE = ("update", "更新已有条目")
    MERGE = ("merge", "合并")
    NOOP = ("noop", "不写入")


# 服务端日志与扩展(architecture/10)

class LogLevel(LabeledEnum):
    """log-parse 归一化后的日志级别；server-log 信号的 check 取其中的 error、critical。"""

    TRACE = ("trace", "跟踪")
    DEBUG = ("debug", "调试")
    INFORMATION = ("information", "信息")
    WARNING = ("warning", "警告")
    ERROR = ("error", "错误")
    CRITICAL = ("critical", "严重")


class ExtensionPoint(LabeledEnum):
    SPEC_EXPORT = ("spec-export", "导出接口描述")
    AUTHZ_ENDPOINTS = ("authz-endpoints", "端点所需能力")
    AUTHZ_ROLES = ("authz-roles", "角色具备的能力")
    ERROR_TRACKING = ("error-tracking", "读取错误追踪平台")
    LOG_PLATFORM = ("log-platform", "查询集中日志平台")
    LOG_PARSE = ("log-parse", "解析日志")
    ALERT_SOURCE = ("alert-source", "读取业务告警")
    STATIC_TOOLS = ("static-tools", "技术栈检查工具")
    PAGE_ROUTES = ("page-routes", "前端页面路由")
    LOCAL_RUN = ("local-run", "本机启动计划")
    DEPLOY_SOURCE = ("deploy-source", "读取部署记录")


class ExtensionLayer(LabeledEnum):
    """扩展点的实现来自哪一层。"""

    PROJECT = ("project", "项目扩展")
    STACK = ("stack", "技术栈扩展")
    CORE = ("core", "核心方法")
    DEFAULT = ("default", "核心默认")


class ExtensionMode(LabeledEnum):
    REPLACE = ("replace", "替换下一层")
    EXTEND = ("extend", "在下一层的结果上修改或补充")


class ExtensionErrorCode(LabeledEnum):
    """前六个由扩展在响应中给出，后四个由核心判定(architecture/10 2.4)。"""

    INVALID_INPUT = ("invalid-input", "请求或 options 不合法")
    NOT_APPLICABLE = ("not-applicable", "代码中没有要读取的内容")
    TOOL_MISSING = ("tool-missing", "所需外部工具未安装或版本不符")
    BUILD_FAILED = ("build-failed", "构建失败")
    SOURCE_UNAVAILABLE = ("source-unavailable", "数据来源不可访问")
    PARSE_FAILED = ("parse-failed", "读到了内容但无法解析")
    TIMEOUT = ("timeout", "超时")
    CRASHED = ("crashed", "异常退出且没有合法的响应")
    PROTOCOL_ERROR = ("protocol-error", "输出不符合协议")
    SCHEMA_INVALID = ("schema-invalid", "输出不符合 schema")


# 状态机事件与副作用(architecture/01 2.3)

class ProblemEvent(LabeledEnum):
    REPRODUCED = ("reproduced", "复现确认有效")
    NOT_REPRODUCED = ("not-reproduced", "复现确认未通过")
    PROMOTED = ("promoted", "间歇出现的问题再次出现")
    SEEN_AGAIN = ("seen-again", "再次出现")
    COVERED_RUN_WITHOUT_OCCURRENCE = ("covered-run-without-occurrence", "覆盖运行中未出现")
    RESOLVED_ON_NEW_COMMIT = ("resolved-on-new-commit", "新 commit 上未命中")
    REGRESSION_CHECK_FAILED = ("regression-check-failed", "复现检查失败")
    TRIAGED = ("triaged", "分诊完成")
    USER_IGNORED = ("user-ignored", "用户忽略")
    USER_FALSE_POSITIVE = ("user-false-positive", "用户判为误报")
    USER_REOPENED = ("user-reopened", "用户重新打开")
    IGNORE_EXPIRED = ("ignore-expired", "忽略的恢复条件满足")
    MERGED = ("merged", "并入其他问题")
    REBUILT = ("rebuilt", "重放时拆出")
    OVERRIDDEN = ("overridden", "用户改判")
    RETRIAGE_REQUESTED = ("retriage-requested", "需要重新分诊")
    ISSUE_CLOSED = ("issue-closed", "关联 Issue 关闭")


class ProblemEffect(LabeledEnum):
    SET_IGNORE_UNTIL = ("set-ignore-until", "写入恢复条件")
    CLEAR_IGNORE_UNTIL = ("clear-ignore-until", "清除恢复条件")
    RESET_CLEAN_RUNS = ("reset-clean-runs", "清零覆盖运行计数")
    CREATE_SUPPRESSION = ("create-suppression", "生成抑制规则")
    MARK_INTERMITTENT = ("mark-intermittent", "标记间歇出现")
    RECORD_RESOLVED_RELEASE = ("record-resolved-release", "记录解决时的 commit")
    ISSUE_REGRESSED = ("issue-regressed", "关联 Issue 按回归处理")
    MERGE_INTO = ("merge-into", "并入目标问题")
    MOVE_TO_DUPLICATE_ISSUE = ("move-to-duplicate-issue", "改挂到被重复的 Issue")


class IssueEvent(LabeledEnum):
    APPROVE = ("approve", "放行")
    FIX_STARTED = ("fix-started", "开始修复")
    NOT_REPRODUCED = ("not-reproduced", "修复前复现不了")
    FIX_HELD = ("fix-held", "修复转人工")
    FIX_DONE = ("fix-done", "修复完成")
    VERIFY_PASSED = ("verify-passed", "合并前验证通过")
    VERIFY_FAILED = ("verify-failed", "合并前验证失败")
    MAIN_MERGED = ("main-merged", "合并了 origin/main")
    PR_CREATED = ("pr-created", "PR 已创建")
    PR_MERGED = ("pr-merged", "PR 已合并")
    PR_CLOSED = ("pr-closed", "PR 关闭且未合并")
    STAGING_VERIFIED = ("staging-verified", "部署后确认通过")
    STAGING_FAILED = ("staging-failed", "部署后确认发现回归")
    PROBLEM_REGRESSED = ("problem-regressed", "关联问题回归")
    USER_CLOSED = ("user-closed", "用户关闭")
    USER_REOPENED = ("user-reopened", "用户重新打开")
    RESTART = ("restart", "从某一步重来")


class IssueEffect(LabeledEnum):
    SET_HOLD = ("set-hold", "设置转人工标记")
    CLEAR_HOLD = ("clear-hold", "清除转人工标记")
    CLOSE = ("close", "写入关闭原因")
    REOPEN = ("reopen", "清除关闭原因")
    REQUEST_RETRIAGE = ("request-retriage", "关联问题重新分诊")
    SYNC_PROBLEMS = ("sync-problems", "按关闭原因同步关联问题")
    FILL_TRIAGE_OUTCOME = ("fill-triage-outcome", "回填分诊结论的实际结果")
    SET_PHASE = ("set-phase", "设置进行中的细分")


# 判定结果


class ReproduceStrategy(LabeledEnum):
    REPLAY = ("replay", "重放请求")
    IMMEDIATE = ("immediate", "直接视为有效")


class IssueLabel(LabeledEnum):
    DISCUSS_WITH_AUTHOR = ("discuss-with-author", "需要先与代码作者讨论")


class Continuation(LabeledEnum):
    AUTO = ("auto", "能")
    INTERACTIVE = ("interactive", "能，进入交互会话")
    CONFIRM_EACH = ("confirm-each", "能，逐次确认")
    WAIT_DEPLOY = ("wait-deploy", "能，但要等部署完成")
    USER = ("user", "否，等待用户")
    NONE = ("none", "无后续步骤")


ALL_ENUMS: tuple[type[LabeledEnum], ...] = (
    Source, Probe, ProbeLevel, SignalAggregateState, ProblemStatus, Verdict, Severity, Complexity, ImpactKind,
    WorthRecommendation, TaskType, SizeTier, Treatment, Lane, Disposition, TriageOutcome,
    IssueStatus, IssuePhase, IssueOrigin, CloseReason, ReviewCategory, ReviewMode, ReviewFindingKind, FixRiskLevel,
    RiskCategory,
    VerifyPhase, CheckResult,
    RegressionKind, RegressionResult, DeploymentStatus, OperationKind,
    OperationExecutor, OperationStatus, Stage, RunStage, RunStatus, HandoffStatus,
    RunnerStatus, Access, ViolationKind, AgentSessionStatus, ScoreResult, ScoreMethod,
    EvalVerdict, EvalCaseCategory,
    SuggestionKind, SuggestionStatus, YieldOutcome, KnowledgeType, KnowledgeStatus,
    KnowledgeWriteDecision, ProblemEvent, ProblemEffect, IssueEvent, IssueEffect,
    ReproduceStrategy, IssueLabel, Continuation, LogLevel, ExtensionPoint,
    ExtensionLayer, ExtensionMode, ExtensionErrorCode,
)


# 知识检索(architecture/03 1.4)

class ContextKind(LabeledEnum):
    STATIC_REVIEW = ("static-review", "静态巡检的增量审查")
    TRIAGE = ("triage", "分诊")
    FIX = ("fix", "修复")


ALL_ENUMS = (*ALL_ENUMS, ContextKind)
