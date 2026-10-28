# cúramóir handoff

> This session closed cm-fkd.9, cm-854, cm-60c, cm-azr and cm-hve. cm-hve (user chose hybrid): the policy refuses inline code reached through wrapper runners (uv/npm/pnpm/yarn/docker/podman), make --eval, pnpm/yarn dlx and uv tool run, every git -c, git config writes of command-valued keys, and command-valued env assignment prefixes. Docs now say it does not stop agent-written files run via pytest/make/go run/scripts. Follow-up cm-hhr: npm exec fetches missing packages (kept allowed; needs a decision). Suite: 2913 passed, 27 skipped.

- **when:** 2026-10-28T15:53:42+00:00
- **branch:** feat/e0-r0-guard-layer
- **uncommitted:** clean
- **in progress:** nothing claimed

## Intended next step

No unblocked P1 bugs or tasks remain except cm-2ui (needs the user's pricing decision for cache tokens). Next feature: cm-eqp.1 (E6-R1 dual filing to bd and gh). cm-hhr needs a decision; cm-xy9 dogfood needs the user's go-ahead.

## Last decision

This session closed cm-fkd.9 (brief 'What changed' from sweep PRs), cm-854 (sweep tests: PyYAML hidden by isolated HOME), cm-60c (fix step loads only the PreToolUse denylist via --settings; real CLI verified) and cm-azr (stale-lock UX: guards.lock.read_lock is the one verdict for the guard, status and doctor; the deny message says which signal fired). Suite: 2853 passed, 27 skipped (tests/plugin excluded; …