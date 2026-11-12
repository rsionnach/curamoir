PASS 1 (CORRECTNESS) sequential R5 review on bead opensrm-l33e.

Template body:
# Correctness Reviewer
You are reviewing code changes with a SINGLE FOCUS: correctness.
Do not comment on naming, style, documentation, or "nice to have" improvements. Only flag things that are wrong, broken, or will produce incorrect results.
Checklist: logic errors (does it do what it's supposed to?); type mismatches across boundaries; failure paths; unguarded access; off-by-one in ranges/bounds; return values; API contracts (do signatures/shapes match their consumers?); state mutations; resource cleanup.
What to read: 1) the diff between BASE and HEAD; 2) the plan/requirements to understand what it SHOULD do; 3) for each changed contract, its consumers.
Output: ### Correctness Issues — #### Critical / #### Important / #### Minor, each with File:line, What's wrong, Why it matters, Suggested fix. ### Correctness Assessment — Verdict PASS / PASS WITH FIXES / FAIL, Reasoning.

Your review scope is THE DIFF, not the whole file at HEAD. Run:
  git -C <workspace>/opensrm diff a75c9ae..f9d7a91
Do not raise findings on content outside that diff. Pre-change view: `git -C <path> show a75c9ae:<file>`; post-change: read the file at the path.

Affected repos:
opensrm: base=a75c9ae, head=f9d7a91, path=<workspace>/opensrm

This repo is a SPECIFICATION (JSON Schema + YAML examples + markdown). "Correctness" here means: the schema accepts exactly what the spec says is valid and rejects what it says is invalid; every positive example validates and every negative fixture is rejected FOR THE REASON its header states; YAML in the prose is valid against the schema; cross-references/anchors resolve; nothing contradicts the schema.
Tests: `bash spec/v2/validate.sh` (validates examples/ positive and tests/invalid/ negative with check-jsonschema). You may run it and run `uvx --quiet check-jsonschema --schemafile spec/v2/schema.json <file>` on scratch files under /private/tmp/claude-501/ (never inside the repo). Do not modify the repo.

Requirements (read these):
- Design spec: <workspace>/nthlayer/docs/superpowers/specs/2026-10-09-l33e-judgment-thresholds-design.md — §1 is what this diff implements.
- Plan Tasks A1/A2: <workspace>/nthlayer/docs/superpowers/plans/2026-10-09-l33e-judgment-thresholds.md (search "## Task A1", "## Task A2").
This is Phase A of a two-phase bead: the nthlayer-common parser changes are Phase B, a separate repo and a later diff.

Bead description:
opensrm-l33e — decision 3c: judgment error magnitudes (segments, stability, calibration; plus audit_sampling's audit_backlog_maximum_age) stop being SLOs and become a distinct manifest concept (now `spec.judgment_threshold`, which also gains feedback_latency). Breaking v2 spec change. Acceptance: the spec and the parser agree on the vocabulary in both directions; TargetConventionWarning no longer fires on error magnitudes (Phase B); gated and released shared-repo-first.

Focus: correctness only — do not comment on other dimensions.

Report format: bullet list of CRITICAL / IMPORTANT / MINOR. Under 350 words. "No correctness issues found" is a valid result.