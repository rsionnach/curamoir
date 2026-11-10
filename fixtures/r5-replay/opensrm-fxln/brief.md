# opensrm-fxln: nthlayer-workers: measure adapter silently ignores every v2 manifest

Found during opensrm-3470's design, scoped out of it deliberately, then
found to be considerably worse than the note that deferred it.

src/nthlayer_workers/measure/adapters/prometheus.py:40 load_specs()
NOTE: a DIFFERENT function from observe/slo/spec_loader.load_specs, same
name, imported by measure/cli.py:118.

IT DOES NOT USE load_manifest. It hand-rolls v1 parsing:

    raw = yaml.safe_load(spec_file.read_text())
    metadata = raw.get("metadata", {})
    service  = metadata.get("name", spec_file.stem)
    spec     = raw.get("spec", {})
    slo_defs = spec.get("slos", {})        # <- v1 shape ONLY

`spec.slos` is the srm/v1 dict-of-dicts. An opensrm.nthlayer.io/v2
manifest carries `spec.slo` (a list of OpenSLO documents) and
`spec.judgment_slo`. So for a v2 manifest slo_defs is {} and the service
contributes ZERO SLOs, with no error and no warning.

REPRODUCED (one v1 manifest, one v2 manifest, one .yml copy of the v1, in
one directory):

    SLOs found: 1
        legacy availability
    v2 manifest contributed: 0 SLOs
    .yml manifest contributed: NOT SEEN

This is not the oh27/3470 bug with a different file name. Those were
"a manifest that failed to parse is dropped uncounted". This one PARSES
FINE and yields nothing, for the format the whole ecosystem migrated to.
opensrm-ih0v made spec.service.type required on v2 and the shipped
archetypes are all v2; anything measured through this adapter that has
been migrated is silently unmeasured.

FOUR DEFECTS, IN DESCENDING SEVERITY

1. v2 manifests yield zero SLOs, silently. See above.
2. `.glob("*.yaml")` misses `.yml`. Same silent-subset-by-extension bug
   opensrm-oh27 fixed in the retrospective path. nthlayer_common now
   exports iter_manifest_files for exactly this (opensrm-3470).
3. `except Exception` logs but does not COUNT, and load_specs returns a
   bare list, so a caller cannot tell a partial view from a complete one
   — the 3470 fix, not yet applied here. `if not isinstance(raw, dict)`
   drops without even a log.
4. `service = metadata.get("name", spec_file.stem)` falls back to the
   FILENAME. A manifest missing metadata.name is silently attributed to a
   service named after its file, so its SLOs are measured against the
   wrong service rather than not at all. Arguably worse than dropping.

IT ALSO REIMPLEMENTS WHAT nthlayer-common OWNS
  - the judgment-SLO type list (reversal_rate, high_confidence_failure,
    calibration, feedback_latency) — note it disagrees with
    JUDGMENT_SLO_TYPES, which has eight members and does not include
    feedback_latency
  - the 99.9 -> 0.999 target normalisation, which nthlayer-common does at
    the OpenSLO boundary (see CLAUDE.md hard rule 1 on the 0-100 vs 0.0-1.0
    convention — getting this wrong in a second place is exactly the
    wrong-side mistake that rule exists to prevent)

WORK
1. Decide first: convert this adapter to load_manifest, or keep the
   hand-rolled reader? Converting fixes 1, 2 and 4 structurally and
   deletes both reimplementations. The reason to hesitate is that this
   returns measure's own SLODefinition, not nthlayer_common's, so the
   conversion has a mapping layer — check whether measure's SLODefinition
   still earns its existence or should be the common one.
2. Then apply the 3470 shape: iter_manifest_files, foreign_yaml_reason,
   log-and-count, and a result object carrying parse_failures.
3. Add a test asserting a v2 manifest produces SLOs. There is currently
   NO test covering v2 through this path, which is why a whole-format
   blind spot survived the migration.

ACCEPTANCE
- A directory holding one v1 and one v2 manifest, one .yaml and one .yml,
  yields SLOs from all of them.
- A manifest without metadata.name is reported, not renamed after its file.
- parse_failures reaches the caller.

RELATED: opensrm-oh27 (retrospective, closed), opensrm-3470 (observe,
merged). This is the third and worst instance of the family.

LABELS: v1.5
