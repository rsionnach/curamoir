# cúramóir handoff

> Closed cm-kry (launcher picks a PyYAML python). Dogfood sweep ran end to end via plain scripts/curamoir: refused (scope, E3-R2) after the fix agent made an out-of-scope insertion in src/curamoir/brief.py; no push or PR; $1.02. Data: Claude Code cache writes are 1-hour (2x input): the CLI cost reconciles exactly at 2x, so cm-2ui's 1.25x default under-prices by about 22% (cm-1et). New decision beads: cm-1et, cm-9ab (scope-breach policy), cm-t3a (exclude fixtures/), plus cm-0ik, cm-mox. The ledger (.curamoir/state/ledger.jsonl) is now committed, as designed.

- **when:** 2026-10-28T17:20:11+00:00
- **branch:** feat/e0-r0-guard-layer
- **uncommitted:** 1 change(s)
  - added: .curamoir/state/ledger.jsonl
- **in progress:** nothing claimed

## Intended next step

Get the user's decisions on cm-1et (cache write default), cm-9ab (scope-breach policy) and cm-t3a (exclude fixtures/), then implement. Other ready work: cm-uxl (signals leave stale locks/orphans), cm-6xd (test suite leaks sandboxes).

## Last decision

Closed cm-hhr (npm exec requires --no), cm-f41 (arity-aware option parsing for git/docker/compose/npm/pnpm/yarn/uv; npm subcommand allowlist; npm exec options read up to --, after 4 automated review rounds) and cm-2ui (cache tokens recorded on sweep lines and priced: model cache_write/cache_read, default 1.25x/0.1x input). Open decisions: cm-0ik (package installs naming a package). Suite: 3035 passed, 27 skipped.