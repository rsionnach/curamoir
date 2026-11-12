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
> sweep works too: behind the guardrails, it fixes the violations the lint
> and the tier-1 scan find in a disposable clone, runs your tests, and opens
> at most one PR.
>
> The sweep is scheduled on a hosted runner, a disposable VM: the GitHub
> Actions workflow `/curamoir:init` offers to write, or a Claude Code Routine
> whose prompt and `/schedule` steps `/curamoir:routine` shows. Use one runner
> per repository: the two share no lock and no ledger. The local adapter
> counts as a runner too: with a hosted runner, remove any cron or launchd
> entry from an earlier setup (see [Local adapter](#local-adapter)). Neither
> hosted runner has yet been exercised end to end. The Routine's cloud
> environment (setup script, variables, network level, branch protection) is in
> [Runners](docs/runners.md#routine-environment), and `/curamoir:routine`
> shows it too. `scripts/curamoir run sweep --repo <root>`
> still runs the sweep on your own machine, as the
> [local adapter](#local-adapter). Why the hosted runner is the default, and
> what each runner guards: [Platform-first runners](docs/direction-platform-first.md).
> How a scheduler or another agent CLI plugs in, with the workflow as the
> worked example: [Runners](docs/runners.md).
>
> Still to come are the r5 review, grades and the promotion commands, which
> together are most of the reason to want this.

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
- **An OS sandbox, to run the sweep on your own machine**: macOS's built-in
  `sandbox-exec`, or on Linux bubblewrap (`bwrap`) with unprivileged user
  namespaces enabled (on Ubuntu 24.04, install the `bubblewrap` package,
  whose AppArmor profile allows them). Without one, a local sweep refuses
  to run your tests on its fixes, so it opens no PR; other platforms are
  not supported. A hosted runner needs none: the VM is the boundary there.

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

For the scheduled sweep on GitHub Actions, accept the workflow
`/curamoir:init` offers. Its header lists what the repository needs: the
`ANTHROPIC_API_KEY` secret, the `CURAMOIR_REF` variable, the setting that
lets Actions open PRs, and, if a ruleset covers the state branch
(`curamoir/state` unless the `CURAMOIR_STATE_BRANCH` variable names
another), a rule letting `github-actions[bot]` push to it. It runs on
GitHub-hosted runners only: on a self-hosted runner, or on GitHub
Enterprise Server, the sweep is refused with reason `runner`, and the job
still shows green, with a warning. Run the local adapter there instead.

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

The table doubles as the checklist. Rows switch to **working** as their epics land.
`/curamoir:routine`, which shows the Routine prompt and its `/schedule` steps,
is a helper and has no row.

## Guardrails

An agent nobody is watching has to be predictable, so the guardrails shipped
before any of the features. These are built for every runner; where a
runner weakens one, the bullet and the table below say so:

- **It never writes to your default branch.** Work goes to `curamoir/*` branches
  and arrives as a PR. This is enforced in code, not in a prompt.
- **One sweep at a time**, where the runner can enforce it. A Routine
  cannot (see the table's One sweep at a time row).
- **A kill switch.** The `curamoir:paused` label, or a `.curamoir/PAUSE`
  file, stops every scheduled job. A label that cannot be read does not
  pause: the run goes ahead, and its ledger line says the label was not
  checked.
- **A spend ceiling.** Month-to-date cost is computed from the ledger before
  each run. If a run would overspend, it refuses to start. It does not yet
  hold on a hosted runner (the table's Spend row).
- **Destructive mistakes are caught.** During a job, `git reset --hard`,
  `git push --force`, `rm -rf`, a redirect that empties its own input and
  similar commands are refused, and each refusal names the safe alternative.
  This guard catches an agent's honest mistakes; it is not what keeps a
  hostile agent in (see below).
- **An open-PR cap.** You will never come back to a wall of PRs.
- **It works in a throwaway clone, never your checkout.** The git cúramóir
  runs there is hardened: no hooks, filters, diff drivers, pager or other
  configuration the clone could plant ever run, and a planted `.git` or
  `.gitattributes`, in any case, refuses the run. Only the reviewed patch
  of the files in scope leaves the clone; anything else the agent touched is
  put back or refuses the run.
- **The repository's own Claude Code configuration never reaches the scan
  or the fix agent.** Both run Claude Code in restricted mode, so the
  clone's settings, hooks and plugins are not read; the command guard above
  comes back through cúramóir's own settings. A Routine is the exception
  for its own session: see [Hosted runner limits](#hosted-runner-limits).
- **Redaction** on everything it writes.

### Where the security boundary is

What contains an agent that has gone wrong, by accident or because something
it read told it to, is not the command guard but the runner. Which runner a
run is on is declared, never guessed: `CURAMOIR_RUNNER=hosted`, which the
workflow and the Routine prompt set, is accepted only where a GitHub-hosted
Actions runner's or a Claude Code cloud session's own environment markers
are set, and refused where they are absent. The check catches a mistaken
declaration; it does not prove where the run is. Unset, the run is local.
Each runner, as built:

| | Hosted (GitHub Actions, Claude Code Routine) | Local (your machine) |
|---|---|---|
| Process boundary | The VM, discarded after the run | An OS sandbox (`sandbox-exec`, `bwrap`) around your tests, self-tested before every gate; missing or failing, the sweep refuses |
| Your checkout | Not on the VM | Out of reach: the work happens in a throwaway clone |
| Credentials at risk | What the run holds: on Actions its `GITHUB_TOKEN`, scoped by the workflow's `permissions:`; on a Routine the session's GitHub access, which acts as you; and, on both, the run's model credentials | Your home directory |
| Agents' tools | Scan: Read, Grep, Glob. Fix: Read, Grep, Glob, Edit; Bash limited to the repository's own test command and Claude Code's built-in read-only commands (no Bash at all if that command cannot be named safely); git denied | Scan: Read, Grep, Glob. Fix: those and Edit; no shell |
| Your tests | Run unsandboxed in the clone, with the VM's network | Run in the OS sandbox, with no network: on macOS not even loopback unless `sweep.test_loopback: true`, on Linux a private loopback only |
| One sweep at a time | On Actions, the workflow's concurrency group queues a second run. On a Routine nothing does: each run's lock is on its own VM, so a scheduled run and **Run now** can overlap; don't start a run while one is going | The supervisor lock ([Local adapter](#local-adapter)) |
| Kill switch | The label, or a `.curamoir/PAUSE` committed to the default branch (a run started by hand on another branch reads that branch's) | The label, or `.curamoir/PAUSE` in your checkout |
| Spend | The ledger records tokens; GitHub Actions bills the API key, a Routine your Claude usage. The monthly ceiling does not yet see earlier hosted runs, so it does not stop them | The ledger prices tokens against the monthly ceiling |
| Memory between runs | Only what is committed. Ledger lines go to a state branch, never the default branch. Workflow: `curamoir/state` (its `CURAMOIR_STATE_BRANCH` variable can name another). Routine: `claude/curamoir-state` (the prompt's own name, so the two runners' ledgers never mix) | The checkout |

### Hosted runner limits

The fix agent's prompt on a hosted runner forbids git, `bd`, `gh`, installs
and the network, but the prompt is not the fence: a git command that writes
the clone's `.git` (or moves HEAD) refuses the whole run, and a stray file
is dropped from the patch.

Past that, the boundary is the disposable VM and the scope of the run's
token, nothing finer. Your tests run there unsandboxed, as the runner's
user. The fix agent's process is not given the run's GitHub and Actions
tokens, but the fix agent's Bash, and code the tests run, can still read
what the sweep holds (on Actions, the run's `GITHUB_TOKEN` and
`ANTHROPIC_API_KEY`), and a process either leaves behind can reach the
run's later steps, the PR included. This is not solved yet. Keep the
workflow's `permissions:` as written. Add no other secret to the sweep
step's `env:`. The agent is kept from these names only, so a secret under
any other name reaches it: `GITHUB_TOKEN`, `GH_TOKEN`,
`GH_ENTERPRISE_TOKEN`, `GITHUB_ENTERPRISE_TOKEN`, `ACTIONS_RUNTIME_TOKEN`,
`ACTIONS_RUNTIME_URL`, `ACTIONS_ID_TOKEN_REQUEST_TOKEN`,
`ACTIONS_ID_TOKEN_REQUEST_URL`, `ACTIONS_CACHE_URL` and
`ACTIONS_RESULTS_URL`.

On a Routine, the session that runs the sweep is itself a Claude Code
session in a clone of your repository, so it loads the default branch's
`CLAUDE.md`, settings and hooks, and they run with the session's GitHub
access and Claude credentials. The prompt tells it to ignore the
repository's instructions, but that is a prompt, not a fence: a hook still
runs. Only what is on the default branch loads, so protect it, as the
Routine recipe says.

### Local adapter

With `CURAMOIR_RUNNER` unset (or `local`), on your own machine, by hand
(`scripts/curamoir run sweep --repo <root>`) or from your own cron or
launchd entry, the sweep runs as the local adapter.

The local adapter counts as a runner. On a repository with a hosted
runner, remove any cron or launchd entry from an earlier setup, and run a
local sweep only by hand while no hosted run is going: the local lock lives
in your checkout and a hosted run's on its VM, so neither sees the other,
and the local ledger never sees hosted spend.

The local adapter runs behind these instead:

- **Your tests run OS-sandboxed**, under `sandbox-exec` (macOS) or `bwrap`
  (Linux). They can write only inside the clone and their own temporary
  directory; cannot read your home directory beyond what the run needs (the
  reused venv and interpreter, PATH, a version manager's installed versions;
  never a credential directory such as `~/.ssh` or `~/.config`); have no
  network (on macOS not even loopback unless `sweep.test_loopback: true` is
  committed, for a suite that starts a local server; on Linux a private
  loopback only); and reach no daemon's socket and, on macOS, no system
  service that acts outside the sandbox (`open`, `launchctl`, AppleEvents,
  DNS). Where the sandbox is missing or fails its self-test, the sweep
  refuses rather than run the tests unconfined. On macOS the tests can
  still read files outside your home directory.
- **No local job's agent has a shell.** Any tool beyond those in the table,
  Bash above all, is refused when the job is set up; only a hosted runner's
  fix agent gets one. With no shell, a file the agent writes is never run by
  it; the tests run it only inside the OS sandbox.
- **The agent's own `claude -p` is not OS-sandboxed.** It runs as you. Its
  reads, and any edit it attempts outside the clone, are fenced by Claude
  Code's permission rules (scoped to the clone, with every other path
  denied), which Claude Code applies to Grep and Glob on a best-effort
  basis. Nothing outside the clone is reviewed by the sweep.
- **The supervisor lock holds one job per repository.** A killed job can
  leave its lock behind. Because the lock is per repository, while a job
  runs every session in that repository is treated as inside it, yours
  included, and the command guard is on there. A stale lock, left by a job
  that was killed, is reported, never overridden, and keeps the guard on in
  your own sessions until you clear it; the denial says so, and
  `/curamoir:status` and `/curamoir:doctor` say whether the lock looks
  stale.
- **`CURAMOIR_JOB` is the guard's other signal.** `curamoir run` sets it
  for the job and everything it starts; in a clone, where there is no lock,
  it is the only one, so anything that drops it from a process's
  environment stops the guard there. Any non-empty value counts, `0` and
  `false` included, so do not export it in your own shell.

### Known limits

On every runner:

- **The command guard catches mistakes, not a hostile agent.** It refuses
  destructive commands an agent might honestly type, along with inline code
  and other constructions it cannot read, and inside a job it refuses anything
  it cannot judge. But a shell and its tools offer more routes than a list of
  rules can close, so it is deliberately not the security boundary: that is
  why no local job's agent has a shell, and why a hosted fix agent's shell
  is contained by the VM (above). A Claude Code hook that times out fails
  *open*; the guard is bounded inside its 5-second budget, and tested to be.
- **Redaction is pattern-based.** It catches the shapes it knows. That reduces
  exposure but doesn't guarantee nothing leaks.

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

The GitHub Actions workflow costs API money: its model calls are billed per
token to the `ANTHROPIC_API_KEY` it is given. (Whether a Claude subscription
may be used unattended there is an open question, so the workflow does not
wire it.) A Routine runs as you and counts against your Claude usage, as
does a sweep on your own machine. The ledger records every run's tokens.

A measured cost table will replace this paragraph before release. The current
estimate for the workflow on a small repo is roughly $4–5 a month against a
default $15 ceiling. That figure is an estimate, not a measurement.

## Licence

MIT. See [LICENSE](LICENSE).
