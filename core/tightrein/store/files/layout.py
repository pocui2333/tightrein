"""工作区内与工作区之外全部路径的唯一来源(architecture/01 4.3)，其他代码不拼接路径。

路径中的编号、角色、简称等片段必须是单个路径段，含路径分隔符或为 `.`、`..` 时抛出 ValueError，
防止外部输入把文件写到预期目录之外。
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from tightrein.domain.clock import format_iso
from tightrein.domain.enums import ExtensionPoint, KnowledgeType, Probe, Stage, VerifyPhase

TOOL_ROOT = Path(__file__).resolve().parents[4]
HANDOFF_SUFFIX = ".json"
EXCHANGE_KINDS = ("request", "response")


def segment(value: str) -> str:
    """校验并返回单个路径段。"""
    if not value or value in (".", "..") or "/" in value or "\\" in value or "\0" in value:
        raise ValueError(f"不是合法的路径段：{value!r}")
    return value


def compact_time(value: datetime) -> str:
    """文件名中的时间：UTC，形如 20260929-021503。"""
    return format_iso(value).replace("-", "").replace(":", "").replace("T", "-").rstrip("Z")


class WorkspaceLayout:
    """一个工作区 `workspaces/<项目>/` 的全部路径。

    output_dir 给出时为 `--output <目录>` 模式：运行目录(交接文档、会话记录、原始输出)与事件日志都在该目录中。
    """

    def __init__(self, root: Path, output_dir: Path | None = None) -> None:
        self.root = root
        self.output_dir = output_dir

    @property
    def project(self) -> str:
        return self.root.name

    def relative(self, path: Path) -> str:
        """相对工作区根目录的路径，用于写入数据库。"""
        return path.relative_to(self.root).as_posix()

    # 工作区中纳入版本管理的文件

    def project_config(self) -> Path:
        return self.root / "project.yaml"

    def normalize_rules(self) -> Path:
        return self.root / "normalize.yaml"

    def suppressions(self) -> Path:
        return self.root / "suppressions.yaml"

    def extensions_dir(self) -> Path:
        """项目扩展：扩展的命令文件与夹具测试，也是项目扩展运行时的工作目录。"""
        return self.root / "extensions"

    def extension_fixtures_dir(self) -> Path:
        return self.extensions_dir() / "tests" / "fixtures"

    def knowledge_dir(self) -> Path:
        return self.root / "knowledge"

    def knowledge_type_dir(self, kind: KnowledgeType) -> Path:
        return self.knowledge_dir() / kind.value

    def knowledge_file(self, kind: KnowledgeType, knowledge_id: str, slug: str) -> Path:
        return self.knowledge_type_dir(kind) / f"{segment(knowledge_id)}-{segment(slug)}.md"

    def knowledge_index(self, kind: KnowledgeType | None = None) -> Path:
        """kind 为空时是总索引。"""
        directory = self.knowledge_dir() if kind is None else self.knowledge_type_dir(kind)
        return directory / "INDEX.md"

    def knowledge_index_page(self, kind: KnowledgeType, first_id: str, last_id: str) -> Path:
        return self.knowledge_type_dir(kind) / f"INDEX-{segment(first_id)}-{segment(last_id)}.md"

    def e2e_dir(self) -> Path:
        return self.root / "e2e"

    def e2e_role_dir(self, role: str) -> Path:
        return self.e2e_dir() / segment(role)

    def regressions_dir(self) -> Path:
        return self.root / "regressions"

    def regression_dir(self, issue_id: str) -> Path:
        return self.regressions_dir() / segment(issue_id)

    def regression_checklist(self, issue_id: str) -> Path:
        return self.regression_dir(issue_id) / "check.yaml"

    def evals_dir(self) -> Path:
        return self.root / "evals"

    def eval_manifest(self) -> Path:
        return self.evals_dir() / "manifest.json"

    def eval_case_dir(self, stage: Stage, case_id: str) -> Path:
        return self.evals_dir() / stage.value / segment(case_id)

    def retrieval_cases(self) -> Path:
        return self.evals_dir() / "retrieval" / "cases.jsonl"

    def rules_dir(self) -> Path:
        """规则库：缺陷变规则验证通过的 Semgrep 规则，静态巡检的确定性工具读取。"""
        return self.root / "rules"

    def issues_dir(self) -> Path:
        return self.root / "issues"

    def issue_file(self, issue_id: str, slug: str) -> Path:
        return self.issues_dir() / f"{segment(issue_id)}-{segment(slug)}.md"

    # 不纳入版本管理的目录

    def worktrees_dir(self) -> Path:
        return self.root / "worktrees"

    def readonly_worktree(self) -> Path:
        return self.worktrees_dir() / "readonly"

    def fix_worktree(self, issue_id: str) -> Path:
        return self.worktrees_dir() / f"fix-{segment(issue_id)}"

    def data_dir(self) -> Path:
        return self.root / "data"

    def database(self) -> Path:
        return self.data_dir() / "tightrein.db"

    def aggregate_lock(self) -> Path:
        return self.data_dir() / "aggregate.lock"

    def run_lock(self) -> Path:
        return self.data_dir() / "run.lock"

    def suppressions_lock(self) -> Path:
        return self.data_dir() / "suppressions.lock"

    def readonly_guard(self, worktree_name: str) -> Path:
        return self.data_dir() / "guards" / f"readonly-{segment(worktree_name)}.json"

    def spec_dir(self, commit: str) -> Path:
        return self.data_dir() / "specs" / segment(commit)

    def openapi(self, commit: str) -> Path:
        """spec-export 写出的接口描述。"""
        return self.spec_dir(commit) / "openapi.json"

    def extension_output(self, commit: str, point: ExtensionPoint) -> Path:
        """按 commit 缓存的扩展输出(architecture/10 2.7)。"""
        return self.spec_dir(commit) / f"{point.value}.json"

    def extension_meta(self, commit: str, point: ExtensionPoint) -> Path:
        return self.spec_dir(commit) / f"{point.value}.meta.json"

    def authz_model(self, commit: str) -> Path:
        return self.spec_dir(commit) / "authz-model.json"

    def runs_dir(self) -> Path:
        return self.data_dir() / "runs"

    def run_dir(self, run_id: str) -> Path:
        if self.output_dir is not None:
            return self.output_dir
        return self.runs_dir() / segment(run_id)

    def handoff_dir(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "handoff"

    def handoff(self, run_id: str, handoff_id: str) -> Path:
        return self.handoff_dir(run_id) / f"{segment(handoff_id)}{HANDOFF_SUFFIX}"

    def handoff_history(self, run_id: str, handoff_id: str, number: int) -> Path:
        """重跑前的旧版本 `<交接文档编号>.<序号>.json`，序号从 1 开始。"""
        if number < 1:
            raise ValueError(f"序号从 1 开始：{number}")
        return self.handoff_dir(run_id) / f"{segment(handoff_id)}.{number}{HANDOFF_SUFFIX}"

    def transcripts_dir(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "transcripts"

    def transcript(self, run_id: str, role: str, subject_id: str) -> Path:
        return self.transcripts_dir(run_id) / f"{segment(role)}-{segment(subject_id)}.jsonl"

    def signals_file(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "signals.ndjson"

    def raw_dir(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "raw"

    def probe_raw_dir(self, run_id: str, probe: Probe) -> Path:
        return self.raw_dir(run_id) / probe.value

    def spec_export_log(self, run_id: str) -> Path:
        """spec-export 的构建与导出输出的保存位置。"""
        return self.probe_raw_dir(run_id, Probe.API_FUZZ) / "spec-export.log"

    def extension_raw_dir(self, run_id: str) -> Path:
        return self.raw_dir(run_id) / "extensions"

    def extension_stderr(self, run_id: str, point: ExtensionPoint, number: int = 1) -> Path:
        """扩展的标准错误输出；同一运行内同一扩展点的第 2 次起文件名带序号。"""
        if number < 1:
            raise ValueError(f"序号从 1 开始：{number}")
        suffix = "" if number == 1 else f"-{number}"
        return self.extension_raw_dir(run_id) / f"{point.value}{suffix}.stderr.log"

    def extension_trial_openapi(self, run_id: str) -> Path:
        """`ext run spec-export` 写出的接口描述；单独调用不进入按 commit 的缓存。"""
        return self.extension_raw_dir(run_id) / "openapi.json"

    def extension_exchange(self, point: ExtensionPoint, number: int, kind: str) -> Path | None:
        """`--output` 模式下扩展请求与响应的副本；不是该模式时为空。kind 为 request 或 response。"""
        if kind not in EXCHANGE_KINDS:
            raise ValueError(f"kind 只能是 {'、'.join(EXCHANGE_KINDS)}：{kind}")
        if number < 1:
            raise ValueError(f"序号从 1 开始：{number}")
        if self.output_dir is None:
            return None
        suffix = "" if number == 1 else f"-{number}"
        return self.output_dir / "extensions" / f"{point.value}{suffix}.{kind}.json"

    def runner_raw_dir(self, run_id: str, role: str, subject_id: str) -> Path:
        return self.raw_dir(run_id) / "runner" / f"{segment(role)}-{segment(subject_id)}"

    def guard_report(self, run_id: str, role: str, subject_id: str) -> Path:
        return self.raw_dir(run_id) / "guards" / f"{segment(role)}-{segment(subject_id)}.json"

    def vcs_raw_dir(self, run_id: str, operation_id: str) -> Path:
        return self.raw_dir(run_id) / "vcs" / segment(operation_id)

    def logs_dir(self) -> Path:
        return self.data_dir() / "logs"

    def events_log(self, day: date) -> Path:
        if self.output_dir is not None:
            return self.output_dir / "events.jsonl"
        return self.logs_dir() / f"events-{day.isoformat()}.jsonl"

    def launchd_out_log(self) -> Path:
        return self.logs_dir() / "launchd.out.log"

    def launchd_err_log(self) -> Path:
        return self.logs_dir() / "launchd.err.log"

    def findings_dir(self) -> Path:
        return self.data_dir() / "findings"

    def finding(self, problem_id: str) -> Path:
        return self.findings_dir() / f"{segment(problem_id)}.md"

    def _output(self) -> Path:
        if self.output_dir is None:
            raise ValueError("只有 --output 模式才有输出目录")
        return self.output_dir

    def output_finding(self, problem_id: str) -> Path:
        """--output 模式下的发现报告；检索来源依赖 finding，二者分开。"""
        return self._output() / "findings" / f"{segment(problem_id)}.md"

    def output_issue_file(self, issue_id: str, slug: str) -> Path:
        """--output 模式下的 Issue 文件。"""
        return self._output() / "issues" / f"{segment(issue_id)}-{segment(slug)}.md"

    def fixes_dir(self, issue_id: str) -> Path:
        return self.data_dir() / "fixes" / segment(issue_id)

    def fix_report(self, issue_id: str) -> Path:
        return self.fixes_dir(issue_id) / "report.md"

    def fix_file(self, issue_id: str, name: str) -> Path:
        """data/fixes/<Issue 编号>/ 下的文件：plan.json、plan.md、prepare.log、conflicts.md、pr-body.md 等。"""
        return self.fixes_dir(issue_id) / segment(name)

    def output_path(self, *parts: str) -> Path:
        """--output 模式下输出目录中的文件。"""
        return self._output().joinpath(*(segment(part) for part in parts))

    def verify_dir(self, issue_id: str, day: date, phase: VerifyPhase) -> Path:
        return self.data_dir() / "verify" / segment(issue_id) / f"{day.isoformat()}-{phase.value}"

    def run_cases_dir(self) -> Path:
        """运行即评测的用例：修复部署后确认通过时由程序保存，评测 fix 时与 evals/ 中封存的用例一起使用。"""
        return self.data_dir() / "eval" / "cases"

    def run_case(self, issue_id: str, suffix: str = ".json") -> Path:
        return self.run_cases_dir() / f"{segment(issue_id)}{suffix}"

    def eval_output_dir(self, eval_id: str) -> Path:
        return self.data_dir() / "evals" / segment(eval_id)

    def eval_plan(self, eval_id: str) -> Path:
        """评测计划、用例清单与各用例哈希、manifest 哈希、随机种子；续跑时据此校验。"""
        return self.eval_output_dir(eval_id) / "plan.json"

    def eval_scores(self, eval_id: str) -> Path:
        """每次运行一行 RunScore；有记录的运行视为已完成。"""
        return self.eval_output_dir(eval_id) / "scores.jsonl"

    def eval_report_json(self, eval_id: str) -> Path:
        return self.eval_output_dir(eval_id) / "report.json"

    def eval_report_md(self, eval_id: str) -> Path:
        return self.eval_output_dir(eval_id) / "report.md"

    def eval_version_dir(self, eval_id: str, label: str) -> Path:
        """版本快照：tightrein 仓库的文件加数据库副本。"""
        return self.eval_output_dir(eval_id) / "versions" / segment(label)

    def eval_project_snapshot(self, eval_id: str, commit: str) -> Path:
        """被测项目在某个 commit 的只读代码快照，供评审与证据核对。"""
        return self.eval_output_dir(eval_id) / "snapshots" / segment(commit)

    def eval_run_output(self, eval_id: str, variant: str, case_id: str, attempt: int) -> Path:
        """一次沙箱运行的 --output 目录。"""
        if attempt < 1:
            raise ValueError(f"次数从 1 开始：{attempt}")
        return self.eval_output_dir(eval_id) / "outputs" / segment(variant) / segment(case_id) / str(attempt)

    def reports_dir(self) -> Path:
        return self.data_dir() / "reports"

    def run_report(self, run_id: str) -> Path:
        return self.reports_dir() / f"run-{segment(run_id)}.md"

    def daily_report(self, day: date) -> Path:
        """每日汇总(progress 类型交接文档)：当天的收件箱、各次运行的产出与卡点。"""
        return self.reports_dir() / f"daily-{day.isoformat()}.md"

    def daily_state(self, day: date) -> Path:
        """每日汇总的数据：当天各次运行与历史，每次运行据此重写汇总。"""
        return self.reports_dir() / f"daily-{day.isoformat()}.json"

    def onboarding_checks_dir(self) -> Path:
        """接入时在主分支上自检检查命令的日志。"""
        return self.data_dir() / "onboarding" / "checks"

    def onboarding_document(self) -> Path:
        """接入清单(progress 类型交接文档)，在工作区根目录。"""
        return self.root / "onboarding.md"

    def improve_dir(self) -> Path:
        """改进建议的决定文档与补丁。"""
        return self.data_dir() / "improve"

    def improve_file(self, suggestion_id: str, suffix: str) -> Path:
        return self.improve_dir() / f"{segment(suggestion_id)}{suffix}"

    def weekly_report(self, day: date) -> Path:
        return self.reports_dir() / f"weekly-{day.isoformat()}.md"

    def archive_dir(self) -> Path:
        return self.data_dir() / "archive"

    def database_backup(self, at: datetime) -> Path:
        return self.archive_dir() / f"tightrein-{compact_time(at)}.db"

    def suppressions_backup(self, at: datetime, number: int = 0) -> Path:
        """写入 suppressions.yaml 前的备份；同一秒内多次备份时以 number 区分。"""
        suffix = f"-{number}" if number else ""
        return self.archive_dir() / f"suppressions-{compact_time(at)}{suffix}.yaml"


class ToolLayout:
    """本工具仓库中的路径；根目录默认为核心所在仓库的根目录。"""

    def __init__(self, root: Path = TOOL_ROOT) -> None:
        self.root = root

    def skills_dir(self) -> Path:
        return self.root / "skills"

    def skill(self, name: str) -> Path:
        return self.skills_dir() / segment(name) / "SKILL.md"

    def workspaces_dir(self) -> Path:
        return self.root / "workspaces"

    def workspace(self, project: str) -> WorkspaceLayout:
        return WorkspaceLayout(self.workspaces_dir() / segment(project))

    def stacks_dir(self) -> Path:
        return self.root / "extensions" / "stacks"

    def stack_dir(self, stack: str) -> Path:
        """技术栈扩展，也是技术栈扩展运行时的工作目录。"""
        return self.stacks_dir() / segment(stack)

    def stack_manifest(self, stack: str) -> Path:
        return self.stack_dir(stack) / "stack.yaml"

    def method_dir(self, stack: str, point: ExtensionPoint, method: str) -> Path:
        """技术栈方法所在的目录：<技术栈>/<扩展点>/<方法>/。"""
        return self.stack_dir(stack) / point.value / segment(method)

    def method_manifest(self, stack: str, point: ExtensionPoint, method: str) -> Path:
        return self.method_dir(stack, point, method) / "method.yaml"

    def stack_fixtures_dir(self, stack: str) -> Path:
        """技术栈方法的夹具：tests/fixtures/<扩展点>/<方法>/<用例>/。"""
        return self.stack_dir(stack) / "tests" / "fixtures"

    def third_party_lock(self) -> Path:
        return self.root / "third_party" / "skills.lock.yaml"

    def third_party_installed(self) -> Path:
        return self.root / "third_party" / "installed.json"

    def local_dir(self) -> Path:
        """本机依赖(Semgrep 的虚拟环境、第三方 skill 的缓存等)，被 .gitignore 忽略。"""
        return self.root / "local"

    def third_party_cache(self, name: str, commit: str) -> Path:
        """第三方 skill 按 commit 下载的缓存。"""
        return self.local_dir() / "third_party-cache" / segment(name) / segment(commit)


class UserLayout:
    """本机用户目录下的路径。"""

    def __init__(self, home: Path) -> None:
        self.home = home

    def config(self) -> Path:
        return self.home / ".config" / "tightrein" / "config.yaml"

    def cache_dir(self) -> Path:
        return self.home / ".cache" / "tightrein"

    def extension_cache(self, name: str) -> Path:
        """扩展的长期缓存，name 为技术栈名或工作区名；经 TIGHTREIN_CACHE_DIR 传给扩展。"""
        return self.cache_dir() / "extensions" / segment(name)

    def state_dir(self) -> Path:
        """本机的运行状态(不是配置)：~/.local/state/tightrein。"""
        return self.home / ".local" / "state" / "tightrein"

    def pause_flag(self) -> Path:
        """全局暂停的标记文件(tightrein pause 不带 --workspace)，内容为暂停时间与说明。"""
        return self.state_dir() / "paused"

    def launch_agent(self, project: str) -> Path:
        return self.home / "Library" / "LaunchAgents" / f"local.tightrein.{segment(project)}.plist"


def rotated_log(path: Path, number: int) -> Path:
    """轮转后的日志 `<文件名>.<序号>`，序号从 1 开始，越大越旧。"""
    if number < 1:
        raise ValueError(f"序号从 1 开始：{number}")
    return path.with_name(f"{path.name}.{number}")
