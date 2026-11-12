PASS 1 (CORRECTNESS) sequential R5 review on bead opensrm-7zpz (Phase C: nthlayer-core 3.0.0). READ-ONLY: never modify repo files, never git checkout/switch/stash/reset; probes only in <claude-tmp>/scratchpad; no background work.

Template: read ~/.claude/skills/rule-of-five-review/correctness-reviewer.md and follow it.

Scope: git -C <workspace>/nthlayer-core diff 53d0570..296f9e2 (one commit: feat(api)!).
Brief with rulings: <claude-tmp>/scratchpad/core3-brief.md (R1–R5). Implementer report: scratchpad/core3-report.md.

Context: nthlayer-common 5.0.0 (sibling ../nthlayer-common main) exports threshold_to_v2_entry, the validated emitter of the v2 spec.judgment_threshold entry. Spec: ../opensrm (main) spec/v2/schema.json JudgmentThresholdSpec, OPENSRM-CORE-v2.md §5.5/§5.5.1.

Check: (1) /manifests judgment_thresholds items are exactly threshold_to_v2_entry(t) for every threshold type and every segments metric, always-present key, order preserved; (2) the OpenAPI items schema (src/nthlayer_core/_openapi/paths_other.py + regenerated docs/api/openapi.json) accepts exactly what spec/v2/schema.json JudgmentThresholdSpec accepts — compare both directions (feed the spec's examples and negatives n20–n92 threshold entries, and fuzzed variants, through both validators; OpenAPI 3.x vs JSON Schema draft-07 differences: nullable, discriminator, oneOf exclusivity — could an entry match two oneOf branches and be rejected?); (3) docs/api/openapi.json is in sync with regen (run `uv run python scripts/regen_openapi.py` into a scratch copy? — if the script only writes in place, instead diff its output logic; do NOT leave the repo modified); (4) any other /manifests consumer path or endpoint (single-manifest GET, catalogue search, etc.) that serialises thresholds differently; (5) pin/lock/EXPECTED_MAJORS/ceiling guard; (6) the BREAKING CHANGE footer accurately describes old vs new item shape (old shape: git show 53d0570:src/nthlayer_core/catalogue.py).

Run: cd nthlayer-core && uv run pytest -q -o addopts="" (expect 276 passed).

Focus: correctness only. CRITICAL / IMPORTANT / MINOR with file:line and fix, under 400 words. End with `git -C <repo> status --short` verbatim.