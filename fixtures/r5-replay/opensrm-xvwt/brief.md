# opensrm-xvwt: iter_manifest_files: same-stem .yaml/.yml pair loads a service twice

Surfaced by the opensrm-fxln edge-cases R5 pass; not fixed there because the gate was mid-run on nthlayer-workers and the fix belongs in nthlayer-common, which must be gated and released first (CLAUDE.md: cross-repo beads gate the shared repo first).

nthlayer-common's iter_manifest_files() returns every file whose suffix is .yaml or .yml, sorted. A directory holding both svc.yaml and svc.yml — the ordinary result of an editor default changing, or two people adding the same service — yields BOTH. Neither is foreign YAML, so foreign_yaml_reason() passes them and both parse.

Consequences, both silent:

1. measure: load_specs appends the service's SLOs twice. evaluate_slos then writes two verdicts per window for the same service/SLO, and count_consecutive_breaches counts verdicts, not windows — so a 3-window hysteresis threshold is reached in 2 real windows (or fewer). Judgment SLOs fire early, which is the same class of wrong as firing never.
2. observe: the same duplication inflates per-service SLO counts and any budget arithmetic derived from them.

Nothing errors and nothing warns; both files are valid manifests.

Proposed fix, in nthlayer-common/src/nthlayer_common/manifest/scan.py:
- iter_manifest_files() detects same-stem collisions, keeps one deterministically (.yaml wins — it is the suffix the ecosystem writes), and reports the dropped path so callers can surface it.
- Returning the collision rather than only logging it matters: this is exactly the partial-view problem opensrm-3470 added parse_failures for, and the operator reading SLO output is who needs to know.

Test provenance: build the fixture from two real manifests the parser accepts, not from a shape chosen to trip the collision check.

Affects nthlayer-common (fix), and nthlayer-workers observe + measure (consume the report).
