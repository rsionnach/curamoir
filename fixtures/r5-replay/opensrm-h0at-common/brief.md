PASS 1 (CORRECTNESS) sequential R5 review on bead opensrm-h0at (checkpoint 1: the nthlayer-common slice only).

Template body:
You are reviewing code changes with a SINGLE FOCUS: correctness. Do not comment on naming, style, documentation, or "nice to have" improvements. Only flag things that are wrong, broken, or will produce incorrect results. Checklist: logic errors; type mismatches across boundaries; error handling; null access; off-by-one; races; return values; API contracts (do signatures match callers / intended callers); state mutations; resource cleanup. Read the diff, read the requirements below, and for each changed function verify the contract holds for its intended callers.

Your review scope is THE DIFF, not the whole file at HEAD. Run:
  git -C <workspace>/nthlayer-common-h0at-error-budget diff 1337a5a..7f07049
Do not raise findings on code outside that diff. Pre-change view: `git -C <path> show 1337a5a:<file>`.

Affected repos:
nthlayer-common: base=1337a5a, head=7f07049, path=<workspace>/nthlayer-common-h0at-error-budget

Bead: opensrm-h0at (P1 bug). measure (nthlayer-workers measure/adapters/prometheus.py:167) and correlate (correlate/prometheus.py:66) query `slo:error_budget:ratio`, which nothing writes; nthlayer-generate writes `slo:error_budget_remaining:ratio` with labels service, slo, objective (nthlayer-generate/src/nthlayer_generate/recording_rules/manifest_builder.py:193-206); workers' drift analyzer (observe/drift/analyzer.py:162) already reads `_remaining`. Ruling: canonical name is `slo:error_budget_remaining:ratio`; it lives in a shared contract module in nthlayer-common (this diff), first slice of opensrm-wo62.3, modelled on nthlayer_common/manifest/threshold_series.py. Next checkpoint (NOT in this diff): generate uses ERROR_BUDGET_REMAINING.metric for the record name; measure uses .selector(service=..., slo=<SLO name>); correlate uses min(.selector(service=...)); drift uses .selector(service=..., slo=...). Check that the API in this diff serves those callers correctly, e.g. that selector() emits valid PromQL for real service/SLO names (sibling repos are readable at ../nthlayer-generate and ../nthlayer-workers for context), and that escaping matches PromQL string-literal rules.

Focus: correctness only.

Report format: bullet list of CRITICAL / IMPORTANT / MINOR. Under 350 words. "No correctness issues found" is a valid result.