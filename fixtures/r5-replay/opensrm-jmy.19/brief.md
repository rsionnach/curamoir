# opensrm-jmy.19: bench post_incident: surface mutation-style override attribution alongside lineage-style outcome_resolution rendering

Discovered during jmy.18 consumer verification (2026-05-20). After jmy.18 lands the sidecar→core POST path, the workers-side post_incident renderer (nthlayer-workers/respond/sre/post_incident.py) automatically surfaces override attribution because it already reads outcome.override.* off the original verdict (mutation-style). The bench-side renderer (nthlayer-bench/sre/post_incident.py) walks lineage to find outcome_resolution children (lineage-style) and does NOT read outcome.override, so the bench-side post-incident output remains blind to operator overrides even after jmy.18 ships.

Scope:
- Extend nthlayer-bench/sre/post_incident.py to read outcome.override.* off original verdicts in addition to its existing outcome_resolution lineage walk
- Render override attribution ("verdict X was overridden by operator Y at time Z for reason R") alongside the existing outcome_resolution rendering
- Both patterns coexist — overrides surface mutation-style data; resolutions surface lineage-style data

Acceptance:
- A verdict with outcome.status="overridden" and a populated outcome.override renders override attribution in bench's post-incident output
- Existing lineage-walk behavior for outcome_resolution children is unchanged
- Both bench and workers-side renderers produce parity-shaped output for the same incident

Effort: 1 session including R5
Priority: P3 — required for bench/CLI parity with workers-side rendering; matters if demos or operator workflows include bench's post-incident output
Discovery: jmy.18 consumer verification (sidecar→core POST reframe via challenge-the-premise)
Dependency: depends-on jmy.18 (the mutation-style writer must exist before the bench-side reader is useful)
