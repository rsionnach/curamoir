PASS 1 (CORRECTNESS) sequential R5 review on bead opensrm-zo0o, checkpoint 4 (nthlayer front door).

# Correctness Reviewer
SINGLE FOCUS: correctness. Do not comment on naming, style, documentation, or nice-to-haves. Only flag things that are wrong, broken, or will produce incorrect results. Checklist: logic errors; type mismatches; error handling and uncaught propagation; unguarded None; off-by-one; return paths; contracts with callers; state mutation.

Scope is THE DIFF: `git -C <workspace>/nthlayer-zo0o-seam diff 0370124..71022e0` (test/test_error_budget_seam.py, docs/metrics-contract.md). Do not raise findings outside it. For the doc, correctness means: does it state what the code actually does? Check its claims against the published/sibling code: nthlayer-generate 4.1.0 (../nthlayer-generate, src/nthlayer_generate/recording_rules/manifest_builder.py: `_slo_kind`, `_budget_skip_reason`, `_error_budget_rules`, the per-kind rule builders), nthlayer-workers 4.2.0 (../nthlayer-workers, measure/adapters/prometheus.py, measure/cli.py, correlate/prometheus.py, observe/worker.py, observe/drift/analyzer.py), nthlayer-common (../nthlayer-common/src/nthlayer_common/manifest/slo_series.py).

Affected repo: nthlayer: base=0370124, head=71022e0, path=<workspace>/nthlayer-zo0o-seam

Requirements: the seam test must fail whenever measure (published nthlayer-workers) and generate (published nthlayer-generate) disagree about any error-budget series over the corpus (opensrm spec v2 examples + generate's shipped examples), except budgets generate's guard deliberately skips, each pinned exactly in EXPECTED_READ_NOT_WRITTEN with a bead (a fix must turn it red; a pinned file leaving the corpus must turn it red). written_not_read is never expected. Each request-ratio kind (availability, latency, error_rate) must have a named witness budget written AND read (KIND_WITNESSES). Existing pins (EXPECTED_REJECTED, EXPECTED_WITH_BUDGET, formats) keep working.

Verify in particular the set logic in the loop (expected_missing, read_not_written accumulation, agreed), whether any disagreement can now slip through unreported, and whether `_selector(manifest, slo)` builds the same string generate's labels and measure's query produce (service = manifest.name?).

Run it exactly as CI does, from the ecosystem root <workspace>:
  uv run --isolated --no-project --python 3.11 --with nthlayer-generate --with nthlayer-workers --with pytest pytest -q -p no:warnings nthlayer-zo0o-seam/test/test_error_budget_seam.py
(currently 1 passed). Do not modify files.

Report: CRITICAL / IMPORTANT / MINOR bullets with file:line, what's wrong, why, fix. Under 350 words. "No correctness issues found" is valid. End with Verdict: PASS / PASS WITH FIXES / FAIL.