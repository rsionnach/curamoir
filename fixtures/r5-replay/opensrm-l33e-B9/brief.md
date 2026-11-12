PASS 1 (CORRECTNESS) sequential R5 review on bead opensrm-l33e — Task B9 commit. STRICTLY READ-ONLY (no checkout/stash/edits; scratch under <claude-tmp>/).

Template: ~/.claude/skills/rule-of-five-review/correctness-reviewer.md.

Scope: `git -C <workspace>/nthlayer-common diff 3e61463..d58a462`. Do not raise findings outside it.
Affected repos: nthlayer-common: base=3e61463, head=d58a462, path=<workspace>/nthlayer-common

Intent (maintainer decision): before 4.0.0 ships, `JudgmentThreshold.measurement` becomes a dedicated `ThresholdMeasurement` (bins, window, probe, segment_dimension, segment_values) instead of the union-shaped `JudgmentMeasurement`; `JudgmentMeasurement` unchanged for judgment SLOs; new public `duration_seconds(str) -> float` (Duration `<int><ms|s|m|h|d|w>`, ValueError otherwise), re-exported; both added to manifest/__init__.py; docs updated. No other behaviour change.

Correctness focus: every producer (v2 `_threshold_measurement`, `srm_v1_threshold`) builds ThresholdMeasurement with the same values as before; `threshold_to_v2_entry` reads the new type correctly (round trip still lossless for all five types); judgment-SLO paths still use JudgmentMeasurement; `duration_seconds` agrees with `is_duration` on accept/reject and computes the right seconds (incl. `ms` vs `m`, `0s`, large integers, bool); exports correct; docs (upgrading-4.0.md table/worked example, CLAUDE.md, architecture.md) match the code. You may run `uv run pytest -q` (~7s) and small probes. Do not modify the repo.

Report: CRITICAL / IMPORTANT / MINOR with file:line, under 250 words; verdict PASS / PASS WITH FIXES / FAIL.