# cúramóir handoff

> Epic 3 sweep pipeline is built through test gate, fix counts and idempotence; only the runner and PR step (cm-fkd.4) are missing. User decisions are DEC notes on cm-fkd: Python drives, the fix step runs in a disposable sandbox clone, tests run there with an allowlisted env (OS sandbox deferred, cm-p2f). Wrap reviewer probes in perl -e 'alarm N; exec @ARGV' and give reviewers a tool-call budget; they stall otherwise. Run pytest with stdin from /dev/null. If r5-lock refresh says an older session ID holds the lock, release it under that ID and re-acquire. Still open: bd dolt push has no remote, and GitHub's default branch is the feature branch.

- **when:** 2026-10-12T19:50:34+00:00
- **branch:** feat/e0-r0-guard-layer
- **uncommitted:** 2 change(s)
  - modified: .curamoir/handoff/latest.md
  - untracked: .curamoir/handoff/history/2026-10-09T11-29-13-172154-000.md
- **in progress:** nothing claimed

## Intended next step

Run cm-fkd.7 (E3-R6: open-PR cap and budget) through epic-executor, then cm-fkd.2 (tier-1 scan). cm-fkd.4 is blocked on cm-x5a.6 (runner lock); its bead notes list every obligation from .3, .5, .6 and .8.

## Last decision

Next: run cm-fkd.7 (E3-R6 open-PR cap + budget) with epic-executor, then cm-fkd.2 (tier-1 scan). cm-fkd.4 (open the PR, wire the runner) is blocked on cm-x5a.6 (runner lock); its bead notes list every obligation from .3/.5/.6/.8 (gh query must include isCrossRepository, PR-body marker, pr_field, snapshot step 2a). User decisions are recorded as DEC notes on cm-fkd: Python drives the sweep; fix …