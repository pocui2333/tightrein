# tightrein

[![test](https://img.shields.io/github/actions/workflow/status/pocui2333/tightrein/test.yml?branch=main&label=test)](https://github.com/pocui2333/tightrein/actions/workflows/test.yml) [![license](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE) [![python](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/)

[简体中文](README.md) | English

**Autonomous defect discovery and controlled AI repair pipeline.**

tightrein runs a continuous, closed-loop pipeline: detects application defects, triages them against evidence, repairs them with AI coding agents ([Claude Code](https://docs.claude.com/en/docs/claude-code), [Codex CLI](https://github.com/openai/codex), Antigravity CLI) in isolated worktrees, verifies the fixes, and submits pull requests. Every step operates inside strictly enforced boundaries: which commands agents may run, maximum diff size, budget caps, and which high-risk actions require human sign-off.

> [!WARNING]
> tightrein is alpha software that interacts directly with your codebase and environments. We recommend starting in a staging environment, using read-only credentials, and retaining the default human approval gates.

## Highlights

- **Multi-source signals & evidence triage**: Ingests signals from API fuzzing ([Schemathesis](https://schemathesis.io/)), static checks ([Semgrep](https://semgrep.dev/) and LLM review), Sentry, Loki, Alertmanager, and custom project probes. Deduplicates signals and verifies validity via an independent adversarial model before filing an issue.
- **Reproduction-first**: Every bug fix begins with generating a reproduction test that fails on the current codebase (Red) and must pass after the fix (Green).
- **Enforced execution boundaries**: Command allowlists, credential-stripped agent environments, and isolated read-only worktrees with pre/post-run git and file snapshot comparisons.
- **Small, reviewable PRs**: Hard default limits of at most 5 files and 200 lines (excluding tests) per PR to eliminate rogue refactorings; larger tasks are split into sub-issues.
- **Human approval gates**: Approval required for issue dispatch, fix plans, and high-risk merges; each gate is individually configurable, but high-risk paths always require human confirmation.
- **Budgets and circuit breakers**: Spending caps per run, day, and week. Automatically halts and delegates to human review upon repeated failures or lack of progress.
- **Heterogeneous agent reviews**: Configurable agent tools and models per role; reviewer and author agents must use distinct models to prevent self-confirmation bias.
- **Outcome-driven learning**: Insights from successful repairs and false positives feed back into rules and reusable skills.

## How it works

The pipeline consists of 8 strictly governed stages:

| Stage | Identifier | Description |
|---|---|---|
| Collect | `collect` | Runs configured data sources and probes to capture runtime signals. |
| Aggregate | `aggregate` | Normalizes and deduplicates signals into problems based on fingerprints and stacktraces. |
| Triage | `triage` | Gathers evidence to confirm validity and assigns severity and treatment (fix now, schedule, observe, drop). |
| Issue | `issue` | Writes structured Markdown issues locally, with optional mirroring to GitHub Issues. |
| Fix | `fix` | Plans changes, writes reproduction tests, and applies minimal patches in an isolated worktree. |
| Verify | `verify` | Re-executes reproduction test suites and checks formatting and diff-bloat thresholds. |
| Release | `release` | Creates branches, commits, opens PRs, tracks deployments, and proposes reverts upon regressions. |
| Learn | `learn` | Aggregates metrics, extracts lessons, and suggests rule and prompt improvements. |

Runs can be triggered manually, scheduled via launchd, or dispatched by code commit and deployment events. Pending decisions are gathered in the approval list: `tightrein status`.

## Requirements

- Python 3.12+, git, authenticated [GitHub CLI (`gh`)](https://cli.github.com/)
- At least one configured agent tool: Claude Code, Codex CLI, or Antigravity CLI

Platform support:

| Feature | macOS | Linux / Docker |
|---|---|---|
| Credential storage (Sentry, Loki, etc.) | System Keychain | Environment variables |
| Scheduled runs (`tightrein project schedule install`) | launchd | cron / systemd |
| Desktop notifications | Native osascript notifications | Disable (`notify: {method: none}`) |

Native Windows is not currently supported; recommended to run under WSL2.

## Installation

```sh
git clone https://github.com/pocui2333/tightrein.git
cd tightrein
python3 -m venv core/.venv
core/.venv/bin/pip install -e core
core/.venv/bin/tightrein admin install    # Registers skills and links the tightrein command into ~/.local/bin
```

With `~/.local/bin` on your `PATH`, run `tightrein` from any directory; otherwise add `core/.venv/bin` to your `PATH`. `tightrein --help` lists every command; see the [command reference](docs/reference/cli.md).

## Quick start

**1. Configure models.** Create `~/.config/tightrein/config.yaml`:

```yaml
agents:
  defaultTool: claude
  stages:
    triage:
      refuter: {capability: standard}   # Reviewer must use a different model
    fix:
      review:
        deep: {capability: standard}
  capabilities:
    light:    {claude: {model: haiku, inputUsdPerMTok: 1, outputUsdPerMTok: 5}}
    standard: {claude: {model: sonnet, inputUsdPerMTok: 3, outputUsdPerMTok: 15}}
    strong:   {claude: {model: opus, effort: high, inputUsdPerMTok: 5, outputUsdPerMTok: 25}}
```

**2. Onboard a project.** Workspaces reside under `workspaces/` (git-ignored). tightrein only performs read-only checks until onboarding is complete.

```sh
tightrein project init --workspace workspaces/my-app --repo ~/code/my-app
tightrein project worktree init --workspace workspaces/my-app     # Request read-only worktree; approve via tightrein approve
tightrein status --workspace workspaces/my-app           # Inspect onboarding questions and recommended commands
```

**3. Run.** Trigger the first run manually; background schedules can take over subsequently.

```sh
tightrein run --workspace workspaces/my-app --select collect+ --probe static --dry-run   # Dry run: preview planned steps
tightrein run --workspace workspaces/my-app --select collect+ --probe static             # Run collection and inspection
tightrein watch                                                                         # Live monitor in another terminal
tightrein project schedule install                                                              # Optional: scheduled weekday runs on macOS
```

See the [first-run tutorial](docs/tutorials/first-run.md) for a full walkthrough.

## Documentation

The CLI output and reference documentation are currently in Chinese.

- [Tutorial: first run](docs/tutorials/first-run.md)
- [Configuration and behavior](docs/reference/configuration.md): sources, approval gates, budgets, GitHub mirroring, fix lanes, agent tools and models
- [Reference](docs/reference/README.md): source methods, handoff documents, project probes
- [How-to guides](docs/how-to/README.md)
- [Design](docs/explanation/README.md)
