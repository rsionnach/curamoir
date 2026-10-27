# cúramóir handoff

> Closed cm-fkd.9 (brief 'What changed' from sweep PRs), cm-854 (sweep tests: PyYAML hidden by isolated HOME, fixed in fixture), and cm-60c: the fix step's claude -p --restricted now loads ONLY the PreToolUse denylist via --settings (fix_step.pretooluse_settings, built from hooks/hooks.json), with CLAUDE_PLUGIN_ROOT pinned. Verified with the real CLI 2.1.286: rm -rf ran without the settings and was refused by the denylist with them. Suite: 2832 passed, 27 skipped (tests/plugin excluded).

- **when:** 2026-10-27T22:29:52+00:00
- **branch:** feat/e0-r0-guard-layer
- **uncommitted:** clean
- **in progress:** nothing claimed

## Intended next step

Next P1: cm-azr (stale-lock UX: status, doctor, deny message) or cm-hve (command policy: inline code via allowlisted runners). cm-2ui needs the user's pricing decision; cm-xy9 dogfood needs the user's go-ahead.

## Last decision

Closed cm-fkd.9 (0b0e971): the brief's 'What changed' lists sweep PRs from the ledger, with gh state. Closed cm-854: 86 sweep tests failed because isolated_home moved HOME, hiding the only PyYAML on this Mac (the per-user site). The fixture now puts the test interpreter's bin first on PATH. …