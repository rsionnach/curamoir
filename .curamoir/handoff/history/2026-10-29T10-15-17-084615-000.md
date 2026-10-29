# cúramóir handoff

> Stopped at user request mid-R5 on cm-p2f (test-gate OS sandbox). Correctness and clarity PASS at a139609 after rounds 4-7 plus clarity fixes; edge-cases review was stopped before reporting. Closed this session: cm-hud, cm-hve, cm-z0k, cm-hhr, cm-f41. User decision: OS-sandbox the job agents own claude -p -> cm-xy9.8 (P0, release blocker, blocked by cm-p2f). Filed cm-xy9.9 (Linux AF_UNIX seccomp), cm-6jl (guard refusal-message fixes); cm-siw notes extended. All committed and pushed.

- **when:** 2026-10-29T10:15:17+00:00
- **branch:** feat/e0-r0-guard-layer
- **uncommitted:** clean
- **in progress:** nothing claimed

## Intended next step

Re-dispatch the R5 edge-cases review for cm-p2f (BASE 892627b, HEAD a139609; include usability layouts: Homebrew/system python, pipx, poetry/pdm venvs, pnpm/yarn PnP, odd repo paths), then provenance, then excellence (advisory); close cm-p2f; start cm-xy9.8.

## Last decision

Closed cm-kry (launcher picks a PyYAML python). Dogfood sweep ran end to end via plain scripts/curamoir: refused (scope, E3-R2) after the fix agent made an out-of-scope insertion in src/curamoir/brief.py; no push or PR; $1.02. Data: Claude Code cache writes are 1-hour (2x input): the CLI cost reconciles exactly at 2x, so cm-2ui's 1.25x default under-prices by about 22% (cm-1et). …