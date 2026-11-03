---
name: r5-supervise
description: >-
  Supervises the sequential Rule-of-Five review of one curamoir bead before it
  may close: four blocking passes (correctness → clarity → edge cases →
  fixture provenance), each a separate lens, one reviewer subagent at a time,
  then one advisory excellence read. Holds the R5 lock for the whole run,
  records every step in .claude/r5-state.json (which the parallel-block hook
  reads), fixes and re-reviews until each pass is clean, and only then closes
  the bead. Activate when the user invokes /r5-supervise <bead-id>, asks to
  "R5" or "review" a bead before closing, or whenever a bead's change is ready
  to close.
---

# /r5-supervise — curamoir's sequential R5 supervisor

Ported from nthlayer-ecosystem/.claude/skills/r5-supervise (cm-99l) for this
single repository. The rule it enforces is the user's, and it is not
negotiable:

- **Every bead, every pass.** Four blocking passes, then the advisory read. No
  pass is skipped for a "small" or "docs-only" change unless the user approves
  that skip for that bead, in this conversation.
- **One lens per reviewer.** Each pass is its own reviewer subagent with its own
  template. Never two lenses in one reviewer.
- **One reviewer at a time, in order.** Never two reviewers in flight, never a
  later pass before an earlier one is clean. The state helper refuses a pass
  out of order, and the parallel-block hook refuses a second agent; record
  the state BEFORE every dispatch (`pass` / `request`).
- **The orchestrator never runs a pass itself.** If a reviewer stalls,
  `r5-state.sh abandon <pass> <reason>`, then `r5-state.sh request <pass>
  <iteration>` and dispatch a fresh reviewer (or resume the stalled one after
  the request); do not substitute your own review or your own mutation
  harness for its verdict.
- **Anything changed after a pass is reviewed by that pass again**, including
  excellence touch-ups and one-line edits made while squashing.

