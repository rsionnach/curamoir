# opensrm-jmy.24: per-recommendation CLI selection (--include / --exclude flags on --apply-to)

Add per-recommendation CLI selection flags to `nthlayer-workers learn recommendations --apply-to` (e.g. `--include <id-list>`, `--exclude <id-list>`, or `--select-ids`). Lets operators apply a subset of a retrospective's recommendations without the all-or-nothing default.

Discovered during jmy.6 challenge-the-premise (2026-05-26). Original jmy.6 ships all-or-nothing application by deliberate v1.5 simplification — retrospectives typically produce 1-3 recommendations, so apply-all is reasonable. But operators will sometimes want to apply 2 of 3, defer the third for review.

Scope:
- Add stable recommendation IDs to SpecRecommendation (deterministic per (incident_id, recommendation_type, target_path)) so the CLI can reference them
- Add `--include <id>[,<id>...]` and `--exclude <id>[,<id>...]` flags to `--apply-to`
- Default: apply all (current jmy.6 behaviour)
- Tests: apply-subset, exclude-subset, both-flags-together-error (mutually exclusive)

Overlap with opensrm-jmy.22 (interactive TUI): jmy.22 covers the TUI selection UX; this bead covers the CLI-flag selection UX. They're distinct surfaces but share the underlying mechanic of "operator chose a subset of the recommendation set." If jmy.22 lands first and the CLI selection is implementable as a thin wrapper around the same selection primitive, fold this into jmy.22 instead of shipping separately.

Priority: P3 — operator-ergonomics enhancement on top of the Learn→Spec loop foundation.
Effort: < 1 session including R5 (small surface).
Discovery: jmy.6 challenge-the-premise (decomposition approved 2026-05-26).
Dependency: depends-on jmy.6 (the recommendation IDs + --apply-to machinery land there first).
