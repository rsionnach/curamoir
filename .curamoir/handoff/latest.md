# cúramóir handoff

> This session closed cm-fkd.9 (brief 'What changed' from sweep PRs), cm-854 (sweep tests: PyYAML hidden by isolated HOME), cm-60c (fix step loads only the PreToolUse denylist via --settings; real CLI verified) and cm-azr (stale-lock UX: guards.lock.read_lock is the one verdict for the guard, status and doctor; the deny message says which signal fired). Suite: 2853 passed, 27 skipped (tests/plugin excluded; test_hook_wiring passes).

- **when:** 2026-10-28T15:37:26+00:00
- **branch:** feat/e0-r0-guard-layer
- **uncommitted:** clean
- **in progress:** nothing claimed

## Intended next step

Next P1: cm-hve (command policy: inline code and arbitrary execution reachable through allowlisted runners). cm-eqp.1 (E6-R1 dual filing) is the next feature bead. cm-2ui needs the user's pricing decision; cm-xy9 dogfood needs the user's go-ahead.

## Last decision

Closed cm-fkd.9 (brief 'What changed' from sweep PRs), cm-854 (sweep tests: PyYAML hidden by isolated HOME, fixed in fixture), and cm-60c: the fix step's claude -p --restricted now loads ONLY the PreToolUse denylist via --settings (fix_step.pretooluse_settings, built from hooks/hooks.json), with CLAUDE_PLUGIN_ROOT pinned. Verified with the real CLI 2.1.286: rm -rf ran without the settings and was refused by the denylist …