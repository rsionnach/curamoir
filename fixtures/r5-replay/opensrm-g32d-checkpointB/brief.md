PASS 1 (CORRECTNESS) sequential R5 review on bead opensrm-g32d, Checkpoint B (nthlayer-common 5.2.0). READ-ONLY: never modify repo files, never git checkout/switch/stash/reset; probes only in <claude-tmp>/scratchpad; foreground only.

Template: read ~/.claude/skills/rule-of-five-review/correctness-reviewer.md and follow it.

Scope: git -C <workspace>/nthlayer-common diff a60802c..470f54d — new src/nthlayer_common/manifest/threshold_series.py and evaluation.py, re-exports in manifest/__init__.py, ASSESSMENT_KINDS in cloudevents.py, tests, docs.

Binding spec: <workspace>/nthlayer/docs/superpowers/specs/2026-10-12-g32d-judgment-threshold-evaluation-design.md (§4 metric contract, §5 evaluator), and the normative OPENSRM-CORE-v2 §5.5.1 / §5.5.5 / §5.5.6 (E1–E4) in ../opensrm. Plan (Tasks 2–6) at ../nthlayer/docs/superpowers/plans/2026-10-12-g32d-judgment-threshold-evaluation.md.

Check: E1 strict exceedance incl. limit 0 and duration bounds; E2 via duration_seconds; E3 every missing/non-finite path → not_evaluable with the right reason, never within; E4 combine; segments variance |segment − overall| in both directions, per-segment E3, distinct values, BoundResult measured/reason rules; AtLeast semantics (true value strictly > v: breach iff v >= limit, else not_evaluable) and that AtLeast cannot appear where it would be mis-handled (segments, combine, to_dict); ThresholdOutcome to_dict/from_dict round trip and JSON shape; THRESHOLD_SERIES vs THRESHOLD_BOUNDS and vs the spec §4 table; public API completeness (every name a consumer needs: evaluate_threshold, Samples, Missing, AtLeast, ThresholdOutcome, BoundResult, SegmentResult, THRESHOLD_SERIES, ThresholdSeries, FEEDBACK_LATENCY_WINDOW; is combine or MissingReason needed by workers' sampler per plan Task 9?); assessment kind; ValueError paths. Cross-check against what plan Tasks 9–12 (workers) will import and call — any name/signature mismatch now is cheap to fix.

Run: cd nthlayer-common && uv run pytest -q -o addopts="" (expect 1757 passed, 7 xfailed).

Focus: correctness only. CRITICAL / IMPORTANT / MINOR with file:line and fix, under 400 words. End with `git -C <repo> status --short` verbatim.