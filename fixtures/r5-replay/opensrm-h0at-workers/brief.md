PASS 1 (CORRECTNESS) sequential R5 review on bead opensrm-h0at (checkpoint 2c: the nthlayer-workers slice only).

Template: read ~/.claude/skills/rule-of-five-review/correctness-reviewer.md and follow it. SINGLE FOCUS: correctness only.

Your scope is THE DIFF:
  git -C <workspace>/nthlayer-workers-h0at-error-budget diff 1fa7967..d5fd87d
Do not raise findings outside it. For each changed function, read its callers.

Affected repo: nthlayer-workers, base=1fa7967, head=d5fd87d, path=<workspace>/nthlayer-workers-h0at-error-budget

Bead opensrm-h0at (P1): measure (measure/adapters/prometheus.py `_query_for`) and correlate (correlate/prometheus.py `fetch_metric_breaches`) queried `slo:error_budget:ratio`, which nothing writes. generate writes `slo:error_budget_remaining:ratio` once per availability-typed classical SLO. Its labels are service = manifest name, slo = SLO name, and objective = str(target). The expression is signed: 1 means a full budget and a negative value means overspent. See ../nthlayer-generate/src/nthlayer_generate/recording_rules/manifest_builder.py:131-210. generate's dispatch uses `"availability" in (slo.slo_type or slo.name).lower()` over get_standard_slos(), which excludes judgment SLOs.

nthlayer-common 5.10.0 owns the name as `ERROR_BUDGET_REMAINING`, with `selector(**labels)` producing `metric{k="v",...}` (see ../nthlayer-common/src/nthlayer_common/manifest/slo_series.py).

This diff makes three changes:
- measure keys its error-budget branch on `slo.slo_type == "availability" and not slo.judgment_type`, where it used to key on name == "availability", and filters the query by slo=name.
- correlate queries `min(selector(service=...))`.
- drift builds its selector with the contract.

Verify the following:
- Measure's new predicate selects exactly the SLOs generate writes an error budget for. Compare the equality against generate's substring check, using the values common's parsers actually produce for slo_type.
- Judgment SLOs keep their previous routing, both those with a builder and those without.
- The breach rule `current < 0.0` still holds for the new query.
- correlate's min() result parses through `_query_instant`.
- No caller depended on the old query string. Look at evaluation_custom_metadata, verdict metadata and dedupe keys.

Report a bullet list of CRITICAL / IMPORTANT / MINOR. Keep it under 350 words. "No correctness issues found" is a valid result.