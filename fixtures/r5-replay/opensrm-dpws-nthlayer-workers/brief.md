# opensrm-dpws: populate trigger_service on retrospective metadata.custom (jmy.21 enabler)

opensrm-jmy.21 added the add_dependency recommendation type, which reads `incident_custom["trigger_service"]` to identify which service's manifest gets patched. Currently no code populates that key on retrospective metadata.custom, so analyze_incident's add_dependency heuristic always emits log.debug + [] in production.

Scope:
- Decide the source of truth for trigger_service: the incident verdict's primary service, the correlation_snapshot's domain.service, or both with precedence?
- Wire build_retrospective() in nthlayer-workers/learn/retrospective.py to populate metadata.custom["trigger_service"]
- Wire the worker path (LearnRetrospectiveModule in worker.py) similarly when it builds retrospective data in-memory
- Tests asserting the field is populated correctly across both paths
- Verify add_dependency recs actually fire in an integration test (currently nthlayer/test/learn-recommendations-integration.sh ships the recs CLI but add_dependency emission is silent until this lands)

Design questions:
- Single field "trigger_service" or richer "triggering_context"? Single is simpler.
- Backward-compat: pre-existing tests must continue to pass when the field is absent (already handled in jmy.21's analyze_incident: log.debug + []).

Priority: P3 — unblocks jmy.21's add_dependency emission in production. Without this, the recommendation type ships but never fires.
Effort: 0.5-1 session including R5.
Discovery: jmy.21 P4 R5 review (excellence pass noted the silent no-op as a back-compat-acceptable behaviour that needs upstream wiring).
