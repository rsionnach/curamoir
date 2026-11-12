PASS 1 (CORRECTNESS) sequential R5 review on bead opensrm-l33e (scoped follow-up commit).

Template: read and follow ~/.claude/skills/rule-of-five-review/correctness-reviewer.md (single focus: things that are wrong, broken, or produce incorrect results).

Scope is THE DIFF: `git -C <workspace>/opensrm diff 6c46a87..03e929b` (3 files, ~33 lines). Do not raise findings outside it.
Affected repos: opensrm: base=6c46a87, head=03e929b, path=<workspace>/opensrm

What the commit is meant to do (maintainer decisions, 2026-10-11): in OPENSRM-CORE-v2.md, normatively specify for `spec.judgment_threshold`: (a) several entries of the same type MAY appear and each is evaluated independently, a breach of any is a breach; (b) `maximum_variance_from_overall` = largest permitted absolute difference, in ratio points, between any listed segment's rate and the overall rate, either direction; (c) feedback_latency p50/p90 are computed over a rolling 30-day window, not configurable in this version; plus a §13 Conformance MUST for thresholds, and §5.5 now admits thresholds on a ServiceManifestTemplate an ai-gate manifest extends. AUTHORING.md points to these. New positive example spec/v2/examples/judgment-thresholds/segments-two-dimensions.yaml pins (a).
Correctness here: does the prose say exactly that, consistently with spec/v2/schema.json (`JudgmentThresholdSpec`) and the existing §5.5/§5A text, with no contradiction (e.g. a stated range that the schema's Ratio [0,1] can't express, a section reference that doesn't resolve, an example that doesn't validate)? You may run `bash spec/v2/validate.sh` (~30s). Do not modify the repo.

Report: CRITICAL / IMPORTANT / MINOR bullets, under 300 words, then verdict PASS / PASS WITH FIXES / FAIL. "No correctness issues found" is valid.