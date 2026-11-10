PASS 1 (CORRECTNESS) sequential R5 review on bead opensrm-dww5 (Checkpoint B).
Read your lens: ~/.claude/skills/rule-of-five-review/correctness-reviewer.md — follow it exactly.
Read the scope (7 repos, ranges, decision): <claude-tmp>/scratchpad/dww5-gate-B-scope.md
Pay particular attention to: every place that still invokes core's CLI as `nthlayer` (anywhere in the 7 repos' diffs and their unchanged call sites that would break after the rename — e.g. Dockerfiles, compose files, CI workflows, scripts, systemd/litestream examples), eco_doctor.py's new duplicate-script logic, and the generate test's parsing of the real message.
Focus: correctness only. Read-only; no subagents. Report CRITICAL / IMPORTANT / MINOR with file:line, what's wrong, fix; then verdict. Under 400 words.