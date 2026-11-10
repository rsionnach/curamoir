PASS 1 (CORRECTNESS) sequential R5 review on bead opensrm-pnmv (Checkpoint C).

Read your lens first: ~/.claude/skills/rule-of-five-review/correctness-reviewer.md — follow it exactly.
Then read the scope (affected repos, diff ranges, requirements): <workspace>/nthlayer/.superpowers/sdd/2026-10-22-pnmv-opensrm-v1-migration/gate-C-scope.md

Focus: correctness only — do not comment on other dimensions. Read-only: do not modify any file, index, HEAD or branch. Do not dispatch subagents.

Report format: bullet list of CRITICAL / IMPORTANT / MINOR, each with file:line, what's wrong, why it matters, suggested fix; then the verdict. Under 350 words. "No correctness issues found" is a valid result.