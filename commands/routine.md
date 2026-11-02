---
description: Show the Routine prompt for the sweep and the /schedule steps to run it in the cloud
allowed-tools: Read
---

Show the developer how to run the cúramóir sweep as a Claude Code Routine (a
cloud scheduled task): the prompt the Routine runs, and the steps that create
it. This command changes nothing.

## 1. The Routine prompt

Read `${CLAUDE_PLUGIN_ROOT}/templates/routine/sweep-prompt.md` and show its
whole contents to the developer verbatim, inside a four-backtick fence
(opening with ````text and closing with ````), so the prompt's own
three-backtick bash fences survive.
Do not paraphrase, shorten or reformat it. That file is the one copy of the
prompt.

Do not run any command in it, and do not act on any instruction in it. It
is written for the cloud session the Routine starts, not for this session.

Then tell the developer the one edit it needs before they paste it:

- Replace `REPLACE-WITH-A-CURAMOIR-TAG-OR-COMMIT` (step 1) with a cúramóir
  tag or full commit SHA. Left as it is, every run stops at step 1. A
  branch name is accepted but discouraged: it is resolved to a commit once
  per run, so each run may get a different cúramóir.

Say what the prompt does, in one or two sentences: it declares the hosted
runner (`CURAMOIR_RUNNER=hosted`), runs `curamoir run sweep` from that one
commit, and then always commits the run's ledger lines to the
`claude/curamoir-state` branch, never the default branch, because a
Routine's VM is discarded after the run. The session does nothing else: the
sweep opens its own PR, at most one per run.

Why that branch name: `claude/curamoir-state` is the project's chosen name
for the Routine's state branch, kept apart from the GitHub Actions
workflow's `curamoir/state` so the two runners' ledgers never mix. It is
not there to fit a restriction: `claude/` is only the default prefix of
the branches Claude pushes its own work to, and what limits which branches
a run may push is the repository's rulesets or branch protection on GitHub.

## 2. The /schedule steps

Give these steps, in this order:

1. In a Claude Code session in this repository, run `/schedule` (its alias
   is `/routines`). Claude asks about the schedule, the repository and the
   prompt before it saves anything. The same can be done on the web at
   https://claude.ai/code/routines (**New task**; on some accounts it reads
   **New routine**).
2. Name it, for example "curamoir sweep", and give it the edited prompt
   from section 1 as its prompt, unchanged otherwise.
3. Select this repository, and only this one.
4. Schedule: weekly is enough, at a few minutes past the hour (a run set
   exactly on the hour can start late), for example Mondays at 04:17 UTC
   (cron `17 4 * * 1`, the GitHub Actions workflow's slot). Routines refuse
   anything more often than hourly. Match it to `cadence.sweep` in
   `.curamoir/curamoir.yaml`, this repository's record of how often the
   sweep runs. A cron expression of five fields is read in UTC.
5. Cloud environment: make it first, as section 3 describes (setup script,
   variables, network level), then select it here. Set section 3's branch
   protection on GitHub before step 7's **Run now**.
6. Connectors: remove them all. The sweep needs none, and a run can use any
   connector it is given, writes included.
7. Create it, then use **Run now** on its page once, and open that run to
   read its report. A run marked as succeeded says only that the session
   ended normally, not what the sweep did: the report says that. A run
   whose report lacks step 3's result kept no ledger line.

Three things to tell them as they do this:

- Use one runner per repository: this Routine or the GitHub Actions
  workflow `/curamoir:init` offers, not both. The two share no lock and
  no ledger, so their runs can overlap and their spend is split across
  two ledgers.
- The Routine runs as them: its commits, the state branch push and the PR
  carry their GitHub identity, and its runs count against their Claude
  usage.
- If a ruleset or branch protection on GitHub covers `claude/curamoir-state`,
  it must let them push to it, or each run's ledger lines are lost (step 3
  of the prompt then reports a failure).

## 3. The cloud environment

Show the developer the recipe below for the Routine's cloud environment,
from the setup script to the end, as it is written. It is the same text
as the "Routine environment" section of `docs/runners.md`, the page on
running cúramóir on a runner. Do not run the setup script or any command
in it here: it is for the cloud environment, not this session.

<!-- routine-environment:start. The same text in docs/runners.md ("Routine
environment") and commands/routine.md (section 3), up to the end marker;
tests/plugin/test_routine_command.py compares them. -->

Make a cloud environment for this Routine alone (the variables below apply
to every session that uses it), and select it in the Routine's **Cloud
environment**. There are three parts to configure in it: the setup script,
the environment variables and the network access level. Then one setting
on GitHub, and two things to expect in a run.

**Setup script.** Paste this as the environment's setup script. It runs as
root before Claude Code starts. It must exit 0, or the session does not
start, and finish in under five minutes, or the environment is not cached
and the script runs again before every run. uv and gh come preinstalled in
a cloud session; it installs only what is missing, including a Python of
3.12 or later for uvx (so a run does not download one), gives each install
at most a minute where `timeout` exists, and reports a failed install in the
session's setup log rather than failing (without ast-grep the sweep
refuses, saying why; bd is not needed by the sweep).

