---
description: Show lock, pause, spend, grades, scheduled jobs, open PRs and handoff age
allowed-tools: Bash(*/scripts/curamoir:*)
---

Show the current cúramóir state. This makes no model calls and no network calls,
and is expected to return in well under two seconds.

```bash
"${CLAUDE_PLUGIN_ROOT}"/scripts/curamoir status --repo "${1:-.}"
```

Relay the output as it is. Do not editorialise or re-order it — the point of this
command is a known, scannable shape.

Two things are worth drawing attention to if present, because they explain
everything else and a developer skimming will miss them:

- **PAUSED.** Nothing is running on a schedule. Say so first.
- **A held lock.** A job is in progress, or a crashed job left the lock behind.
  If the lock looks stale say so, and name the file — `.curamoir/state/lock` —
  since clearing it by hand is the fix. Do not promise that
  `/curamoir:doctor` will say more: it does not check the lock, and
  `/curamoir:status` is the only thing that reads it.

If spend is close to the ceiling, say that plainly too: at the ceiling,
scheduled jobs refuse to run rather than overrunning — quietly, recording the
refusal in the ledger.
