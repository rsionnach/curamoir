PASS 1 (CORRECTNESS) sequential R5 review on bead opensrm-pnmv (Checkpoint B: nthlayer-common 5.3.0).

Read ~/.claude/skills/rule-of-five-review/correctness-reviewer.md and apply it as your focus.

Scope is THE DIFF: `git -C <workspace>/nthlayer-common-pnmv diff 0c7377b..f7ab272`. Pre-change view: `git show 0c7377b:<file>`. Read-only (scratch probes with `uv run --frozen python` from that worktree are fine; never modify the repo).

Bead: NthLayer rejects `apiVersion: opensrm/v1` (the published OpenSRM v1 schema, opensrm/spec/v1/schema.json) with an actionable error, and a new pure converter `nthlayer_common.manifest.opensrm_v1.convert_opensrm_v1_to_v2(data, *, service_type=None) -> MigrationResult(manifest, unmapped)` converts it to `opensrm.nthlayer.io/v2` following opensrm/spec/v2/AUTHORING.md §7, reporting every field with no v2 home instead of dropping it. Also: common's v2 parser accepts `resource:` dependency refs; `is_manifest_file` recognises opensrm/v1 by content.

Authorities: spec <workspace>/nthlayer/docs/superpowers/specs/2026-10-22-pnmv-opensrm-v1-migration-design.md (Revision 1 — §4 field map, §5 errors); decision doc on front-door branch docs/pnmv-opensrm-v1 (docs/superpowers/decisions/opensrm-v1-support.md); spec sibling <workspace>/opensrm (v1 schema, v2 schema, AUTHORING §7, examples). Controller rulings made during implementation (treat as decided unless wrong against the spec): <workspace>/nthlayer/.superpowers/sdd/2026-10-22-pnmv-opensrm-v1-migration/progress.md.

Focus on correctness of the whole: does every converted value carry the v1 semantics into v2 correctly (ratios never ×100; error_rate complemented; windows preserved; entity refs built right; owner/escalation/annotations; contract promise formats) as read back by parse_opensrm_v2 and validated by spec/v2/schema.json; is anything converted that §4 says is unmapped, or vice versa; can any schema-valid v1 document crash the converter with a non-ValueError or produce output that fails the v2 schema or parser; are the loader rejection and the resource: parser change correct and free of regressions for srm/v1, v2 and legacy manifests.

Report: CRITICAL / IMPORTANT / MINOR bullets with file:line, under 400 words. "No correctness issues found" is valid.