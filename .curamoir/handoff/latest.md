# cúramóir handoff

> Epic 3 (cm-fkd) is 9/11; cm-fkd.7 (ef6dfee, job_guard.run_guarded: cap+budget before any work) and cm-fkd.2 (28fc42d, tier-1 scan) are done. The rest is blocked: cm-fkd.4 (runner + PR) waits on cm-x5a.6 (runner lock, P0, itself behind cm-x5a.1 'curamoir run <job>') and on two small ready beads, cm-4pu and cm-v5p (API/contract gaps the excellence reads found); cm-fkd.9 waits on cm-fkd.4. Next step: do cm-4pu and cm-v5p, then cm-x5a.1 -> cm-x5a.6 -> cm-fkd.4. Check whether cm-7ck is already satisfied by job_guard (it passes per_run_ceiling_usd as the Budget estimate) and close it if so. cm-551 (capture a real claude -p payload) needs the user's OK for a live call. Process: write test output to a file, not through a pipe (a piped pytest hung …

- **when:** 2026-10-20T17:46:58+00:00
- **branch:** feat/e0-r0-guard-layer
- **uncommitted:** clean
- **in progress:** nothing claimed

## Intended next step

Run cm-fkd.7 (E3-R6: open-PR cap and budget) through epic-executor, then cm-fkd.2 (tier-1 scan). cm-fkd.4 is blocked on cm-x5a.6 (runner lock); its bead notes list every obligation from .3, .5, .6 and .8.

## Last decision

Epic 3 sweep pipeline is built through test gate, fix counts and idempotence; only the runner and PR step (cm-fkd.4) are missing. User decisions are DEC notes on cm-fkd: Python drives, the fix step runs in a disposable sandbox clone, tests run there with an allowlisted env (OS sandbox deferred, cm-p2f). Wrap reviewer probes in perl -e 'alarm N; exec …