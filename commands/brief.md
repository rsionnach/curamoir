---
description: Generate the away-brief — what happened while you were gone, and one task to start on
allowed-tools: Bash(*/scripts/curamoir:*)
---

Generate the away-brief.

```bash
"${CLAUDE_PLUGIN_ROOT}"/scripts/curamoir brief --repo "${1:-.}"
```

Show it to the developer as it is. It is six fixed sections in a fixed order, and
the shape is the product: someone returning after three weeks should be able to
skim it without being taught how.

What to add, briefly, and only where it helps:

- **Lead with anything in "Blocked on you".** That section is the only one
  describing work that cannot proceed without them, and a developer skimming will
  miss it.
- **Then "Start here".** It names exactly one task and why it was chosen. If they
  want something else, offer to pick again rather than listing alternatives — the
  single suggestion is deliberate, because a list is a backlog and the backlog is
  what they already had.

If sections read "not available yet", say plainly why: those jobs have not run
yet. Do not present an empty section as though nothing happened — those are
different claims, and conflating them makes the brief untrustworthy.

The brief is also written to `.curamoir/briefs/latest.md`, which is plain
markdown and readable without cúramóir installed. Mention that if they ask how to
read it from a phone.
