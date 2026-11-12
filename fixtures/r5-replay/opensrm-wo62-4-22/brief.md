PASS 1 (CORRECTNESS) sequential R5 review on bead opensrm-wo62.4.22.

Template body (from ~/.claude/skills/rule-of-five-review/correctness-reviewer.md):
---
You are reviewing code changes with a SINGLE FOCUS: correctness. Do not comment on naming, style, documentation, or "nice to have" improvements. Only flag things that are wrong, broken, or will produce incorrect results.
Checklist: logic errors; type mismatches across function boundaries; error handling / uncaught propagation; unguarded null access; off-by-one; races; return paths; API contracts (do signatures match callers?); state mutations; resource cleanup.
Read: the diff between BASE and HEAD; the requirements below; for each changed function, its callers.
Output: "### Correctness Issues" with Critical / Important / Minor (each: short description, File: path:line, What's wrong, Why it matters, Suggested fix), then "### Correctness Assessment" Verdict PASS / PASS WITH FIXES / FAIL with reasoning.
---

Your review scope is THE DIFF, not the whole file at HEAD. Run:
  git -C <workspace>/nthlayer-generate-wo62-4-22 diff 4b278a3fa401452101e8925bf1a6bb33ad844511..66841e9a7dc52b25a9f8fbb04f921eeb4ea41ef3
Do not raise findings on code outside that diff. Pre-change view: `git -C <path> show 4b278a3:<file>`; post-change: read the file at the path. Do NOT edit any files.

Affected repos:
nthlayer-generate: base=4b278a3fa401452101e8925bf1a6bb33ad844511, head=66841e9a7dc52b25a9f8fbb04f921eeb4ea41ef3, path=<workspace>/nthlayer-generate-wo62-4-22
(nthlayer-common sibling at <workspace>/nthlayer-common is read-only reference.)

Bead (opensrm-wo62.4.22): port the assertions that generate 4.0.0 (opensrm-wo62.4.6) deleted and that had no manifest-path equivalent — recording-rule naming conventions / service label / availability and latency rule sets (onto build_recording_rules_from_manifest), alert validation (generate_alerts_from_manifest raises template `for: 0m` to 1m), and "no duplicate technology dashboard panels". Fixtures must come from shipped examples or the spec, not the implementation. Rulings: J1 — the two quiet-mode ports are deferred to opensrm-iiay (plan/apply JSON stdout polluted by structlog); J2 — fix the duplicate dashboard panels in this bead (DashboardBuilderSDK._build_technology_panels dedupes by resolved template name, falling back to the raw type when there is no template); J3 — the "k8s panels auto-added for an API" port is dropped (behaviour removed with the SDK builder); J4 — Rob approved the 6 golden updates (staged env overlays payment-api dev/prod and search-api prod; each drops exactly one duplicate panel set). Also filed separately: opensrm-jlbm (srm/v1 example latency shape schema-invalid) — do not re-raise.

Correctness angles worth checking: does the dedupe key (get_template_or_none(dep_type).name else dep_type) ever merge two technologies that should both get panels, or fail to merge true aliases (check the template registry in src/nthlayer_generate/dashboards/templates/__init__.py, including the use_intent_templates=False / legacy-template path and case sensitivity); does the dedupe change behaviour for an untemplated type (previously an error path — E6 / opensrm-wo62.4.16); is the golden diff consistent with "drop exactly one duplicate set" (check one changed golden under tests/golden/data/cli_legacy_env_*/staged__*); do the new tests assert what their names and docstrings claim.

Focus: correctness only. Report format: bullet list of CRITICAL / IMPORTANT / MINOR. Under 350 words. "No correctness issues found" is a valid result.