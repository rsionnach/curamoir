PASS 1 (CORRECTNESS) sequential R5 review on bead opensrm-wo62.4.7.

Template body:
You are reviewing code changes with a SINGLE FOCUS: correctness. Do not comment on naming, style, documentation, or "nice to have" improvements. Only flag things that are wrong, broken, or will produce incorrect results.
Checklist: logic errors; type mismatches across function boundaries; error handling (failure paths covered, errors propagating uncaught); null/None access; off-by-one; return values; API contracts (signatures vs callers); state mutations; resource cleanup.
What to read: 1. the diff; 2. the requirements below; 3. for each changed function, read its callers to verify the contract holds.
Output: Critical / Important / Minor, each with file:line, what's wrong, why it matters, suggested fix. Then Verdict: PASS / PASS WITH FIXES / FAIL with 1-2 sentence reasoning.

Your review scope is THE DIFF, not the whole file at HEAD. Run:
  git -C <workspace>/nthlayer-common-wo62-4-7 diff 9be8fca..8315ce6
Do not raise findings on code outside that diff. Pre-change view: `git -C <path> show 9be8fca:<file>`; post-change: read files at the path. Read-only: do not modify any file.

Affected repos:
nthlayer-common: base=9be8fca, head=8315ce6, path=<workspace>/nthlayer-common-wo62-4-7

Bead description:
Legacy-loader parity gaps exposed when nthlayer-generate switched its CLI onto common's load_manifest:
1. Latency threshold: the legacy loader dropped spec.indicator.threshold_ms; generate then used slo.target (the 0-100 objective) as the bound. Fix: new SLODefinition.threshold_ms.
2. Legacy queues/caches ignored `type:`; generate guessed technology from the name. Fix: carry it.
3. Map `stream-processor` -> `stream`.
Rulings (Rob, 2026-10-28): R1 the legacy loader reads, in order, spec.indicator.threshold_ms -> spec.threshold_ms -> spec.latency_threshold (generate's legacy sloth read only the first; legacy recording rules only the third; `nthlayer setup` writes the second). R2 cache/queue `type:` goes into Dependency.database_type (an NthLayer extension field). R3 the alias goes in SERVICE_TYPE_ALIASES and so applies to every format.
Context: generate's legacy code lives at <workspace>/nthlayer-generate (src/nthlayer_generate/generators/sloth.py convert_indicator_to_sli; recording_rules/builder.py; dashboards/builder_sdk.py) if you need to confirm the legacy semantics. Other consumers of SLODefinition / Dependency / SERVICE_TYPE_ALIASES inside common (v1_compat, v2 emitters/serialisers, round-trip code) are in scope as callers.

Focus: correctness only.
Report format: bullet list of CRITICAL / IMPORTANT / MINOR. Under 350 words. "No correctness issues found" is a valid result.