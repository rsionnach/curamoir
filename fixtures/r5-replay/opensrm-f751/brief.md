PASS 1 (CORRECTNESS) sequential R5 review on bead opensrm-f751.

Template body (from ~/.claude/skills/rule-of-five-review/correctness-reviewer.md):
---
You are reviewing code changes with a SINGLE FOCUS: correctness. Do not comment on naming, style, documentation, or "nice to have" improvements. Only flag things that are wrong, broken, or will produce incorrect results.
Checklist: logic errors; type mismatches; error handling; unguarded null access; off-by-one; races; return paths; API contracts; state mutations; resource cleanup.
Read: the diff; the requirements below; for each changed function, its callers.
Output: "### Correctness Issues" Critical / Important / Minor (File: path:line, What's wrong, Why it matters, Suggested fix); "### Correctness Assessment" Verdict PASS / PASS WITH FIXES / FAIL.
---

Your review scope is THE DIFF, not the whole file at HEAD. Run:
  git -C <workspace>/nthlayer-generate-f751 diff 8d5bc2c9b814f0e426ba0ff5a00115bcd8e03558..f973c0ac1199223bc475ff1088d8fb3ff47f8de1
Do not raise findings outside that diff. You may run read-only commands, the CLI (`cd <path> && uv run nthlayer ...`, writing only under a mktemp dir) and tests (`PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:cacheprovider <selection>`), but do NOT edit files.

Affected repos:
nthlayer-generate: base=8d5bc2c9b814f0e426ba0ff5a00115bcd8e03558, head=f973c0ac1199223bc475ff1088d8fb3ff47f8de1, path=<workspace>/nthlayer-generate-f751

Bead (opensrm-f751): `nthlayer apply <svc> -v --output json` printed orchestration progress via print() (orchestration/engine.py:34, _log_success; 19 prints under orchestration/) to stdout ahead of the JSON report. Ruling L1 (Rob): in JSON mode ONLY, apply_command (src/nthlayer_generate/cli/apply.py) wraps orchestrator.apply() in contextlib.redirect_stdout(sys.stderr); text mode unchanged (verbose cli_legacy_apply_text goldens pin progress on stdout). Test: tests/smoke/test_json_stdout.py apply case parametrised over -v. Out of scope, do not re-raise: apply --dry-run (opensrm-wlt1); the --lint step after apply.

Correctness angles: does anything else in apply_command's JSON path (after orchestrator.apply, before print_apply_json — e.g. warnings, Rich console output whose file was bound at Console construction rather than resolved per write — check cli/ux.py's console) still reach stdout; is redirect_stdout safe if orchestrator.apply raises (context restored, exception propagates, exit code); does the orchestrator or any handler hold a reference to sys.stdout captured before the redirect (e.g. module-level `print = functools.partial(print, file=sys.stdout)` or a stored stream) so the redirect misses it; does anything inside apply spawn a subprocess that inherits fd 1 (redirect_stdout only swaps the Python object, not fd 1); does the redirected output still respect -v (no progress without -v).

Focus: correctness only. Report format: bullet list of CRITICAL / IMPORTANT / MINOR. Under 300 words. "No correctness issues found" is a valid result.