# cúramóir handoff

> Closed cm-hhr (npm exec requires --no), cm-f41 (arity-aware option parsing for git/docker/compose/npm/pnpm/yarn/uv; npm subcommand allowlist; npm exec options read up to --, after 4 automated review rounds) and cm-2ui (cache tokens recorded on sweep lines and priced: model cache_write/cache_read, default 1.25x/0.1x input). Open decisions: cm-0ik (package installs naming a package). Suite: 3035 passed, 27 skipped.

- **when:** 2026-10-28T16:42:50+00:00
- **branch:** feat/e0-r0-guard-layer
- **uncommitted:** clean
- **in progress:** nothing claimed

## Intended next step

Run the user-approved real dogfood sweep (cm-xy9 note, 2026-10-28): 'scripts/curamoir run sweep --repo .' on this repo; it may push a curamoir/sweep-* branch and open ONE PR, left open for the user. Report exactly what it did; then decide cm-mox with the user.

## Last decision

Closed cm-z0k: the cm-hve policy checks were bypassable (security review), now allowlist-first: git config writes only safe keys, with options read before the first positional as git does; make --eval prefixes/-E clusters/variables; package runners run only allowlisted programs, no --with, dlx/create/tool run refused; docker/podman limited to build/ps/inspect/...; go -toolexec/-exec, cargo --config; env prefixes allowlisted. User decisions recorded 2026-10-28: cm-2ui own …