# cúramóir handoff

> Closed cm-fkd.9 (0b0e971): the brief's 'What changed' lists sweep PRs from the ledger, with gh state. Closed cm-854: 86 sweep tests failed because isolated_home moved HOME, hiding the only PyYAML on this Mac (the per-user site). The fixture now puts the test interpreter's bin first on PATH. Test-only; real sweeps were never affected. Full suite: 2829 passed, 27 skipped (tests/plugin excluded).

- **when:** 2026-10-27T22:10:28+00:00
- **branch:** feat/e0-r0-guard-layer
- **uncommitted:** clean
- **in progress:** nothing claimed

## Intended next step

Next P1: cm-60c (denylist hook in the fix step's restricted claude -p before it gets Bash) or cm-azr (stale-lock UX). cm-hve (command policy) is also P1. cm-2ui needs the user's pricing decision; cm-xy9 dogfood needs the user's go-ahead.

## Last decision

Closed cm-fkd.9 (0b0e971): the brief's 'What changed' lists sweep PRs from the ledger, with state from one gh pr list --state all. The window is the absence; closed-unmerged PRs are left out; a gh failure gives 'state unknown'. cm-ydl closed as its duplicate. …