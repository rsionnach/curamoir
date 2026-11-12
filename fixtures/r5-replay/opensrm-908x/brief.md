PASS 1 (CORRECTNESS) sequential R5 review on bead opensrm-908x. READ-ONLY: never modify repo files, never git checkout/switch/stash/reset; probes only in <claude-tmp>/scratchpad; foreground only.

Template: read ~/.claude/skills/rule-of-five-review/correctness-reviewer.md and follow it.

Scope: git -C <workspace>/nthlayer-common diff 28dbfab..eb6fc7f — adds public `threshold_from_v2_entry(entry, where="judgment_threshold entry") -> JudgmentThreshold` in manifest/parser/v2.py wrapping `_parse_judgment_threshold`, re-exported from manifest/__init__.py; tests/test_threshold_from_v2_entry.py; docs.

Bead: consumers (nthlayer-workers, g32d) receive core 3.0's /manifests judgment_thresholds items (exact v2 entries from threshold_to_v2_entry) and need a typed way back. Check: (1) for every v2 entry the manifest parser accepts inside a manifest, the function returns the same JudgmentThreshold, and rejects what it rejects — are there checks the manifest parser applies to thresholds OUTSIDE _parse_judgment_threshold (e.g. ai-gate service-type requirement, template concatenation, pre-3c judgment_slo placement, cross-entry checks) that this per-entry function cannot apply, and does that matter for the intended consumer? (2) does it accept a served entry from core 3.0 exactly (core always emits segments measurement.window; calibration measurement; flat keys); (3) default-window behaviour: from(to(t)) == t when t came from a manifest with no window; (4) exception type consistency with threshold_to_v2_entry (ValueError) — is the asymmetry (OpenSRMV2ParseError vs ValueError) a trap for consumers? is OpenSRMV2ParseError a ValueError subclass? (5) import cycles / public API.

Run: cd nthlayer-common && uv run pytest -q -o addopts="" (expect 1675 passed, 7 xfailed).

Focus: correctness only. CRITICAL / IMPORTANT / MINOR under 300 words. End with `git -C <repo> status --short` verbatim.