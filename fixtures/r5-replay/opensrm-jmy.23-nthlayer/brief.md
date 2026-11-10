# opensrm-jmy.23: financial_impact field on SpecRecommendation (jmy.1 follow-up)

Add `financial_impact` field to SpecRecommendation so each generated recommendation carries the per-incident financial impact estimate. Pulls from `nthlayer_common.outcomes.compute_financial_impact` (shipped in jmy.1).

Discovered during jmy.6 scoping (2026-05-26). Originally listed as item #4 in jmy.6 but blocked on jmy.1 (now closed). With outcomes foundation in place, this becomes an additive enrichment that doesn't fit in jmy.6's "loop foundation" framing.

Scope:
- Add `financial_impact: FinancialImpact | None = None` field to SpecRecommendation dataclass
- In `analyze_incident()`, when the incident's verdict chain has outcomes bound (via apply_override_to_verdict or otherwise), call `compute_financial_impact(outcomes, decisions_affected=..., failure_mode=..., volume_source=...)` and attach to each recommendation
- Surface the field in the recommendation's YAML output: e.g.
  ```yaml
  estimated_impact:
    amount: 5400
    currency: USD
    decisions_affected: 1200
    failure_mode: false_positive
    volume_source: metric
  ```
- Operator-facing rationale: "this incident cost $X; tightening the SLO target by Y prevents the next one of these"
- Tests for new field on existing recommendation types (tighten_slo, add_deploy_gate, add_dependency)

Design questions to resolve:
- Should the field appear inline in each recommendation, or as a top-level "impact" block applied to the whole recommendation set? Per-recommendation is more flexible if the engine emits multiple recommendations for a single incident.
- What's the volume_source default when both metric and spec_estimate are available — prefer metric (real-time data) or spec_estimate (deterministic)?

Priority: P3 — adds business-impact framing to recommendations; not required for the loop itself.
Effort: 1 session including R5 (small surface, foundation already in jmy.1).
Discovery: jmy.6 scoping (decomposition approved 2026-05-26).
Dependency: blocks-on jmy.6 (the recommendations engine integration surface lands there first).
