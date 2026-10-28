# cúramóir handoff

> Closed cm-z0k: the cm-hve policy checks were bypassable (security review), now allowlist-first: git config writes only safe keys, with options read before the first positional as git does; make --eval prefixes/-E clusters/variables; package runners run only allowlisted programs, no --with, dlx/create/tool run refused; docker/podman limited to build/ps/inspect/...; go -toolexec/-exec, cargo --config; env prefixes allowlisted. User decisions recorded 2026-10-28: cm-2ui own cache rates defaulted (write 1.25x, read 0.1x input); cm-hhr require 'npm exec --no'; cm-xy9 ONE real dogfood sweep approved (may push a branch and open a PR, left open); cm-mox waits on that sweep. New follow-up cm-0ik (package installs naming a package) needs a decision. Suite: 2958 passed, 27 skipped.

- **when:** 2026-10-28T16:11:08+00:00
- **branch:** feat/e0-r0-guard-layer
- **uncommitted:** clean
- **in progress:** nothing claimed

## Intended next step

Agreed order: cm-hhr (npm exec --no), then cm-2ui (cache-token pricing), then the approved real dogfood sweep on this repo (report exactly what it did; feeds cm-mox).

## Last decision

This session closed cm-fkd.9, cm-854, cm-60c, cm-azr and cm-hve. cm-hve (user chose hybrid): the policy refuses inline code reached through wrapper runners (uv/npm/pnpm/yarn/docker/podman), make --eval, pnpm/yarn dlx and uv tool run, every git -c, git config writes of command-valued keys, and command-valued env assignment prefixes. Docs now say it does not stop agent-written files run via pytest/make/go run/scripts. …