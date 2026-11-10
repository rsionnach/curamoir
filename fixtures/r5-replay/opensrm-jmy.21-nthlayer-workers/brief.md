# opensrm-jmy.21: add_dependency recommendation type (correlator/topology integration)

Add a new recommendation type `add_dependency` to nthlayer-workers/learn that detects undeclared dependencies discovered during an incident and proposes them as manifest patches.

Discovered during jmy.6 scoping (2026-05-26). The original jmy.6 description listed `add_dependency` as item #1 but it has a separate concern from the rest of the bead — it requires correlator/topology integration to detect the dependencies in the first place.

Scope:
- Define `add_dependency` recommendation shape on SpecRecommendation (target spec path under `dependencies`, source incident, confidence)
- Integrate with topology_drift / contract_divergence assessments (or accept a "dependencies discovered during incident" set as input) to identify undeclared deps that participated in the cascading failure chain
- Heuristic: a service Y appeared in the correlation chain for service X's incident, but X's manifest doesn't list Y as a dependency
- Tests for the new recommendation type's emission
- Wire the new type through `analyze_incident()` and through jmy.6's CLI surface (the CLI emits whatever recommendations the engine produces; new type doesn't need new flags)

Design questions to resolve:
- Does the engine pull topology from assessments, or accept a passed-in dependencies set? Probably the former (decouples engine from caller).
- What's the confidence model — same as tighten_slo/add_deploy_gate, or different signal (chain participation count)?

Priority: P3 — improves retrospective recommendation breadth; doesn't block the Learn→Spec loop foundation which jmy.6 ships.
Effort: 1-2 sessions including R5.
Discovery: jmy.6 scoping (decomposition approved 2026-05-26).
Dependency: depends-on jmy.6 (the CLI surface that consumes the new recommendation type lands there first).
