# opensrm-tu04.3: P3-DOC.3: Manifest authoring guide

OpenSRM v2 manifests: SLOs, judgment SLOs (8 types), contracts, deps, templates. v1 migration.

SCOPE DECISION 2026-08-14 (Rob): target OpenSRM v2, NOT v1. v2 (OPENSRM-CORE-v2.md) defines the full 8 judgment SLO types the bead calls for: reversal_rate, high_confidence_failure, audit_sampling, outcomes, escalation, segments, stability, calibration (kind: JudgmentSLO / judgment_type syntax), plus the v1->v2 migration story. v1 on main has only ~4 schematized judgment constructs and no migration — do not write the guide against v1.

BLOCKED on:
- opensrm-7774: land v2 spec docs from the deferred chore/specs-and-docs branch onto main.
- opensrm-k7xw: author v2 schema.json so the '8-type worked examples parse correctly' acceptance is achievable (no v2 validator exists today).

Then: write guide + example per service type + 8 worked judgment-SLO examples + v1->v2 migration, all validated against the v2 schema.

## Acceptance

Example per service type. Worked examples for all 8 judgment SLO types. Examples parse correctly.
