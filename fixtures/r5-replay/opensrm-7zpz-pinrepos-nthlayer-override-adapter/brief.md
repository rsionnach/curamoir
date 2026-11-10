PASS 1 (CORRECTNESS) sequential R5 review on bead opensrm-7zpz (Phase C, pin-only repos). READ-ONLY: never modify files, never git checkout/switch/stash/reset; no background work.

Template: read ~/.claude/skills/rule-of-five-review/correctness-reviewer.md and follow it.

Scope — THE DIFF in each repo:
- git -C <workspace>/nthlayer-generate diff 06b3106..6ff84d5
- git -C <workspace>/nthlayer-bench diff 6771ea3..aaff2ef
- git -C <workspace>/nthlayer-override-adapter diff 82b0da5..bdf493c

Change: nthlayer-common pin >=4.0.0,<5.0.0 → >=5.0.0,<6.0.0, relock, release-smoke EXPECTED_MAJORS 4→5; generate also gains a WHY-5.0 pin comment and the ported test_declared_ceiling_is_exactly_the_next_major guard.

nthlayer-common 5.0.0 (sibling ../nthlayer-common on main; read docs/upgrading-5.0.md and its CHANGELOG 5.0.0 section): closed judgment_threshold entries; segments require metric; srm/v1 lift (used by generate's `nthlayer migrate-manifest` via v1_compat.convert_v1_to_v2) rejects keys it would drop, honours feedback_latency unit, feedback_latency window only absent/30d; threshold_to_v2_entry public and validating. Verify independently that each repo has no code path broken by 5.0: grep every nthlayer_common import in src/ and tests/; for generate specifically, exercise `migrate-manifest` against every srm/v1 example/fixture in the repo that has judgment thresholds (examples/opensrm/*.yaml, tests fixtures) — does any now get rejected where it previously migrated (e.g. a feedback_latency with a non-30d window, a description/labels, an unknown key)? Also check generate's own srm/v1 examples for keys the 5.0 lift now rejects. Check floors equal the version under test and uv.lock resolves 5.0.0.

Run suites: cd <repo> && uv run pytest -q -o addopts="" (expected: generate 3893 passed 20 skipped; bench 329; adapter 121).

Focus: correctness only. CRITICAL / IMPORTANT / MINOR under 350 words. End with `git -C <repo> status --short` for all three, verbatim.