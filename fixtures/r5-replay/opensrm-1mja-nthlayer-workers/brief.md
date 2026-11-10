# opensrm-1mja: End-to-end integration test for add_dependency emission (dpws follow-up)

Background: opensrm-dpws (closed 2026-05-30) wired trigger_service + declared_dependencies_by_service into both retrospective code paths, enabling jmy.21's add_dependency heuristic to produce recommendations. dpws design § 3.7 deferred the end-to-end integration test ("seed incident -> run learn retrospective -> run learn recommendations -> assert add_dependency recs in plan") to jmy.6's scope, but jmy.6 closed (2026-05-28) before dpws landed, so the test never materialised.

Scope (tight):
- Extend nthlayer/test/learn-recommendations-integration.sh with an add_dependency scenario:
  - Construct a plan YAML containing one add_dependency Recommendation (field=spec.dependencies[+], proposed_value={name: svc-new, type: unknown})
  - Construct a minimal target manifest in SPECS_DIR
  - Run nthlayer-learn recommendations --from plan.yaml --apply-to SPECS_DIR
  - Assert exit code 0, manifest now contains svc-new in spec.dependencies, apply summary lists one applied rec
- Asserts the apply layer's [+] sigil + classify_outcome implementation from jmy.21 is wired end-to-end via the CLI command surface

Out of scope:
- Implementing _build_plan_from_incident (currently NotImplementedError stub in cli.py:327; that is its own bead, would need CoreAPIClient.get_retrospective_for_incident)
- Three-tier integration (would belong in integration-three-tier.sh)
- Demo orchestrator wiring

Effort: 0.5-1 session including R5.
Discovery: opensrm-dpws design section 3.7 (deferral note).
