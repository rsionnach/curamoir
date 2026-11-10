PASS 1 (CORRECTNESS) sequential R5 review on bead opensrm-h0at (checkpoint 2b: the nthlayer-generate slice only).

Template: read ~/.claude/skills/rule-of-five-review/correctness-reviewer.md and follow it. SINGLE FOCUS: correctness only.

Your scope is THE DIFF:
  git -C <workspace>/nthlayer-generate-h0at-error-budget diff b62e252..aba8a4b
Do not raise findings outside it.

Affected repo: nthlayer-generate, base=b62e252, head=aba8a4b, path=<workspace>/nthlayer-generate-h0at-error-budget

Bead context: opensrm-h0at (P1). measure and correlate (nthlayer-workers) read `slo:error_budget:ratio`, a series nothing writes. generate writes `slo:error_budget_remaining:ratio` with the labels service, slo and objective. nthlayer-common 5.10.0 (sibling at ../nthlayer-common, src/nthlayer_common/manifest/slo_series.py) now owns that name as `ERROR_BUDGET_REMAINING` (SloSeries: metric, labels, selector()).

This diff:
- makes generate's availability rule take its record name from `ERROR_BUDGET_REMAINING.metric`;
- adds a test that every error-budget rule from the shipped example manifests carries exactly the contract's labels.

Generate's output must be byte-identical. The supervisor ran the full suite (6147 passed, 12 skipped, the same 12 skips as on main) without NTHLAYER_UPDATE_GOLDENS, and no golden changed. Check that the change is correct and that nothing else in generate still hard-codes the name in a way that should have moved. Other mentions are in docs and demo files, and in the module docstring's SloCard line; judge whether any of them is a correctness issue.

Report a bullet list of CRITICAL / IMPORTANT / MINOR. Under 300 words. "No correctness issues found" is a valid result.