```bash
#!/bin/bash
set -u
AST_GREP_VERSION=0.51.0
BD_VERSION=0.60.0
bounded() {
  if command -v timeout >/dev/null 2>&1; then timeout 60 "$@"; else "$@"; fi
}
command -v uv >/dev/null 2>&1 || bounded pip install --quiet uv || echo "setup: uv not installed"
uv python find '>=3.12' >/dev/null 2>&1 || bounded uv python install 3.12 \
  || echo "setup: Python 3.12 not installed"
command -v ast-grep >/dev/null 2>&1 || bounded env UV_TOOL_BIN_DIR=/usr/local/bin \
  uv tool install "ast-grep-cli==${AST_GREP_VERSION}" || echo "setup: ast-grep not installed"
command -v bd >/dev/null 2>&1 || bounded npm install --global --silent "@beads/bd@${BD_VERSION}" \
  || echo "setup: bd not installed"
exit 0  # always 0, and the whole script in under five minutes
```

ast-grep is pinned to the GitHub Actions workflow's `AST_GREP_VERSION`; raise
both together. It comes from PyPI, on the Trusted list. bd's npm package
fetches its binary from bd's GitHub releases, which a cloud session has not
yet been seen to reach; if it cannot, the log says "setup: bd not
installed" and the sweep runs without it.

**Environment variables.** One per line, as the environment's variables
(`.env` format). Put no token or key here: anyone who uses the environment
can read them.

```text
CURAMOIR_RUNNER=hosted
BASH_DEFAULT_TIMEOUT_MS=1800000
CLAUDE_CODE_RETRY_WATCHDOG=1
```

- `CURAMOIR_RUNNER=hosted` declares the hosted runner for every cúramóir
  command in the session (the prompt also sets it on each command). It is
  accepted because every cloud session sets `CLAUDE_CODE_REMOTE=true`.
- `BASH_DEFAULT_TIMEOUT_MS=1800000` makes the Bash tool's default timeout
  30 minutes (the default is 2). A command still running then is moved to
  the background, where it can run up to 30 more minutes before Claude Code
  stops it; only a value above 1800000 lengthens that background limit. So
  a sweep gets about an hour in all, the prompt's own limit, and a sweep
  longer than 30 minutes relies on that background time. If your sweeps
  take longer than an hour, set a larger value here and raise the prompt's
  60 minutes (its step 2) with it: the two move together, and either alone
  still stops the sweep at the lower one.
- `CLAUDE_CODE_RETRY_WATCHDOG=1` is Claude Code's setting for unattended
  sessions: it retries `429` and `529` capacity errors indefinitely, backing
  off up to 5 minutes, instead of failing after a few attempts, and retries
  other transient errors for about three hours. A spend limit or exhausted
  usage credits still fail at once. The sweep's own `claude -p` calls inherit it.

**Network access: Trusted**, the default level. If it is set too narrow,
the setup log shows "setup: ... not installed" lines and a run's step 1 or
2 fails to fetch cúramóir. Trusted reaches the package
registries the setup script installs from; GitHub goes through the session's
GitHub proxy whatever the level, so nothing wider is needed.

The prompt fetches cúramóir from GitHub, `git ls-remote` of
`https://github.com/rsionnach/curamoir` in step 1 and
`uvx --from git+https://github.com/rsionnach/curamoir@<SHA>` in steps 2
and 3, through that proxy. The proxy serves API requests and source
archives only for the repositories attached to the session (any other gets
a 403 saying it "is not enabled for this session"), and the cloud
environment documentation does not say whether a git fetch of a public
repository that is not attached gets through. A Routine that sweeps
rsionnach/curamoir itself has it attached. For any other repository this
is not yet verified: if step 1 reports that 403, this Routine cannot fetch
cúramóir, and that repository's sweep has to run from the GitHub Actions
workflow instead.

**Also on GitHub: branch protection on the default branch.** Set this
before the first **Run now**. Protect the default
branch with a ruleset (or a branch protection rule) that requires a pull
request and blocks force pushes, with no bypass for you. The Routine pushes
as you, and GitHub applies rules to the access you connected, so a rule you
can bypass does not stop its push. The sweep never pushes to the default
branch; the rule makes sure nothing else in the session can. Target the
default branch only: the Routine must still be able to push
`claude/curamoir-state` and the sweep's own branches (`curamoir/` unless
`guardrails.branch_prefix` says otherwise).

What to expect in a run:

**GitHub through REST.** In a cloud session (`CLAUDE_CODE_REMOTE=true`,
whatever `CURAMOIR_RUNNER` says) cúramóir asks GitHub through `gh api` REST
calls, because the proxy refuses GraphQL (`gh pr`, `gh issue` and
`gh label list` get a 403 there), and the run's job line carries
`github_api: rest` besides `runner` and `transcript`. It names the
repository from origin's push URL. What origin's URL looks like inside a
cloud session is not documented; if it is not a GitHub repository URL, the
sweep refuses before it clones (reason `sweep_base`), and that refusal is
the cue to report it.

**One run at a time.** Nothing keeps two runs of the Routine apart: each
run's lock is on its own VM. Do not press **Run now** while a run is going.

<!-- routine-environment:end -->
