# cúramóir handoff

> Closed cm-fkd.9 (0b0e971): the brief's 'What changed' lists sweep PRs from the ledger, with state from one gh pr list --state all. The window is the absence; closed-unmerged PRs are left out; a gh failure gives 'state unknown'. cm-ydl closed as its duplicate. Full suite: 2743 passed, 86 failed. All 86 also fail on clean HEAD, with the same set: sweep tests where lint cannot import PyYAML because the sweep's HOME is a temp dir and /usr/bin/python3's PyYAML is in ~/Library/Python. Filed as cm-854 (P1).

- **when:** 2026-10-27T21:03:21+00:00
- **branch:** feat/e0-r0-guard-layer
- **uncommitted:** clean
- **in progress:** nothing claimed

## Intended next step

Fix cm-854 first (suite not green on this machine). Epic 3 then has only the epic left open. Next: cm-60c or cm-azr. cm-2ui needs a pricing decision; cm-xy9 dogfood needs the user's go-ahead.

## Last decision

Closed cm-x5a.1 (curamoir run <job>), cm-x5a.6 (runner supervisor lock) and cm-fkd.4 (the sweep runner and its PR, built as ST1-ST6 = cm-fkd.10-15; HEAD 9202626). 'scripts/curamoir run sweep --repo <root>' now runs behind the guards: lint + tier-1 scan, fix step in a disposable clone, test gate (green/reduced), at most one PR on branch <prefix>sweep-YYYY-MM-DD-<base8> from origin/<GitHub default>. Acceptance uses stand-in …