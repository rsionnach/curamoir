PASS 1 (CORRECTNESS) sequential R5 review on bead opensrm-9dbl. Work quickly; short foreground commands only; do not modify files.

First, skim your focus instructions in ~/.claude/skills/rule-of-five-review/correctness-reviewer.md.

Scope: test code only (generated golden JSON is output; sample only as needed):
  git -C <workspace>/nthlayer-generate-9dbl diff 44350d6..c6f0a57 -- tests/golden/test_golden_legacy_cli.py
Affected repos: nthlayer-generate: base=44350d6, head=c6f0a57, path=<workspace>/nthlayer-generate-9dbl

Bead: `cd <workspace>/opensrm && bd show opensrm-9dbl`. TESTS ONLY: six new SINGLETONS cells pinning legacy CLI non-JSON output formats and non-default flags (plan table/markdown; apply dry-run --env and PagerDuty-failure warnings; recommend-metrics table/yaml/level; validate-slo table; generator dry-run flags + alerts runbook_url; ownership demo), plus helpers (_sorted_dependency_extractors, apply "<duration>" normalisation, a raising PagerDuty resource-manager stub with an API key set).

Correctness questions: does each cell exercise the format/flag it claims, through the legacy entry point? Do the new normalisations (sorted dependency extractors, duration masking) hide anything a later switch could legitimately change? Is the PagerDuty-failure stub patched at the use-site lookup and impossible to reach a real network even with the API key set? Any cell that could leak state (env vars, monkeypatches) into other cells?

Report: CRITICAL / IMPORTANT / MINOR bullets with file:line and a concrete failure scenario. Under 300 words. "No correctness issues found" is valid.