PASS 1 (CORRECTNESS) sequential R5 review on bead opensrm-g32d (Checkpoint C: nthlayer-workers).

Read ~/.claude/skills/rule-of-five-review/correctness-reviewer.md and apply it as your focus.

Your review scope is THE DIFF: `git -C <workspace>/nthlayer-workers-g32d-thresholds diff 8e7b0a1..53ecc14`. Pre-change view: `git show 8e7b0a1:<file>`. Do not raise findings outside that diff. Read-only.

Bead: opensrm-g32d — nthlayer-workers must evaluate judgment thresholds per OpenSRM CORE §5.5.6 E1–E4 instead of mis-evaluating error magnitudes as SLOs. This branch (workers 3.0.0, BREAKING): pins nthlayer-common 5.2.0; removes dead SLO-path error-magnitude branches; adds measure/thresholds/{sampler,state,phase}.py (Prometheus sampling incl. Python bucketQuantile with AtLeast for +Inf ranks; per-key identity + re-arm hysteresis; a threshold phase in MeasureModule.process_cycle emitting judgment_threshold_evaluation assessments and quality_breach verdicts at severity high); MEASURE_THRESHOLD_AUTONOMY (default off) feeding threshold breaches into the one-way autonomy ratchet; docs.

Authorities: design <workspace>/nthlayer/docs/superpowers/specs/2026-10-12-g32d-judgment-threshold-evaluation-design.md; metric contract <workspace>/nthlayer/docs/metrics-contract.md; nthlayer-common 5.2.0 at ../nthlayer-common (evaluation.py, threshold_series.py). Ledger of rulings made during implementation (treat as decided unless you find them wrong against the spec): <workspace>/nthlayer/.superpowers/sdd/2026-10-12-g32d-judgment-threshold-evaluation/progress.md.

Focus on correctness of the whole: does a real manifest served by core 3.0 /manifests reach evaluate_threshold with the right samples, the right key, and produce the right assessment/verdict/state transitions end to end; are any interactions between the SLO phases and the threshold phase wrong (shared dicts, ordering, carry-forward, cleanup); does anything break for an existing deployment on upgrade (restored state from 2.x, absent new keys).

Report: CRITICAL / IMPORTANT / MINOR bullets with file:line, under 400 words. "No correctness issues found" is valid.