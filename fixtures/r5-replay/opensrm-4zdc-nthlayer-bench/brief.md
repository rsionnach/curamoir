PASS 1 (CORRECTNESS) sequential R5 review on bead opensrm-4zdc. READ-ONLY: never modify files, never run git checkout/switch/stash/reset; probe files only in <claude-tmp>/scratchpad.

Template: read ~/.claude/skills/rule-of-five-review/correctness-reviewer.md and follow it.

Scope is THE DIFF:
- git -C <workspace>/nthlayer-bench diff 1106934..a2be80a
- git -C <workspace>/nthlayer-override-adapter diff 70ebf50..44a00c5

Bead: "nthlayer-bench + nthlayer-override-adapter: widen nthlayer-common to >=4.0.0,<5.0.0. No code touchpoints. Range widening + relock only, so eco-doctor stops reporting SIBLING>CEILING once common 4.0.0 is the sibling."

Context: nthlayer-common 4.0.0 (sibling at ../nthlayer-common; see its docs/upgrading-4.0.md and CHANGELOG) removed `nthlayer_common.manifest.JUDGMENT_SLO_TYPES`, moved segments/stability/calibration (+feedback_latency, audit_backlog) from SLODefinition to JudgmentThreshold / ReliabilityManifest.judgment_thresholds. nthlayer-core 2.0.0 now serves them under /manifests `judgment_thresholds` not `slos`. Verify the "no code touchpoints" claim independently: every nthlayer_common import in both repos' src/ and tests/, any use of CoreAPIClient.get_manifests / manifest dicts / slos, and anything else 4.0 changed (read the CHANGELOG for the full breaking list). Also check the floor really is the version under test and that uv.lock resolves common 4.0.0.

Run tests: cd <repo> && uv run pytest -q -o addopts="".

Focus: correctness only. Report CRITICAL / IMPORTANT / MINOR under 350 words; "No correctness issues found" is valid. End with `git -C <repo> status --short` for both repos, verbatim.