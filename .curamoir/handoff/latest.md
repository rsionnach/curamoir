# cúramóir handoff

> Epic 3 (cm-fkd) is 9/11. This session closed cm-4pu (7ed9006: run_guarded returns GuardedRun; repo_labels with a strict, casefolded KillSwitch label check; read-only JobRun.ledger; strict_config in repo_config fails closed on every OSError) and cm-v5p (2240fac: tier-1 runner contract; the line's cost is derived from top-level tokens; budget.UNRECORDED for a call with no usage; a (0,0) line can no longer switch the ceiling off; status sandbox_changed). cm-fkd.4 (runner + PR) is still blocked on cm-x5a.6 (runner lock, behind cm-x5a.1 'curamoir run <job>'); its notes now list the gh label adapter rules (--limit, --json name, time out), adding the scan's usage to the top-level tokens, and an end-to-end UNRECORDED test. Next: cm-x5a.1 -> cm-x5a.6 -> cm-fkd.4, or the new P1 cm-2ui (budget ignores …

- **when:** 2026-10-22T13:23:23+00:00
- **branch:** feat/e0-r0-guard-layer
- **uncommitted:** clean
- **in progress:** nothing claimed

## Intended next step

Run cm-fkd.7 (E3-R6: open-PR cap and budget) through epic-executor, then cm-fkd.2 (tier-1 scan). cm-fkd.4 is blocked on cm-x5a.6 (runner lock); its bead notes list every obligation from .3, .5, .6 and .8.

## Last decision

Epic 3 (cm-fkd) is 9/11; cm-fkd.7 (ef6dfee, job_guard.run_guarded: cap+budget before any work) and cm-fkd.2 (28fc42d, tier-1 scan) are done. The rest is blocked: cm-fkd.4 (runner + PR) waits on cm-x5a.6 (runner lock, P0, itself behind cm-x5a.1 'curamoir run <job>') and on two small ready beads, cm-4pu and cm-v5p (API/contract gaps the excellence reads found); cm-fkd.9 waits on cm-fkd.4. Next step: …