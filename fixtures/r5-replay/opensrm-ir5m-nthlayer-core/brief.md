# opensrm-ir5m: five siblings: admit nthlayer-common 3.0.0, and absorb what it breaks in workers

nthlayer-common 3.0.0 is published, and all five consumers resolve it from the
sibling path source, so their suites were already running against it while every
declared range still said `>=2.1.2,<3.0.0`. eco-doctor reported ten findings —
STALE and SIBLING>CEILING for each of the five — which are range-class findings,
so `/r5-supervise`'s pre-flight was refusing to dispatch pass 1 in ANY of them.
Every R5 gate in the workspace was blocked by the release.

## The range

`>=3.0.0,<4.0.0` in all five. The ceiling alone is not enough and the floor is
not scope creep: `test_declared_floor_is_the_version_under_test` is red at
`>=2.1.2,<4.0.0`, and its own docstring says "Widening the published range to
make this pass is how the bug got here" — 2.1.2 would be published as supported
while nothing has run it, and on the v2 path 2.x parses SLO targets 100x
differently (opensrm-ocvu). `EXPECTED_MAJORS` 2 -> 3 for the same reason.

generate, core, bench and override-adapter had ONLY the four range-guard
failures. Counts close exactly afterwards (3542+4=3546, 252+4=256, 324+4=328,
116+4=120), so no behavioural change in any of them.

## What 3.0.0 actually broke, all in workers

1. `tests/measure/test_prometheus.py` asserted dispatch-on-judgment_type using
   `maximum_reversal_rate: 5.0`. Never legal — opensrm v2 types the field as
   Ratio [0, 1] — but common 2.x accepted it and complemented it to -400.0,
   which the load-time validator skips because it only flags targets in (0, 1).
   So the adapter compared a measured ratio against -400.0: never a breach, for
   an SLO whose entire purpose is to breach. A fixture written from what the
   parser took, not from what the spec requires. Corrected to 0.05 (exercises
   dispatch identically) plus a new test pinning the rejection via
   `parse_failures`, not log text.

2. The same-stem duplicate case moved upstream (opensrm-xvwt): common drops
   `svc.yml` at the scan and raises `ManifestCollisionWarning`, so workers never
   sees it and `manifest_duplicate_skipped` no longer fires. Outcome assertions
   unchanged and still passing; only the trace assertion moved to the warning
   that now carries it.

   The consequence worth the bead: `_load_manifests_from_specs` keys on
   `manifest.name`, not the stem, so its duplicate branch is still LIVE for two
   different stems declaring one service — and the same-stem test was its ONLY
   coverage. 3.0.0 left a live branch with zero tests.
   `TestDuplicateServiceAcrossDifferentStems` restores it, kill-checked (removing
   the branch turns it red). Its fixture sorts after `svc-good.yaml` on purpose:
   `svc-good-copy.yaml` sorts BEFORE it ('-' 0x2D vs '.' 0x2E) and would have
   asserted the wrong file was skipped.

## Gap found, not fixed here

workers is the only one of the five with NO `test_dependency_declarations.py`
and no `EXPECTED_MAJORS` guard, so nothing in its own suite fails when its
declared range drifts from the version under test — only eco-doctor catches it.
It is also the repo the 3.0.0 upgrade notes name by name. Recorded in its
pyproject comment; a guard for workers deserves its own bead.

## State

Branch `bump-common-3` in all five, committed, nothing pushed:
generate 37b2ee4 · core 894a3cd · bench 98d5e52 · override-adapter 0859dad ·
workers 8abccc0. eco-doctor after: 8 repos checked, no drift, exit 0.
Suites green in all five; ruff clean in workers.
