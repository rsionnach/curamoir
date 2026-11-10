PASS 1 (CORRECTNESS) sequential R5 review on bead opensrm-wo62.4.3.

Read first: <claude-tmp>/scratchpad/wo62-4-3-context.md (bead context, rulings, scope exclusions) and the template ~/.claude/skills/rule-of-five-review/correctness-reviewer.md.

Scope: THE DIFF only, excluding goldens:
  git -C <workspace>/nthlayer-generate-wo62-4-3 diff 88057bb..e3c952e -- . ':!tests/golden/data'
Affected repo: nthlayer-generate, base=88057bb, head=e3c952e, path=<workspace>/nthlayer-generate-wo62-4-3
Pre-change view: git -C <path> show 88057bb:<file>. Read-only: do not modify files.

Focus: correctness only — logic errors, exception types that escape (load_manifest raises FileNotFoundError, ManifestLoadError (not a ValueError subclass), and bare ValueError from the model for tier/type), contract mismatches between each CLI and its *_from_manifest twin (arguments dropped or no longer passed, e.g. environment, output paths, routing), callers of changed helpers (extract_dependency_technologies, dependency_technology, the dashboard builder, sloth, recording rules). Check each command's every exit path.
Report: CRITICAL / IMPORTANT / MINOR bullets with file:line, under 350 words, then Verdict. "No correctness issues found" is valid.