Helpers (all in `.claude/hooks/`, all resolve this repository's main worktree):
`r5-lock.sh` (the run lock), `r5-state.sh` (the only writer of the state
file), `r5-parallel-block.sh` (the hook, wired in `.claude/settings.local.json`
for `Agent|Task`). The reviewer window works like this: `r5-state.sh pass`
(or `request`, for a re-dispatch) asks for exactly one reviewer; the hook lets
the next subagent dispatch through and records it as reviewer_dispatched,
atomically; every later dispatch is refused until `r5-state.sh findings`
closes the window (or `abandon`, and then `request` again). In a fix round
the hook records each dispatch as fix_dispatched and lets one fix agent run
at a time, until `event fixes_applied`. Never dispatch two subagents in one
message while a reviewer has been requested: the second is refused. The run
belongs to the session that started it (or `adopt`ed it); other sessions'
dispatches are refused only while the lock's lease is live.

## Inputs

```
/r5-supervise <bead-id> [--base <sha>] [--head <sha>]
```

Without `--base/--head`: HEAD = the newest commit whose message names the
bead; BASE = the parent of the oldest such commit (`git log --grep`).

## Pre-flight (abort with the reason if any fails)

1. **Lock:** `bash .claude/hooks/r5-lock.sh acquire r5-supervise <bead-id>`.
   Exit 2 = held by another session or a nesting attempt: stop, show the
   message, do not delete the lock without asking the user.
2. **Bead:** `bd show <bead-id> --json` — open or in_progress (reopen a closed
   bead being re-reviewed: `bd update <id> --status in_progress`).
3. **State:** `bash .claude/hooks/r5-state.sh init <bead-id> <base> <head>`.
   Exit 2 = another run is in flight: stop.
4. **Clean tree:** `git status --short` shows nothing but untracked noise
   (`.DS_Store`). Uncommitted work would contaminate the review.
5. **Baseline:** the full suite passes at HEAD
   (`uv run pytest -q -p no:cacheprovider`), `uv run ruff check src tests`,
   `scripts/curamoir-lint --repo .`. A red baseline is the user's to decide on.

On a pre-flight failure after step 3: `r5-state.sh finish aborted`. After
step 1: release the lock (`r5-lock.sh release r5-supervise`).

## Pass loop

For each pass in `correctness`, `clarity`, `edge-cases`, `provenance`:

1. `bash .claude/hooks/r5-lock.sh refresh r5-supervise` (exit 1 = lock lost:
   abort the run).
2. `bash .claude/hooks/r5-state.sh pass <pass>` (requests the reviewer).
3. Read `~/.claude/skills/rule-of-five-review/<pass>-reviewer.md` (edge-cases
   uses `edge-cases-reviewer.md`).
4. Dispatch exactly one reviewer subagent, alone in its message
   (subagent_type `pr-review-toolkit:code-reviewer` or `general-purpose`).
   The hook records it (reviewer_dispatched) and refuses every other
   dispatch until step 5. If the dispatch did not launch (a denied
   permission prompt, another hook), it is still recorded:
   `abandon <pass> "never started"`, `request`, dispatch again. Its brief:
   - `PASS <n> (<PASS>) of the sequential R5 review of bead <bead-id>.`
   - the template body, inlined;
   - the scope: `git diff <base>..<head>`; no findings outside the diff; the
     pre-change view via `git show <base>:<file>`;
   - the bead (`bd show` output) and any user decisions in its notes;
   - `Focus: <pass> only — do not comment on other dimensions.`;
   - report: CRITICAL / IMPORTANT / MINOR with file:line, a verdict.
   - **provenance only:** it may mutate files for the kill check and must
     restore every one (`git checkout -- <file>`, never `git stash` or
     `checkout .`); macOS has no `timeout` binary, so run each mutant through
     `subprocess.run(..., timeout=...)`; it ends by printing
     `git status --short` verbatim. A pass whose kill check could not run is
     NOT clean.
   While that reviewer runs, dispatch nothing else (the hook refuses).
5. When it reports: `bash .claude/hooks/r5-state.sh findings <pass> <c> <i> <m> <verdict>`.
   After provenance, check `git status --short` yourself; a dirty tree is a
   failed pass: restore, record `event reviewer_left_tree_dirty`, re-dispatch.
6. If `critical + important == 0`: `r5-state.sh done <pass> <fix_iterations>`
   and go to the next pass. (Minors: fix them in the same fix round if cheap,
   then that fix round is re-reviewed like any other.)
7. Otherwise: refresh the lock, dispatch ONE fix subagent (or fix it yourself
   if the change is a few lines; the hook records a fix agent as
   fix_dispatched and refuses a second); when the fix is in place,
   `r5-state.sh event fixes_applied '{"files":[...]}'` (this closes the fix
   agent's window; record it only after the agent has finished); run the full suite, ruff and curamoir-lint
   (`event tests_passed` / `event tests_failed` — a failure stops the run with
   status failed); commit (`r5(<pass>): <summary> · <bead-id>` with the
   Co-Authored-By trailer); `event commit_made '{"sha":"..."}'`; then
   `r5-state.sh request <pass> <iteration + 1>` (refused while a fix agent
   is open) and re-dispatch the SAME pass (step 4). `done` is refused unless
   that pass's clean findings are the run's last window event, so a fix or a
   re-request after them always goes back through the reviewer.
8. Three fix iterations without a clean pass: stop and ask the user.
9. A finding that needs a design decision: `event design_question_raised`,
   ask the user, keep the lock (the run is paused, not over).

## Advisory stage — excellence

After all four blocking passes are clean: refresh, `r5-state.sh pass
excellence`, dispatch the excellence reviewer once
(`excellence-reviewer.md`, "This pass is ADVISORY"), then `r5-state.sh
advisory <c> <i> <m> <verdict>`. No fix loop. If you choose to apply any of its
suggestions, that change goes back through every blocking pass it touches
(the rule above); otherwise record the suggestions as follow-up beads.
A NEEDS WORK verdict or a CRITICAL: do not close; ask the user.

## Close

1. `bd close <bead-id> --reason "<per-pass findings counts, fix iterations,
   commits, provenance mutants run/survived, advisory notes>"`.
2. `bash .claude/hooks/r5-state.sh finish completed` (archives the trail as
   `.claude/r5-state-<bead>-completed-<ts>.json`).
3. `bash .claude/hooks/r5-lock.sh release r5-supervise`.

On failure or abort: `r5-state.sh finish failed|aborted`, then release the
lock. A crash leaves the lease to expire after 15 minutes; after that, other
sessions' dispatches go through, and a new session resumes the run (below).
The helpers' short mutex is a kernel flock(2), dropped when its holder dies,
so a killed helper never wedges the run.

## Resume

If `.claude/r5-state.json` exists with status in_progress: read it, re-acquire
the lock (idempotent in the same session; a new session reclaims it once
the old lease has run out), then `r5-state.sh adopt` if the run's
session_id is not this session, and log `event session_restarted`. Continue
from `current_pass`. If its last window event is reviewer_dispatched and
that reviewer is gone (or never started), `abandon` it and `request` the
pass again; if it is fix_dispatched, check the fix agent's work, then
`event fixes_applied`.
If that pass is provenance, check `git status --short` first and ask the user
before restoring anything — a leftover mutant looks like real work.

## Several beads

Run beads one after another, never interleaved: finish (close or abort) one
bead's run before `init` on the next. The lock and the state file both refuse
a second run while one is in flight.
