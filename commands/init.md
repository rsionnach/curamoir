---
description: Scaffold .curamoir/ in this repository, detecting language and module layout
argument-hint: "[path-to-repo]"
allowed-tools: Bash(*/scripts/curamoir:*), Read, Edit
---

Initialise cúramóir in this repository.

Run the scaffolding, which is deterministic and makes no model calls:

```bash
"${CLAUDE_PLUGIN_ROOT}"/scripts/curamoir init --repo "${1:-.}"
```

Then report to the developer:

- **What was detected** — the languages and the module list, verbatim from the
  output. If fewer than three modules were found, say so plainly: review
  selection will round-robin instead of choosing by grade, which is a real
  limitation rather than a detail.
- **What to check before anything runs.** Point them at
  `.curamoir/curamoir.yaml` and name the two settings most likely to be wrong
  for their repo: `modules.include` (detection is a guess) and
  `budget.monthly_ceiling_usd` (their money).
- **What happens next.** Suggest `/curamoir:doctor` to confirm prerequisites.
  The sweep runs on a schedule only once a hosted runner is set up for it:
  the GitHub Actions workflow below, or a Claude Code Routine
  (`/curamoir:routine`, when it ships). Until then nothing runs unattended.

Then **ask the developer** whether to write the scheduled GitHub Actions
sweep, `.github/workflows/curamoir-sweep.yml`. Before they answer, tell them
what it involves:

- It runs `curamoir run sweep` weekly (and on demand) on a GitHub-hosted
  runner and may open one PR of fixes per run.
- It needs a `CURAMOIR_REF` repository variable: pin a cúramóir tag or
  commit SHA. The job fails at its first step without it. A branch name is
  accepted but discouraged: it is resolved to a commit once per run, so
  each run may get a different cúramóir.
- It needs an `ANTHROPIC_API_KEY` repository secret, and every run spends
  API money on that key (billed per token). The subscription token is not
  wired in, because whether its terms permit unattended use is unsettled.
- It commits each run's ledger lines to a `curamoir/state` branch, never to
  the default branch.
- It needs "Allow GitHub Actions to create and approve pull requests" turned
  on in the repository's Actions settings, or its PR cannot open.

Only if they say yes, run:

```bash
"${CLAUDE_PLUGIN_ROOT}"/scripts/curamoir init --repo "${1:-.}" --github-workflow
```

(On an initialised repository this reports "already initialised" again,
still writes the workflow, and exits 0 when the workflow was written.)
Relay what it printed. If a workflow was already there, nothing was
overwritten: the template went to `curamoir-sweep.yml.new` beside it;
offer to diff and merge. Remind them the workflow does nothing until it
is committed and pushed, and that its header comment lists what it needs.
If they say no, write nothing.

If the command reports that cúramóir is already initialised, it will have
written `.curamoir/curamoir.yaml.new` rather than touching the existing config.
Offer to diff the two and merge, and do not overwrite the existing file unless
the developer asks for it.

Do not edit `.curamoir/curamoir.yaml` yourself as part of this command beyond
what the developer explicitly asks for — it is a file they own.
