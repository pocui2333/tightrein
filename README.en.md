# tightrein

[![test](https://img.shields.io/github/actions/workflow/status/pocui2333/tightrein/test.yml?branch=main&label=test)](https://github.com/pocui2333/tightrein/actions/workflows/test.yml) [![license](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE) [![python](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/)

[简体中文](README.md) | English

**A pipeline that finds code defects and repairs them under control.**

tightrein runs an unattended closed loop: it looks for leads in a project's runtime data and code, gathers evidence to decide whether each one is a real problem, files the ones worth fixing as issues, hands them to AI coding agents ([Claude Code](https://docs.claude.com/en/docs/claude-code), Antigravity CLI, [Codex CLI](https://github.com/openai/codex)) in isolated worktrees, opens and merges a PR once self-checks and review pass, and confirms the fix in production where the problem was first reported. Every step runs inside strict boundaries: what agents may read and write, which commands they may run, how much they may change, how much quota they may use, and which decisions only a human can make.

> [!WARNING]
> tightrein is alpha software that lets AI agents act on your code and environments. Start with a staging environment, use read-only credentials, and keep the default human gates.

## Highlights

- **Many sources, evidence first**: leads come from project probes, error tracking (Sentry), logs (Loki), access logs, alerts (Alertmanager), API fuzzing ([Schemathesis](https://schemathesis.io/)), static review ([Semgrep](https://semgrep.dev/) plus model review) and findings made while working on other tasks. They are deduplicated into problems, and only problems confirmed by evidence become issues; high-risk ones are blind-reviewed again by a different model.
- **One implementation flow that skips what is already known**: prepare → locate → design → approve → code → check → review → deliver; the code notes left by assessment are shared by every step instead of each step re-reading the code.
- **Enforced boundaries**: read-only command allowlist, child-process environment allowlist (credentials never reach an agent), read-only worktrees, two tiers of protected files (forbidden and high-risk), and a per-PR change cap (10 files, 400 lines by default).
- **Human gates**: issue dispatch, plan approval and merging can each require a human; low-risk cases can be set to automatic, but a touched forbidden file, a high-risk merge, exceeding the change cap and decisions that need a choice always go to a human.
- **Usage bounded by subscription quota**: a reserve is kept for you (5-hour window and weekly quota), each issue has a token cap, work stops when there is no progress or after 3 rounds of revisions, and both dependencies and objects have circuit breakers.
- **Reviewer and author use different models**: refutation and deep review must use a different model from the one being reviewed, so they do not share a blind spot.
- **Resumable and traceable**: every step writes one handoff (conclusion, required facts, metrics, notes) that also serves as a checkpoint; an interrupted run continues from the checkpoint, and external writes carry idempotency keys.
- **Retro and knowledge base**: at the end of each run tightrein checks its own failures, waste, misjudgments and interruptions and records them with a rating; project conventions, defect patterns and lessons go into a knowledge base that assessment and implementation look up by location.

## How it works

Five stages:

| Stage | Name in commands | What it does |
|---|---|---|
| Collect | `collect` | Gathers leads (signals) from seven sources, then deduplicates them into problems (new or regressed) |
| Assess | `assess` | Decides whether a problem is real, sets severity (P0 to P3) and treatment; files the ones to fix as issues awaiting dispatch |
| Implement | `implement` | Changes code in a worktree, self-checks, reviews, and hands over to release |
| Release | `release` | Commits, syncs main, opens the PR, waits for CI, merges, tracks the deployment, accepts, cleans up |
| Retro | `retro` | Checks tightrein's own problems in this run and records them |

Run it by hand (`tightrein run`) or let a timer (launchd on macOS) advance it automatically, within the allowed hours, up to a configured stage. Everything that needs you is listed in `tightrein status`. See the [architecture overview](docs/explanation/overview.md) (Chinese).

## Requirements

- macOS (the timer uses launchd; other systems can run it by hand), Python 3.12+, git;
- [GitHub CLI (`gh`)](https://cli.github.com/), logged in;
- Agent tools: the default settings use Claude Code and Antigravity CLI, both signed in with a subscription; with Claude only, point a few call sites at Claude models (see the tutorial).

## Install

```sh
git clone https://github.com/pocui2333/tightrein.git
cd tightrein
python3.12 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/tightrein admin install    # checks tools and vendored skills, extends agy's read-only allowlist, links tightrein into ~/.local/bin
```

With `~/.local/bin` on your `PATH`, run `tightrein` from any directory.

## Quick start

Follow the [tutorial: first run](docs/tutorials/first-run.md) (Chinese):

```sh
tightrein project add ~/code/my-app      # create the workspace and inspect the repository
# edit setup.json and settings.json, fill in sites.json and secrets.json under workspaces/my-app/
tightrein project check                  # trial run: fetches data only, no model calls
tightrein project ready                  # mark ready and install the timer
tightrein run                            # advance one round by hand
tightrein status                         # one-off snapshot; tightrein watch for a live view
```

Output language follows the project's `language` setting (`zh` or `en`); `--lang` overrides it per command.

## Commands

13 everyday commands:

| Command | What it does |
|---|---|
| `tightrein` / `tightrein status [--json]` | One-off snapshot; the default when no command is given |
| `tightrein watch [--collect]` | Live view |
| `tightrein show <id> [--steps] [--doc pending/failure/deliver]` | Details of a problem or issue; `show --search "<words>"` finds by description |
| `tightrein approve <id> [--option <n>] [--note "<note>"]` | Approve what is awaiting review |
| `tightrein reject <id> --note "<reason>"` | Reject; the plan is redone for the given reason |
| `tightrein new "<request>" [--severity P0..P3] [--type bug/feature]` | File your own request; goes straight to assessment |
| `tightrein run [collect/assess/implement/release/retro] [--object <id>] [--dry-run]` | Run by hand: without a stage, advance one round by state |
| `tightrein pause` / `stop` / `resume` | Stop after the current step / stop now / resume |
| `tightrein take <id>` / `give <id>` | Take over by hand / hand back |

5 groups:

| Group | Subcommands |
|---|---|
| `project` | `add`, `check`, `ready`, `show`, `config`, `list`, `remove`: onboarding |
| `problem` | `list`, `mute`, `unmute`, `reopen`: view and handle problems |
| `retro` | `list`, `show`, `close`: the record book of tightrein's own problems |
| `knowledge` | `list`, `show`, `confirm`, `drop`, `add`: the project knowledge base |
| `admin` | `install`, `uninstall`, `rebuild`, `check`, `clean`: local install, self-check and maintenance |

Global options go after the command: `-p <project>`, `--json`, `--lang zh/en`, `--yes`. Full reference: [commands](docs/reference/commands.md) (Chinese).

## Repository layout

```
src/tightrein/   code (src layout): collect, assess, implement, release, retro, knowledge,
                 onboard, agents, prompts, protocol, store, settings, cli
settings/        global values: defaults.json (committed); controls.json, sites.json, secrets.json (local)
vendor/          pinned third-party skills
tests/           tests, mirroring src/tightrein/
docs/            documentation
workspaces/      per-project workspaces (not in git)
```

Every module folder has a `README.md`: what it is, flow, inputs and outputs, configuration, design rationale, and what it deliberately does not do.

## Documentation

The documentation is written in Chinese.

- [Tutorial: first run](docs/tutorials/first-run.md)
- How-to: [onboard a project](docs/how-to/onboard-project.md), [write a project probe](docs/how-to/write-project-probe.md), [add a method](docs/how-to/add-method.md)
- Reference: [commands](docs/reference/commands.md), [configuration](docs/reference/configuration.md), [handoff formats](docs/reference/handoff.md), [platform methods](docs/reference/methods.md)
- [Architecture overview](docs/explanation/overview.md)
- Contributing: [CONTRIBUTING.md](CONTRIBUTING.md)
