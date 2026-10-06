---
description: Validate config and prerequisites, verify the hooks fire, and report fixes
allowed-tools: Bash(*/scripts/curamoir:*)
---

Check that cúramóir is correctly installed and configured here.

```bash
"${CLAUDE_PLUGIN_ROOT}"/scripts/curamoir doctor --repo "${1:-.}"
```

Relay every finding with its fix. Then help with the fixes, in this order:

1. **Errors first.** cúramóir cannot work until these are resolved. Offer to do
   whatever is safe and local — correcting a key in `.curamoir/curamoir.yaml`,
   for instance. Do not install software without asking.
2. **Warnings second.** These are degraded-but-usable modes, and some are
   choices rather than faults. `bd` missing means findings go to GitHub only;
   `ast-grep` missing means AST rules skip while regex rules still run. Say what
   the developer loses, and let them decide whether they care.

Be accurate about the hook check specifically. It runs the real hook against a
real destructive command, so a passing result means the hook genuinely fires and
denies. If it fails, say plainly that command hooks fail OPEN in Claude Code,
which means a hook that does not deny is protecting nothing — it is not a
cosmetic problem.

Do not claim cúramóir is healthy if there are errors.
