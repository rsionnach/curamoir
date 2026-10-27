# cúramóir handoff

> Closed cm-x5a.1 (curamoir run <job>), cm-x5a.6 (runner supervisor lock) and cm-fkd.4 (the sweep runner and its PR, built as ST1-ST6 = cm-fkd.10-15; HEAD 9202626). 'scripts/curamoir run sweep --repo <root>' now runs behind the guards: lint + tier-1 scan, fix step in a disposable clone, test gate (green/reduced), at most one PR on branch <prefix>sweep-YYYY-MM-DD-<base8> from origin/<GitHub default>. Acceptance uses stand-in claude/gh and a bare origin (tests/test_sweep_acceptance.py); a real dogfood run is deferred to cm-xy9. /curamoir:sweep stays look-only and points at 'scripts/curamoir run sweep' (user decision). Full suite: 2828 passed, 2 skipped (tests/plugin excluded). Follow-ups filed this run: cm-0ew (branch_prefix validation), cm-mox (a PR file whose edit fixed nothing, deferred by user), cm-1qo (flaky lint ResourceWarning), cm-ydl (brief reads sweep PRs), …

- **when:** 2026-10-27T20:16:40+00:00
- **branch:** feat/e0-r0-guard-layer
- **uncommitted:** clean
- **in progress:** nothing claimed

## Intended next step

Epic 3 has cm-fkd.9 (brief 'What changed' from sweep PRs) left; then cm-60c (load the E0-R5 denylist into the fix step before it ever gets Bash) or cm-azr (stale-lock UX). cm-2ui (budget ignores cache tokens) still needs a pricing decision from the user. A real dogfood sweep (cm-xy9) needs the user's go-ahead, since it pushes and opens a real PR.

## Last decision

Epic 3 (cm-fkd) is 9/11. Last session closed cm-4pu (7ed9006) and cm-v5p (2240fac); everything is pushed and the tree is clean. cm-4pu:
- run_guarded returns GuardedRun(outcome, reason, value), with UNUSABLE_ROOT for a root that cannot be inspected. …