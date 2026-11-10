PASS 1 (CORRECTNESS) sequential R5 review on bead opensrm-h0at (checkpoint 3: the nthlayer front-door slice).

Template: read ~/.claude/skills/rule-of-five-review/correctness-reviewer.md and follow it. SINGLE FOCUS: correctness only.

Scope is THE DIFF:
  git -C <workspace>/nthlayer diff 5feca5b..f4ec2fb
The files are test/test_error_budget_seam.py (new), .github/workflows/ci.yml (new job error-budget-seam), docs/metrics-contract.md and CLAUDE.md.

Context (opensrm-h0at): measure (nthlayer-workers) read `slo:error_budget:ratio`, which nothing writes. That is fixed in common 5.10.0, generate 4.0.9 and workers 4.0.1. This slice adds a front-door seam test. For every manifest in opensrm/spec/v2/examples, nthlayer-generate/examples/services and nthlayer-generate/examples/opensrm, it checks that the set of error-budget series generate writes (via build_recording_rules_from_manifest, filtered on ERROR_BUDGET_REMAINING.metric, rendered with .selector(service, slo)) equals the set of queries that measure's load_specs gives with query_kind == "error_budget". It runs in CI against the published packages, with uv --isolated --no-project. The supervisor ran it locally: it passes on generate 4.0.10, workers 4.0.2 and common 5.10.1, and it fails on workers 4.0.0.

Verify:
- the test can't pass vacuously. Check that the corpus is found, that there is a minimum count, and that manifests the loader rejects are skipped only for that reason. A broad `except Exception` in _load: could it hide a real regression? Consider a manifest that used to load now failing, and so silently dropping out.
- the CI job's checkout layout makes `Path(__file__).parents[2]` resolve to the directory holding opensrm/ and nthlayer-generate/. Actions checkout `path:` is relative to $GITHUB_WORKSPACE.
- uv's flags: are --isolated, --no-project and --python 3.11 valid together for `uv run`? Do the published packages support 3.11?
- the doc edits are accurate.

Report a bullet list of CRITICAL / IMPORTANT / MINOR. Keep it under 300 words. "No correctness issues found" is a valid result.