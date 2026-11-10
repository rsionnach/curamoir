PASS 1 (CORRECTNESS) sequential R5 review on bead opensrm-7zpz (Phase B: nthlayer-common 5.0.0, also fixes opensrm-1rnr). READ-ONLY: never modify repo files, never git checkout/switch/stash/reset. Probes only in <claude-tmp>/scratchpad.

Template: read ~/.claude/skills/rule-of-five-review/correctness-reviewer.md and follow it.

Scope is THE DIFF: git -C <workspace>/nthlayer-common diff ebf9685..2642e41

Binding spec: sibling ../opensrm (checked out on branch feat/7zpz-segments-metric): `git -C ../opensrm diff e48fd9b..HEAD -- OPENSRM-CORE-v2.md spec/v2/schema.json` — CORE §5.5 (entries + measurement/probe/outcome_signal closed), §5.5.1 (segments metric enum reversal_rate|high_confidence_failure|outcomes|escalation; measurement.window any metric, >0, default 30d; source iff reversal_rate, enum lineage|calibration_sample|downstream_signal; confidence_threshold iff high_confidence_failure, ratio; outcome_signal {source, window} iff outcomes), §5.5.5 (feedback_latency window fixed 30d), §5.5.6 E3.
Implementation brief with supervisor rulings: <claude-tmp>/scratchpad/common5-brief.md (R1–R5). Implementer report: scratchpad/common5-report.md. Accepted deviations: confidence_threshold uses the strict _ratio guard (strings rejected, matching schema type:number); srm/v1 calibration rejects `unit`.

Check: parser accepts exactly what the schema accepts for judgment_threshold (both directions — find any manifest the schema accepts that the parser rejects, or vice versa; probe with the spec's examples and negatives n67–n92 and your own variants); the closure table is the single source and matches the schema per type; round-trip threshold_to_v2_entry ∘ parse is identity incl. default window and outcome_signal; template concatenation/overrides paths apply the same rules; srm/v1 lift (1rnr): unit handling (absent, s, ms, m, h, d, w, bogus), window on feedback_latency (absent / 30d / other), exact accepted key set; v1_compat round trip; the pre-3c fix-it suggestion; public API (__init__ re-exports, __all__); bool/None/NaN guards; anything a consumer (core's manifest_to_dict) could silently mis-serialise because two types share ThresholdMeasurement.window.

Run: cd nthlayer-common && uv run pytest -q -o addopts="" (expect 1618 passed, 7 xfailed).

Focus: correctness only. Report CRITICAL / IMPORTANT / MINOR with file:line and fix, under 450 words. End with `git -C <repo> status --short` verbatim.