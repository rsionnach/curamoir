# cúramóir handoff

> Epic 3 (cm-fkd) is 9/11. Last session closed cm-4pu (7ed9006) and cm-v5p (2240fac); everything is pushed and the tree is clean. cm-4pu:
- run_guarded returns GuardedRun(outcome, reason, value), with UNUSABLE_ROOT for a root that cannot be inspected. - repo_labels feeds a strict KillSwitch label check: an iterable of str, stripped and casefolded. Any other shape degrades the check. - JobRun gains read-only ledger/outcome/reason, and a later outcome clears a stale reason. …

- **when:** 2026-10-22T13:44:46+00:00
- **branch:** feat/e0-r0-guard-layer
- **uncommitted:** clean
- **in progress:** nothing claimed

## Intended next step

Pick one: (a) cm-x5a.1 ('curamoir run <job>'), then cm-x5a.6 (runner lock, P0), then cm-fkd.4 (sweep runner + PR, whose bead notes list every obligation, including the gh label adapter rules and folding the scan's tokens into the top-level counts), run through epic-executor; or (b) cm-2ui (P1, the budget ignores cache tokens), which needs a pricing decision from the user first. Before either, check whether cm-7ck is already satisfied by job_guard (it passes per_run_ceiling_usd as the Budget estimate) and close it if so.

## Last decision

Epic 3 (cm-fkd) is 9/11. Last session closed cm-4pu (7ed9006) and cm-v5p (2240fac); everything is pushed and the tree is clean. cm-4pu:
- run_guarded returns GuardedRun(outcome, reason, value), with UNUSABLE_ROOT for a root that cannot be inspected. …