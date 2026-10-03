# tightrein

[![test](https://github.com/pocui2333/tightrein/actions/workflows/test.yml/badge.svg)](https://github.com/pocui2333/tightrein/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)

[简体中文](README.md) | English

**Autonomous defect discovery and repair for your codebase, with the reins in your hands.**

tightrein runs a continuous pipeline. It finds defects in your application, triages them against evidence, fixes them with AI coding agents ([Claude Code](https://docs.claude.com/en/docs/claude-code), [Codex CLI](https://github.com/openai/codex), Antigravity CLI), verifies the fixes, and opens pull requests. Every step runs inside boundaries you set: which commands agents may run, how much code they may change, how much they may spend, and which decisions need your approval.

> [!WARNING]
> tightrein is alpha software that runs AI agents against your code and environments. Start against a staging environment, with read-only platform tokens and the default approval gates.

## Highlights

- **End-to-end pipeline.** Collects signals from API fuzzing ([Schemathesis](https://schemathesis.io/)), static review ([Semgrep](https://semgrep.dev/) and LLM review), error tracking (Sentry), logs (Loki), alerts (Alertmanager), and custom project probes. It deduplicates the signals, then checks each one against evidence before filing an issue.
- **Reproduce first.** Every bug fix starts with a test that fails on the current code and must pass after the fix.
- **Human approval gates.** By default you approve issues, fix plans, pushes, and merges. Each gate can be automated individually; high-risk changes always require a human.
- **Enforced boundaries.** Command allowlists, agent environments without credentials, read-only worktrees, and git and file snapshots compared before and after every agent run.
- **Small, reviewable PRs.** At most 5 files and 200 lines per PR by default, excluding tests. Larger work is split into sub-issues.
- **Budgets and circuit breakers.** Spending limits per run, day, and week. After repeated failures or no progress, work goes back to a human.
- **Mix and match agents.** Pick a tool and model per role. A reviewer must use a different model than the agent whose work it reviews.
- **Learns from outcomes.** Lessons from fixes and false positives feed back into triage and fixing.

## How it works

| Stage | What happens |
|---|---|
| **collect** | Runs the configured sources and records raw signals. |
| **aggregate** | Normalizes and deduplicates signals into problems. |
| **triage** | Gathers evidence, decides whether the problem is real, and assigns severity, task type, size, and treatment: fix now, schedule, observe, or won't fix. |
| **issue** | Writes a Markdown issue locally. A GitHub mirror is optional. |
| **fix** | Plans the fix, writes the reproduction test, implements the change, runs the checks, and reviews it. Small, low-risk changes take a fast lane; large ones are split. |
| **verify** | Re-runs the reproduction checks before merge and after deployment. |
| **release** | Creates the branch, commit, and PR, tracks merge and deployment, and proposes a revert if a regression appears. |
| **learn** | Tracks metrics, records lessons, and suggests rule and configuration changes. |

Runs can be triggered manually, on a schedule (launchd), or by events such as a new commit or a new deployment. Decisions waiting for you are collected in a single inbox: `tightrein status`.

## Requirements

- Python 3.12+, git, and an authenticated [GitHub CLI](https://cli.github.com/)
- At least one agent tool: Claude Code, Codex CLI, or Antigravity CLI

The following features currently require macOS:

| Feature | Depends on | Without it |
|---|---|---|
| Platform tokens (read-only tokens for Sentry, Loki, Vercel, etc.) | Keychain | Don't connect those platforms |
| Scheduled runs (`tightrein schedule install`) | launchd | Run manually, or call `tightrein tick` from another scheduler |
| Desktop notifications | `osascript` | Set `notify: {method: none}` in `~/.config/tightrein/config.yaml` |

Everything else is independent of macOS but has so far been tested only on macOS. Native Windows is not supported.

## Installation

```sh
git clone https://github.com/pocui2333/tightrein.git
cd tightrein
python3 -m venv core/.venv
core/.venv/bin/pip install -e core
core/.venv/bin/tightrein install    # installs the skills into the agent tools found on this machine
```

Add `core/.venv/bin` to your `PATH`, or call `core/.venv/bin/tightrein` directly.

## Quick start

**1. Configure models.** Create `~/.config/tightrein/config.yaml`:

```yaml
agents:
  defaultTool: claude
  stages:
    triage:
      refuter: {capability: standard}   # reviewers must use a different model than the author
    fix:
      review:
        deep: {capability: standard}
  capabilities:
    light:    {claude: {model: haiku, inputUsdPerMTok: 1, outputUsdPerMTok: 5}}
    standard: {claude: {model: sonnet, inputUsdPerMTok: 3, outputUsdPerMTok: 15}}
    strong:   {claude: {model: opus, effort: high, inputUsdPerMTok: 5, outputUsdPerMTok: 25}}
```

**2. Onboard a project.** Workspaces live under `workspaces/`, which git ignores. Until onboarding is complete, tightrein only takes read-only actions.

```sh
tightrein workspace init --workspace workspaces/my-app --repo ~/code/my-app
tightrein worktree init --workspace workspaces/my-app     # request a read-only worktree; approve it with tightrein confirm
tightrein status --workspace workspaces/my-app           # open questions, recommended answers, and the commands to run
```

**3. Run.** Trigger the first scan manually; after that, scheduled runs can take over.

```sh
tightrein run --workspace workspaces/my-app --select collect+ --probe static --dry-run   # show what would run
tightrein run --workspace workspaces/my-app --select collect+ --probe static
tightrein watch                                         # live view, in another terminal
tightrein schedule install                              # optional: run on weekdays
```

For a complete walkthrough, see the [first-run tutorial](docs/tutorials/first-run.md) (in Chinese).

## Documentation

The CLI output and the documentation are currently in Chinese.

- [Tutorial: first run](docs/tutorials/first-run.md)
- [Configuration and behavior](docs/reference/configuration.md): sources, approval gates, budgets, GitHub mirroring, fix lanes, agent tools and models
- [Reference](docs/reference/README.md): source methods, handoff documents, project probes
- [How-to guides](docs/how-to/README.md)
- [Design](docs/explanation/README.md)

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). To report a vulnerability, see [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE)
