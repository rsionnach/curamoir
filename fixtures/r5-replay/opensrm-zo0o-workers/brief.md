PASS 1 (CORRECTNESS) sequential R5 review on bead opensrm-zo0o, checkpoint 3 (nthlayer-workers).

# Correctness Reviewer
You are reviewing code changes with a SINGLE FOCUS: correctness. Do not comment on naming, style, documentation, or "nice to have" improvements. Only flag things that are wrong, broken, or will produce incorrect results.

Checklist: logic errors; type mismatches across function boundaries; error handling and uncaught propagation; unguarded None access; off-by-one; races; return paths; API contracts (do signatures match their callers?); state mutations; resource cleanup.

What to read: (1) the diff; (2) the requirements below; (3) for each changed function, its callers, to verify the contract holds.

Your review scope is THE DIFF, not the whole file at HEAD. Run:
  git -C <workspace>/nthlayer-workers-zo0o-measure-budgets diff 444ef91..def4bb0
Do not raise findings on code outside that diff. Pre-change view: `git -C <path> show 444ef91:<file>`; post-change: read the file at that path.

Affected repo: nthlayer-workers: base=444ef91, head=def4bb0, path=<workspace>/nthlayer-workers-zo0o-measure-budgets

Requirements (bead opensrm-zo0o, rulings by the owner):
- Z1: every classical SLO is good/total against its objective; nthlayer-generate (>=4.1.0, sibling checkout at ../nthlayer-generate, see src/nthlayer_generate/recording_rules/manifest_builder.py `_slo_kind`, `_budget_skip_reason`, `_error_budget_rules`) writes `slo:error_budget_remaining:ratio` labels {service, slo, objective} for each availability, latency and error_rate SLO. measure evaluates every such SLO through the budget path (breach when < 0), no per-kind logic. The contract name/labels are nthlayer-common's `ERROR_BUDGET_REMAINING` (../nthlayer-common/src/nthlayer_common/manifest/slo_series.py).
- D5: throughput, freshness and lag stay budget-less; measure reports them not evaluable (E3 style, reason no_data_source) rather than querying a series nobody writes.
- Z2: correlate uses bottomk(1, ...) keeping the slo label; drift targets only SLOs that have a budget.
- Design decision D-CP3a (implementer's): worker-mode drift discovers budgeted SLOs from Prometheus with `group by (slo) (budget{service=...})`, once per service per cycle; a failed lookup skips the service with one warning. Manifest dicts in worker mode come from nthlayer-core GET /manifests (../nthlayer-core/src/nthlayer_core/catalogue.py) and lack threshold_ms/good/total queries.
- The latency_seconds kind, duration-unit table, _target_seconds and SLODefinition.unit are removed; "ratio" remains only for judgment types with no builder.

Verify in particular: that _classical_kind agrees with generate's _slo_kind on every input generate sees (so measure asks for exactly the budgets generate writes); the slo label values measure/drift filter on equal what generate writes (str of the SLO name; nthlayer-common selector renders with str()); that bottomk's returned labels are parsed correctly; that nothing else in nthlayer-workers (CLI, worker-mode MeasureModule, observe) still constructs SLODefinition with unit= or depends on removed names; and that evaluate-once's reporting of not-evaluable SLOs is correct.

Tests: `cd <path> && PYTHONDONTWRITEBYTECODE=1 uv run pytest -q -p no:warnings` (currently 2322 passed, 1 known skip). Do not modify files.

Report format: bullet list of CRITICAL / IMPORTANT / MINOR, each with file:line, what's wrong, why it matters, suggested fix. Under 350 words. "No correctness issues found" is a valid result. End with Verdict: PASS / PASS WITH FIXES / FAIL.