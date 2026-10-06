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
- **What happens next.** Nothing runs on a schedule until they set that up.
  Suggest `/curamoir:doctor` to confirm prerequisites.

If the command reports that cúramóir is already initialised, it will have
written `.curamoir/curamoir.yaml.new` rather than touching the existing config.
Offer to diff the two and merge, and do not overwrite the existing file unless
the developer asks for it.

Do not edit `.curamoir/curamoir.yaml` yourself as part of this command beyond
what the developer explicitly asks for — it is a file they own.
