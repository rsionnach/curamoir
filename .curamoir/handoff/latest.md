# cúramóir handoff

> E14 Phase 2 done: PR #1 (feat/e14-platform-first -> feat/e0-r0-guard-layer), one commit per bead; closed E14.1-E14.5, E14.7-E14.10, follow-ups cm-2qp.12/.13, bug cm-93e; full suite 4743 passed, 5 skipped. User decisions taken this phase: --restricted and the denylist kept on hosted; hosted ledger to a state branch (curamoir/state; Routine claude/curamoir-state). Open for the user: cm-2qp.11 (hosted credential exposure), cm-6f0 (checkout hook launcher imports a planted curamoir package from cwd — pre-existing, touches the local path), cm-t5j (fix_scope byte-identical revert + drop), BASH_DEFAULT_TIMEOUT_MS kept at 1800000, Routine recipe defaults (fetch via proxy, bd via npm, ast-grep failure non-fatal, one environment per Routine). Chores filed: cm-908, cm-bpo, cm-wsf, cm-3aj.

- **when:** 2026-11-02T21:55:19+00:00
- **branch:** feat/e14-platform-first
- **uncommitted:** 1 change(s)
  - untracked: .DS_Store
- **in progress:** nothing claimed

## Intended next step

Wait for the user's go-ahead. Phase 3 (E14.6 dogfood) starts only after PR #1 merges: enable the workflow on rsionnach/curamoir, set CURAMOIR_REF to the merged commit and the ANTHROPIC_API_KEY secret, trigger workflow_dispatch, and work through cm-2qp.6's notes checklist; then the Routine (cm-2qp.14 first).

## Last decision

E14 Phase 1 (bead triage) applied: Epic 14 is cm-2qp with children cm-2qp.1..10 (E14.1..E14.10); labels local-adapter and guard-freeze in use; cm-p2f, cm-1et, cm-2ui closed with R5 waived per direction §6; cm-x5a.2/.3 closed as superseded. bd dolt push fails: no dolt remote is configured.