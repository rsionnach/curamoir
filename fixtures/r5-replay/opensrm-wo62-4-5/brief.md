PASS 1 (CORRECTNESS) sequential R5 review on bead opensrm-wo62.4.5. Read-only: do not modify any file (you may run `PYTHONDONTWRITEBYTECODE=1 uv run python ...` / pytest from the repo path to probe behaviour).

First read the brief context file: <claude-tmp>/scratchpad/ctx5.md (diff scope, repo path, bead, Rob's settled rulings, out-of-scope beads).

Template body:
# Correctness Reviewer — SINGLE FOCUS: correctness. Do not comment on naming, style, docs or nice-to-haves. Only flag things that are wrong, broken or will produce incorrect results.
Checklist: logic errors; type mismatches across function boundaries; error handling / uncaught propagation; unguarded null access; off-by-one; races; return paths; API contracts (signatures vs callers); intended state mutations; resource cleanup.
What to read: 1) the diff; 2) the requirements to understand what the code SHOULD do; 3) for each changed function, its callers (cli/plan.py, cli/apply.py, demo.py and anything else constructing ServiceOrchestrator or OrchestratorContext — grep the whole src/ and tests/) to verify the contract holds.
Specific angles: does every handler's generate() produce what the matching CLI command produces for the same manifest (cli/generate.py, cli/generate_alerts.py, cli/dashboard.py, cli/recording_rules.py, cli/backstage.py)? Does anything still reference service_def / detector / ctx.detector at runtime? Is the env passed consistently (handlers no longer receive environment — is the manifest already overlaid in every path, including apply)? PagerDuty: plan vs generate guards when ownership.pagerduty is None; v1 tier/team values reaching get_schedules_for_tier and PagerDutyResourceManager. Exceptions from load_manifest in plan()/apply() — are all types caught as before?

Output: Critical / Important / Minor, each with File:line, what's wrong, why it matters, suggested fix. Then Verdict PASS / PASS WITH FIXES / FAIL with 1-2 sentences. Under 350 words. "No correctness issues found" is valid.