---
description: Write a session handoff now, with an optional note to your future self
argument-hint: "[note]"
allowed-tools: Bash(*/scripts/curamoir:*)
---

Write a handoff for this session.

A handoff is written automatically when the session ends or compacts. This
command is for ending a session deliberately, and a deliberate handoff is
usually the better one: you state the next step, rather than leaving it to be
inferred from the last thing that happened.

Before running it, ask yourself what the developer would actually want their
future self to know, and offer a note if they have not given one. The single most
useful sentence is almost always **the intended next step** — not a summary of
what was done, which git already records.

```bash
"${CLAUDE_PLUGIN_ROOT}"/scripts/curamoir handoff --repo "${1:-.}" --note "$ARGUMENTS"
```

If `$ARGUMENTS` is empty, run it without `--note` rather than passing an empty
string.

Then show the developer the handoff and ask whether it says the right thing. It
is a file they will read cold in three weeks, so it is worth ten seconds now.

Two things not to do:

- Do not put secrets in the note. It is redacted on the way out, but redaction
  catches shapes it knows and is not a guarantee.
- Do not paste diffs or file contents into the note. The handoff deliberately
  carries summaries only; the diff is in git.
