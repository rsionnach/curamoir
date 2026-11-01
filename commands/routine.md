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
5. Cloud environment: the one section 3 describes.
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

<!-- cm-2qp.10 fills this section: the Routine's cloud environment recipe
(setup script, environment variables, network access level, and the
default branch's protection rule), the same text as docs/runners.md's
"Routine environment" section, which carries the same placeholder until
then. Until then it says only what follows. -->

The recipe for the Routine's cloud environment (its setup script,
environment variables and network level) is not part of this version of
the command (bead cm-2qp.10). Say so plainly, and do not suggest one: until
it lands, a Routine made from these steps is not expected to complete a
sweep.

The developer can instead run the sweep on GitHub Actions now:
`/curamoir:init` offers that workflow.
