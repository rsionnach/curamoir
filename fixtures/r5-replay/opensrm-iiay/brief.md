PASS 1 (CORRECTNESS) sequential R5 review on bead opensrm-iiay.

Template body (from ~/.claude/skills/rule-of-five-review/correctness-reviewer.md):
---
You are reviewing code changes with a SINGLE FOCUS: correctness. Do not comment on naming, style, documentation, or "nice to have" improvements. Only flag things that are wrong, broken, or will produce incorrect results.
Checklist: logic errors; type mismatches; error handling; unguarded null access; off-by-one; races; return paths; API contracts; state mutations; resource cleanup.
Read: the diff; the requirements below; for each changed function, its callers.
Output: "### Correctness Issues" Critical / Important / Minor (File: path:line, What's wrong, Why it matters, Suggested fix); "### Correctness Assessment" Verdict PASS / PASS WITH FIXES / FAIL.
---

Your review scope is THE DIFF, not the whole file at HEAD. Run:
  git -C <workspace>/nthlayer-generate-iiay diff 49f61582f053d5739cbb40a6107288a38127b970..d76609d3c76cedaba11c071649c82d82aa96c1c5
Do not raise findings outside that diff. Pre-change view: `git -C <path> show 49f6158:<file>`. You may run read-only commands, the CLI (`cd <path> && uv run nthlayer ...`, writing only under a mktemp dir) and tests (`PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:cacheprovider <selection>`), but do NOT edit any files.

Affected repos:
nthlayer-generate: base=49f61582f053d5739cbb40a6107288a38127b970, head=d76609d3c76cedaba11c071649c82d82aa96c1c5, path=<workspace>/nthlayer-generate-iiay

Bead (opensrm-iiay, P1): `nthlayer plan --format json` and real `nthlayer apply --output json` wrote structlog debug lines to stdout ahead of the JSON when the manifest has a dependency (payment-api.yaml), because the CLI never configured logging. Fix: demo.main calls nthlayer_generate.logging.configure_logging(WARNING, or INFO when the subcommand's -v is set) unless structlog.is_configured() (conftest configures structlog, and 8 test files call main() in-process). configure_logging changed from a JSON renderer to stdlib-routed human console lines on stderr (PositionalArgumentsFormatter + ConsoleRenderer, colours only on a TTY, basicConfig stream=sys.stderr) — a flagged design choice to keep the pre-fix human output format. Test: tests/smoke/test_json_stdout.py (subprocess; payment-api + api-minimal; plan with/without -v; real apply). Filed separately, do not re-raise: opensrm-wlt1 (apply --dry-run ignores --output json).

Correctness angles: does every structlog logger in the codebase actually go through the new config (module-level `structlog.get_logger()` proxies bound before main runs; cache_logger_on_first_use=True; any logger obtained/used at import time before main configures); do loggers created with stdlib `logging.getLogger` (e.g. dashboards/manifest_builder uses %-style logger.warning) now reach stderr at the right level; does anything print structured logs through a non-stdlib path; does basicConfig no-op when handlers already exist; is `getattr(args, "verbose", False)` right for every subcommand (some may have a `verbose` attr meaning something else, or a different flag); does any stdout non-JSON print remain in plan --format json / apply --output json for other shipped manifests (try several under examples/services and examples/opensrm); does the change alter any other command's stdout content that users or scripts rely on (e.g. warnings that tests or goldens expected on stdout).

Focus: correctness only. Report format: bullet list of CRITICAL / IMPORTANT / MINOR. Under 350 words. "No correctness issues found" is a valid result.