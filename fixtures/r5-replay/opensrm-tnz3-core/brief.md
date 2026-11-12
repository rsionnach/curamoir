PASS 1 (CORRECTNESS) sequential R5 review on bead opensrm-tnz3 (nthlayer-core part). STRICTLY READ-ONLY on the repo; scratch only under <claude-tmp>/.

Template: ~/.claude/skills/rule-of-five-review/correctness-reviewer.md.

Scope: `git -C <workspace>/nthlayer-core diff 1e814a3..bc9d0a3`. Do not raise findings outside it.
Affected repos: nthlayer-core: base=1e814a3, head=bc9d0a3, path=<workspace>/nthlayer-core

Requirements: nthlayer-core moves to nthlayer-common >=4.0.0,<5.0.0 (4.0.0 moved judgment error magnitudes out of ReliabilityManifest.slos into `judgment_thresholds: list[JudgmentThreshold]` — type, bounds (dict, stored as declared), measurement: ThresholdMeasurement(bins, window, probe{frozen_input_set, frequency}, segment_dimension, segment_values); see <workspace>/nthlayer-common/docs/upgrading-4.0.md and nthlayer-common/src/nthlayer_common/manifest/models.py). Maintainer decision: catalogue.manifest_to_dict adds a new top-level `judgment_thresholds` key (always present; list of {type, bounds, measurement} with only non-default measurement fields), beside unchanged `slos`. Pin-guard test EXPECTED_MAJORS bumped 3→4.

Check: serialisation correct for all five types (calibration bins/window; stability probe; segments dimension/values; audit_backlog/feedback_latency empty measurement), bounds values as declared (floats vs duration strings), JSON-serialisable (the API returns JSON — confirm ProbeConfig/dataclasses are converted, not leaked), slos output unchanged; uv.lock diff only reflects the 4.0.0 relock (+ any stale self-version line) — check nothing else drifted; any other place in core's src that reads manifest.slos expecting calibration/segments/stability (grep) and would now silently lose them. You may run `cd nthlayer-core && uv run pytest -q` (~seconds) and a small probe. Do not modify the repo.

Report: CRITICAL / IMPORTANT / MINOR with file:line, under 250 words; verdict PASS / PASS WITH FIXES / FAIL.