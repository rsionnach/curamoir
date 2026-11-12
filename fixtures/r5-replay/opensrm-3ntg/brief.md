PASS 1 (CORRECTNESS) sequential R5 review on bead opensrm-3ntg.
Read your lens: ~/.claude/skills/rule-of-five-review/correctness-reviewer.md — follow it exactly.
Read the scope: <claude-tmp>/scratchpad/3ntg-gate-scope.md
For docs, "correct" means every command, flag, required/optional status, default, exit code and behavioural claim in the changed pages matches the parser and handler that implements it — sample-check the workers observe pages and check-deploy/gate claims against source with file:line. For the test, check the extraction logic (continuations, `$ ` prompts, placeholders, comment stripping) can't silently drop invocations.
Focus: correctness only. Read-only; no subagents. Report CRITICAL / IMPORTANT / MINOR with file:line and fix; verdict. Under 400 words.