# opensrm-jmy.6: Spec patch generation + CLI --apply-to / --pr (Learn→Spec follow-up)

Follow-up to opensrm-jmy.2 (MVP landed: SpecRecommendation model + tighten_slo + add_deploy_gate engine + tests).

Remaining scope from NTHLAYER_MISSING_CAPABILITIES_SPEC.md § 2:

1. add_dependency recommendation type — needs correlator/topology integration to detect undeclared dependencies discovered during the incident. The retrospective doesn't currently expose this; the engine needs to either consume topology assessments or be passed a "dependencies discovered" set.

2. CLI integration in nthlayer-workers/learn:
   - `nthlayer-workers learn recommendations --incident INC-X --output FILE` (basic emission — likely small)
   - `--apply-to <specs-dir>` — load existing manifests, apply patches as a git diff. Requires manifest write-back (only the v2 parser currently writes via convert_v1_to_v2).
   - `--interactive` — TUI walkthrough (accept/reject/modify each recommendation)
   - `--pr` — git branch + GitHub PR creation. Integrates with `gh` CLI; needs auth handling.

3. Spec patch generation — the structured proposed_value dict needs to land at the correct YAML path in the spec file. May need a deep-merge helper that preserves comments/order (PyYAML default loses comments).

4. financial_impact field — depends on opensrm-jmy.1 (business outcome binding) being implemented so spec.outcomes carries a per-failure cost figure.

Foundation in place (commit nthlayer-workers/learn/recommendations.py): SpecRecommendation + Recommendation dataclasses, analyze_incident(), to_yaml() serialisation, _tighten_slo_recommendations + _add_deploy_gate_recommendations heuristics, 22 tests.

Filed by jmy.2 close.
