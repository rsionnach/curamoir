PASS 1 (CORRECTNESS) sequential R5 review on bead opensrm-wo62.4.6.

Template body (from ~/.claude/skills/rule-of-five-review/correctness-reviewer.md):
---
You are reviewing code changes with a SINGLE FOCUS: correctness.
Do not comment on naming, style, documentation, or "nice to have" improvements. Only flag things that are wrong, broken, or will produce incorrect results.
Checklist: logic errors; type mismatches across function boundaries; error handling / uncaught propagation; unguarded null access; off-by-one; races; return paths; API contracts (do signatures match callers?); state mutations; resource cleanup.
What to read: the diff between BASE and HEAD; the requirements below; for each changed function, its callers.
Output: "### Correctness Issues" with Critical / Important / Minor (each: short description, File: path:line, What's wrong, Why it matters, Suggested fix), then "### Correctness Assessment" with Verdict PASS / PASS WITH FIXES / FAIL and 1-2 sentence reasoning.
---

Your review scope is THE DIFF, not the whole file at HEAD. Run:
  git -C <workspace>/nthlayer-generate-wo62-4-6 diff 8d80d7e31b5de9b53f6fe0b9e746b2eb3a023c0d..f733dd30a05c6ec093a1938c1de75f144c7aa689
(the diff is large because it deletes 355 golden data files under tests/golden/data/legacy_*; use `-- . ':!tests/golden/data'` to exclude them, and `--stat` first to orient). Do not raise findings on code outside that diff. For the pre-change view of a file, use `git -C <path> show 8d80d7e:<file>`; for the post-change view, read the file at the path directly. This is a Python repo (nthlayer-generate); it depends on the sibling checkout <workspace>/nthlayer-common (read-only reference; legacy loading now lives in nthlayer_common/manifest/parser/{loader,legacy}.py). Do NOT edit any files.

Affected repos:
nthlayer-generate: base=8d80d7e31b5de9b53f6fe0b9e746b2eb3a023c0d, head=f733dd30a05c6ec093a1938c1de75f144c7aa689, path=<workspace>/nthlayer-generate-wo62-4-6

Bead description (opensrm-wo62.4.6, "generate 4.0.0: delete the legacy parse_service_file/ServiceContext stack"):
Delete specs/parser.py, render_resource_spec, the legacy builders, validator's legacy route, specs/__init__ legacy exports, and the unwired dependencies and dashboard-validate commands. Breaking: release as a major (4.0.0). Rulings from Rob:
- F2: the ServiceContext/Resource dataclasses in specs/models.py STAY as an internal adapter, used by dashboards and the metrics recommender (their rewrite is wo62.4.20).
- F3: the DELETE-SAFE list is specs/parser.py (+render_resource_spec), environments.py, environment_merger.py, variable_substitution.py, template.py; generate_sloth_spec, convert_to_sloth_slo; generate_alerts_for_service, extract_dependencies, infer_technology_from_name; the loki service-file path and extract_dependencies_from_resources; recording_rules/builder.py, build_dashboard, sdk_adapter.create_service_dashboard; dashboards/validator extract_technologies and extract_custom_overrides; cli/dependencies.py, cli/dashboard_validate.py (incl. list_intents_command), slos/dependencies.py, scripts/regenerate_hybrid_dashboards.py; ResourceDetector; test_golden_legacy_generators.py and its 12 legacy_* data dirs. CASES/ENV_CASES move into tests/golden/_corpus.py first.
- H1: tests/golden/_corpus.no_user_templates must also hide nthlayer-common's manifest-relative _find_template_dir walk-up (nthlayer_common.manifest.parser.loader), not just generate's CustomTemplateLoader.
- H2: two assertions are ported onto the manifest path: test_init's generated-file check (via load_manifest) and an unknown-template rejection test (via validate_service_file). Others are deferred to wo62.4.22.
- H3: two already-@skip'd dashboard test classes written against a dead object API are deleted.
The invariant: CLI behaviour is unchanged; no surviving golden changed.

Correctness-specific things worth checking: any remaining src/ or tests/ import or call of a deleted name (including lazy imports inside functions and getattr strings); `__all__` entries naming removed symbols; that the monkeypatch target in _corpus.no_user_templates is the lookup path both loader.py callers actually use; that the fixture provenance headers' new line citations point at the code they claim; that the README 4.0.0 replacement table names functions that exist with compatible meaning.

Focus: correctness only. Report format: bullet list of CRITICAL / IMPORTANT / MINOR. Under 350 words. "No correctness issues found" is a valid result.