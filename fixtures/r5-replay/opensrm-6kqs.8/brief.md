PASS 1 (CORRECTNESS) sequential R5 review on bead opensrm-6kqs.8 (synthetic r5_baseline fixtures), nthlayer front door.

# Correctness Reviewer
SINGLE FOCUS: correctness. Not naming, style or nice-to-haves. Checklist: logic errors; type mismatches; error handling; unguarded None; contracts with callers; state mutation (note: module-level tables monkeypatched per test).

Scope is THE DIFF: `git -C <workspace>/nthlayer-synthetic-fixtures diff dd96acc..041d2bd`. Do not raise findings outside it.
Affected repo: nthlayer: base=dd96acc, head=041d2bd, path=<workspace>/nthlayer-synthetic-fixtures

Requirement (owner ruling, bead opensrm-6kqs.8): replace the 13 trimmed-real-archive fixtures with synthetic ones: same structure and numbers, invented bead ids and timestamps, no free text; reproduce the structural quirks the tests exist for (hook-blocked dispatches, stall-and-resend, pass-less fix events, combined pass, re-runs, totals-only v1, blocking-only findings_count_initial, dry run, unreviewed gate). README traces each fixture to a pattern. The real known-data tables (BLOCKED_DISPATCHES, PASSLESS_EVENTS) keep their real keys; tests swap in synthetic counterparts; the real keys are checked at runtime by the new `unmatched_table_entries`, which main() warns on.

Verify: (1) every synthetic fixture equals its predecessor except target_bead, started_at and ts values (compare against `git -C <path> show dd96acc:test/fixtures/r5-baseline/<old name>`; the old->new name map is in the README's pattern table and test comments); (2) the SYNTHETIC_* keys in test/test_r5_baseline.py are the counterparts of the real entries (same event, same pass, same position in the trail); (3) `unmatched_table_entries` is correct (set precedence, key shapes: BLOCKED keys are (bead, pass, ts), PASSLESS keys are (bead, event, ts)), and main() over the real archives at <workspace>/.claude prints NO warning (`python3 <path>/scripts/r5_baseline.py --archive-dir <workspace>/.claude 2>&1 >/dev/null`); (4) the new `table_hits` key in each JSON record does not change any count; (5) the autouse monkeypatch cannot leak between tests.

Tests: `cd <path> && uv run --no-project --with pytest pytest -q -p no:cacheprovider test/test_r5_baseline.py` (50 passed). Do not modify files.

Report: CRITICAL / IMPORTANT / MINOR with file:line, what's wrong, why, fix. Under 300 words. End with Verdict: PASS / PASS WITH FIXES / FAIL.