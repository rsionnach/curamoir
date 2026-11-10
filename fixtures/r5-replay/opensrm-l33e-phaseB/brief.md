PASS 1 (CORRECTNESS) sequential R5 review on bead opensrm-l33e — Phase B (nthlayer-common 4.0.0).

Template: read and follow ~/.claude/skills/rule-of-five-review/correctness-reviewer.md (single focus: things that are wrong, broken, or produce incorrect results).

Your review scope is THE DIFF: `git -C <workspace>/nthlayer-common diff 76db7f4..678ce6d`. Do not raise findings on code outside that diff. Pre-change view: `git -C <path> show 76db7f4:<file>`.
Affected repos: nthlayer-common: base=76db7f4, head=678ce6d, path=<workspace>/nthlayer-common

What the change should do (requirements):
- Design spec: <workspace>/nthlayer/docs/superpowers/specs/2026-10-09-l33e-judgment-thresholds-design.md §2.
- Binding amendments and later decisions: <workspace>/nthlayer/.superpowers/sdd/2026-10-09-l33e-judgment-thresholds/phaseB-amendments.md and the "Checkpoint A"/"Rob sign-off"/"Ruling" lines of <workspace>/nthlayer/.superpowers/sdd/2026-10-09-l33e-judgment-thresholds/progress.md.
- The authoritative spec it must agree with: sibling <workspace>/opensrm (branch feat/l33e-judgment-thresholds): spec/v2/schema.json `JudgmentThresholdSpec`, OPENSRM-CORE-v2.md §5.5–§5.5.6.
Summary: judgment error magnitudes (segments, stability, calibration, audit backlog, feedback latency) become `JudgmentThreshold` (manifest/thresholds.py validates; models hold V2_JUDGMENT_SLO_TYPES / V2_JUDGMENT_THRESHOLD_TYPES / SRM_V1_JUDGMENT_THRESHOLD_TYPES); v2 parser reads `judgment_threshold` (template concatenation, overrides append-only); srm/v1 lifts calibration→ECE and feedback_latency→p50; convert_v1_to_v2 emits judgment_threshold; pre-3c placements rejected with a fix-it entry; contract promises on threshold/unknown types error; magnitudes leave target_validation; JUDGMENT_SLO_TYPES removed (breaking, 4.0.0).
Downstream consumers (generate, core, workers, bench, adapter) are NOT in scope — they migrate in separate beads.

Correctness focus: schema/parser agreement on the same bytes; every parse error is the parser's own type (OpenSRMV2ParseError / OpenSRMParseError / ValueError for v1_compat), never TypeError/AttributeError; no remaining path converts or passes through a magnitude as an SLO target; round-trip srm/v1 → v2 → parse is lossless; template merge for keys other than judgment_threshold unchanged. Tests: `cd <workspace>/nthlayer-common && uv run pytest -q` (~1–2 min) — you may run it, or targeted files. Do not modify the repo.

Already-logged deferred minors (do not re-report unless you judge one now Important): see lines beginning "Task B" containing "minor (deferred)" in progress.md.

Report format: CRITICAL / IMPORTANT / MINOR bullets with file:line, under 350 words; verdict PASS / PASS WITH FIXES / FAIL. "No correctness issues found" is valid.