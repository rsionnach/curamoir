<div align="center">

# cúramóir

*Irish: **caretaker** — from **cúram**, care, charge, responsibility.*<br>
*"Faoi mo chúram": in my charge.* &nbsp;·&nbsp; Pronounced roughly **KOO-rum-ore**.

</div>

**A Claude Code plugin that looks after an agent-written codebase while you're away, and briefs you when you come back.** Every correction you make twice becomes a rule the agent can't break.

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Status: pre-release](https://img.shields.io/badge/Status-pre--release-orange.svg)](#current-status)
[![Claude Code plugin](https://img.shields.io/badge/Claude%20Code-plugin-8A63D2.svg)](#install)

The ASCII name, used for the plugin, the commands and the `.curamoir/` directory, is `curamoir`.

## The problem

You come back to a repository after three weeks. You don't remember what you
were doing, the agent you left behind has merged things you haven't read, and
the first forty minutes go on working out where you were. That cost comes from
*re-entering* a codebase, not from building it, and cúramóir exists to remove it.

Spec Kit, Superpowers, BMAD, GSD and the other popular agentic workflows are all
*creation* pipelines. Each one assumes the operator is always there. None of them
says what happens to a repository between sessions:

- **Entropy accumulates.** Agent-written code drifts from the conventions you
  set. Every session adds a little, and nobody is watching between sessions.
- **Corrections don't stick.** You tell the agent the same thing in March and
  again in April. The correction lives in a chat transcript, not in the repo.
- **Re-entry is expensive.** The context you had when you stopped is gone, and
  rebuilding it from `git log` costs the whole first evening back.

## What cúramóir does

cúramóir starts after merge and stays there. One agent works on a cadence and
keeps the codebase steady while you're absent. When you return, it hands you a
brief.

It is built for **solo developers who work in bursts**. Some weeks that means an
evening every day, some weeks nothing at all. It has to be useful after a
month away, and it must never need tending.

### 1. Corrections climb a ladder until they can't be broken

The core idea is a **promotion ladder**. A correction you keep making starts as
a written principle. Each time it recurs, it climbs:

| Tier | What it is | Who enforces it |
|---|---|---|
| **Principle** | A rule written in `.curamoir/principles.md`, with an id, a `why` and examples | A human, or a review pass |
| **Rule** | A machine-checkable lint rule with an agent-readable message and a fix pattern | `curamoir-lint`, in CI and pre-commit |
| **Guard** | A hook that blocks the edit that would break the rule | A Claude Code `PreToolUse` hook |

Once the sweep has fixed the same violation enough times, the principle becomes
a promotion candidate. A rule that keeps firing becomes a guard. The ladder runs
down as well as up: demotions are recorded, so a rule nobody believes in can be
retired rather than argued with in every review.

cúramóir enforces *your project's own* principles. It is not a general linter.

### 2. A cadence that holds the line

- **Sweep:** a cheap-tier scheduled run that fixes mechanical principle
  violations and opens a PR.
- **Review (r5):** a deep-tier run of single-lens reviewers (Correctness, Edge
  Cases, Clarity, Excellence) over one slice of the codebase. It files findings
  and changes no code.
- **Grades:** a deterministic A–F score for each module, so you can see drift
  without reading a diff.

### 3. Re-entry costs one short read

- **Handoff:** a note written automatically at session end. It records state,
  the last decision and the next step.
- **Away-brief:** everything that happened since your last session, with one
  suggested first task.

## Current status

> **Pre-release, under construction.** These work today: the guardrails, the
> plugin skeleton, session handoffs, the away-brief, the principles file with
> its rule format and bundled example rules (`examples/`), and `curamoir-lint`,
> which runs your rules standalone in CI or pre-commit (see
> [Writing a rule](docs/rules-authoring.md) and
> [Running `curamoir-lint`](docs/curamoir-lint.md)). So
> `/curamoir:init`, `/curamoir:status`, `/curamoir:doctor`, `/curamoir:handoff`
> and `/curamoir:brief` all do something real, and so does the lint gate. The
> sweep works too: `scripts/curamoir run sweep --repo <root>` runs behind the
> guardrails, fixes the violations the lint and the tier-1 scan find in a
> disposable clone, runs your tests, and opens at most one PR. Still to come
> are the r5 review, grades, the promotion commands and `/curamoir:schedule`,
> which together are most of the reason to want this. **Nothing is scheduled
> for you yet:** `curamoir run` is the one entry point a cron entry or CI job
> calls, but you write that entry yourself.

cúramóir is built with cúramóir. Its own principles are in
[`.curamoir/principles.md`](.curamoir/principles.md), and its handoffs are in
[`.curamoir/handoff/`](.curamoir/handoff/).

## Install

Prerequisites:

- **Python 3.12 or newer** as `python3` (or `python3.12` and up) on PATH,
  with PyYAML; `/curamoir:doctor` reports either missing.
- **GitHub's `gh` CLI, authenticated** (`gh auth login`). Every scheduled
  job asks it whether the repository is paused (the `curamoir:paused`
  label), and the sweep uses it to count open PRs and to open its own.
- **The `claude` CLI**, which the sweep's fix step runs headless.
- **`ast-grep`**, if any of your rules use `matcher: ast-grep`.
- **`bd` (beads)**, optional: with it, findings are filed as beads too;
  without it they go to GitHub only, with a ledger note.

```bash
claude plugin marketplace add rsionnach/curamoir
claude plugin install curamoir@curamoir
```

Then, inside a repository:

```
/curamoir:init      # scaffold .curamoir/, detecting language and module layout
/curamoir:doctor    # check the install and every hook
/curamoir:status    # see where things stand — no model calls
```

For development, load the plugin in place:

```bash
claude --plugin-dir /path/to/curamoir
```

## Commands

Every command is namespaced under the plugin, so each one reads `/curamoir:<name>`.

| Command | What it does | |
|---|---|---|
| `/curamoir:init` | Scaffold `.curamoir/` in a repo, detecting language and module layout | **working** |
| `/curamoir:status` | Lock, pause, spend, grades, next jobs, open PRs, handoff age — no model calls | **working** |
| `/curamoir:doctor` | Validate config, run every hook, report actionable fixes | **working** |
| `/curamoir:handoff` | Write a session handoff by hand, with a note and an intended next step | **working** |
| `/curamoir:brief` | Generate the away-brief on demand | **working** |
| `/curamoir:review` | Run the r5 review on a module interactively | planned |
| `/curamoir:promote` | Promote a principle to a rule, or a rule to a guard | planned |
| `/curamoir:demote` | Retire a rule or guard, recorded | planned |
| `/curamoir:grade` | Print the per-module grade table | planned |
| `/curamoir:cost` | Month-to-date and trailing-90-day spend, projected month-end | planned |
| `/curamoir:sync` | Reconcile beads against GitHub issues | planned |
| `/curamoir:smoke` | Run the quickstart smoke test on demand | planned |
| `/curamoir:schedule` | Write a local cron or launchd entry for the scheduled jobs | planned |

The table doubles as the checklist. Rows switch to **working** as their epics land.

## Guardrails

An agent nobody is watching has to be predictable, so the guardrails shipped
before any of the features:

- **It never writes to your default branch.** Work goes to `curamoir/*` branches
  and arrives as a PR. This is enforced in code, not in a prompt.
- **One job at a time**, via a supervisor lock. A stale lock is reported, never
  silently overridden.
- **A kill switch.** `touch .curamoir/PAUSE` and every scheduled job stops.
- **A spend ceiling.** Month-to-date cost is computed from the ledger before
  each run. If a run would overspend, it refuses to start.
- **Destructive commands are blocked.** During a job, `git reset --hard`,
  `git push --force`, `rm -rf` and similar commands are refused, and each refusal
  names the safe alternative.
- **An open-PR cap.** You will never come back to a wall of PRs.
- **Redaction** on everything it writes.

It has four known limits:

- **Guards are a ratchet against drift, not a security boundary.** A Claude Code
  hook that times out fails *open*, so a guard that hangs stops protecting.
- **The command policy stops code written at call time, not code written to a
  file.** During a job it refuses inline code (`bash -c`, `python -c`,
  `node -e`, `make --eval`), including through a runner such as `uv run` or
  `docker run`; packages fetched and run (`pnpm dlx`, `uv tool run`); and git
  settings or environment variables whose value is a command (`core.pager`,
  `GIT_SSH_COMMAND`). It does not stop the agent writing a file and running it
  through an allowed runner (`pytest`, `make`, `go run`, a script). The sweep's
  scope review of every changed file is what bounds that.
- **Redaction is pattern-based.** It catches the shapes it knows. That reduces
  exposure but doesn't guarantee nothing leaks.
- **The command denylist knows a job only by its signals.** It enforces only
  inside a cúramóir job, which it detects from the `CURAMOIR_JOB` environment
  variable that `curamoir run` sets for the job and everything it starts, with
  the supervisor lock that `curamoir run` holds as a fallback. Because the
  lock is per-repository, while a job runs every session in that repository
  is treated as inside it, yours included. In a clone of the repository,
  where there is no lock, the variable is the only signal, so anything that
  drops it from a process's environment stops enforcement there. A stale lock,
  left by a job that was killed, keeps the denylist on in your own sessions
  until you clear it; the denial says so, and `/curamoir:status` and
  `/curamoir:doctor` say whether the lock looks stale. The sweep's fix step
  runs Claude Code in restricted mode, which loads no plugin, so it loads the
  denylist hook through its own settings (it has no Bash tool today). Any
  non-empty `CURAMOIR_JOB` counts, `0` and `false` included, so do not export
  it in your own shell.

## What it deliberately does not do

- Planning, brainstorming, spec authoring or feature implementation. Use
  Superpowers, Spec Kit or a chat for those.
- A task graph of its own. cúramóir reads and writes beads (`bd`) and GitHub
  issues; it does not replace them.
- Multi-agent coordination, mailboxes or swarm orchestration. There is exactly
  one supervisor per repo.
- General bug scanning or linting. Use `ubs`, `ruff`, `eslint` and the like for
  that.
- Any opinion about the creation pipeline upstream of merge.
- Telemetry that leaves the machine. All metrics are local files in the repo.

## Cost

Local mode is the default. It uses your existing Claude subscription, so it
costs nothing beyond that. CI mode costs API money, because subscription auth
isn't available there.

A measured cost table will replace this paragraph before release. The current
estimate for CI mode on a small repo is roughly $4–5 a month against a default
$15 ceiling. That figure is an estimate, not a measurement.

## Licence

MIT. See [LICENSE](LICENSE).
