# cúramóir

You come back to a repository after three weeks. You don't remember what you
were doing, the agent you left behind has merged things you haven't read, and
the first forty minutes go on reconstructing where you were. That cost — the
cost of *re-entering* a codebase, not of building it — is what cúramóir exists
to remove.

Every popular agentic workflow is a creation pipeline that assumes a
continuously present operator. None of them has a story for what happens to a
repository between sessions. cúramóir sits after merge and stays there: one
agent, on a cadence, holding the line while you're away, handing you a brief
when you return.

**Every correction you make twice becomes a rule the agent can't break.**

> **Status: pre-release, under construction.** Epic 0 (guardrails) and the
> plugin skeleton are implemented. The jobs that use them — sweep, review,
> brief, handoff — are not built yet. Not usable in anger.

## Install

```bash
claude plugin marketplace add rsionnach/curamoir
claude plugin install curamoir@curamoir
```

For development, load it in place:

```bash
claude --plugin-dir /path/to/curamoir
```

## Commands

Each command is namespaced under the plugin, so they all read `/curamoir:<name>`.

| Command | What it does |
|---|---|
| `/curamoir:init` | Scaffold `.curamoir/` in a repo, detecting language and module layout |
| `/curamoir:status` | Lock, pause, spend, grades, next jobs, open PRs, handoff age — no model calls |
| `/curamoir:doctor` | Validate config, check hooks fire, report actionable fixes |
| `/curamoir:handoff` | Write a session handoff by hand, with an optional note |
| `/curamoir:brief` | Generate the away-brief on demand |
| `/curamoir:review` | Run the r5 review on a module interactively |
| `/curamoir:promote` | Promote a principle to a rule, or a rule to a guard |
| `/curamoir:demote` | Retire a rule or guard, recorded |
| `/curamoir:grade` | Print the per-module grade table |
| `/curamoir:cost` | Month-to-date and trailing-90-day spend, projected month-end |
| `/curamoir:sync` | Reconcile beads against GitHub issues |
| `/curamoir:smoke` | Run the quickstart smoke test on demand |
| `/curamoir:schedule` | Write a local cron or launchd entry for the scheduled jobs |

None of these are implemented yet. They land with their epics, and this table is
the checklist (E1-R6).

## What it deliberately does not do

- Planning, brainstorming, spec authoring, or feature implementation. Use
  Superpowers, Spec Kit, or a chat.
- A task graph of its own. cúramóir reads and writes beads (`bd`) and GitHub
  issues; it does not replace them.
- Multi-agent coordination, mailboxes, or swarm orchestration.
- General bug scanning or linting. cúramóir enforces *the project's own*
  principles; use `ubs`, `ruff`, `eslint` etc. for the rest.
- Any opinion about the creation pipeline upstream of merge.
- Telemetry that leaves the machine. All metrics are local files in the repo.

## Guardrails

An agent nobody is watching has to be predictable, so the guardrails shipped
before any feature:

- **Never writes to your default branch.** Work goes to `curamoir/*` branches
  and arrives as a PR. Enforced in code, not in a prompt.
- **One job at a time**, via a supervisor lock. A stale lock is reported, never
  silently overridden.
- **A kill switch.** `touch .curamoir/PAUSE` and every scheduled job stops.
- **A spend ceiling.** Month-to-date cost is computed from the ledger before
  each run; it refuses rather than overrunning.
- **Destructive commands blocked.** `git reset --hard`, `git push --force`,
  `rm -rf` and friends are refused during a job, each with the safe alternative
  named.
- **An open-PR cap.** You will never come back to a wall of PRs.
- **Redaction** on everything it writes.

Two honest limits. Guards are a ratchet against drift, not a security boundary:
a Claude Code hook that times out fails *open*, so a guard that hangs stops
protecting. And redaction is pattern-based — it catches shapes it knows, and
reduces exposure rather than guaranteeing absence.

## Cost

Local mode is the default and uses your existing subscription, so it costs
nothing beyond it. CI mode costs API money, because subscription auth isn't
available there.

A real measured cost table replaces this paragraph before release (E11-R4).
Current estimate for CI mode on a small repo is roughly $4–5/month against a
default $15 ceiling, which is an estimate and labelled as one.

## Licence

MIT.
