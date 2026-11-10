PASS 1 (CORRECTNESS) sequential R5 review on bead opensrm-6kqs.1 (R5 Gate v2 Phase A.1, baseline script), nthlayer front door.

# Correctness Reviewer
SINGLE FOCUS: correctness. Not naming, style, docs wording, or nice-to-haves. Only flag what is wrong, broken, or produces incorrect results. Checklist: logic errors; type mismatches; error handling and uncaught propagation; unguarded None; off-by-one; return paths; contracts with callers; state mutation.

Scope is THE DIFF: `git -C <workspace>/nthlayer-r5-baseline diff da79c55..0e343ae` (scripts/r5_baseline.py, test/test_r5_baseline.py, test/fixtures/r5-baseline/, docs/r5-gate.md, .github/workflows/ci.yml, CLAUDE.md). Do not raise findings outside it.

Affected repo: nthlayer: base=da79c55, head=0e343ae, path=<workspace>/nthlayer-r5-baseline

Requirements (spec nthlayer/docs/superpowers/specs/2026-11-03-r5-gate-v2-nthlayer-spec.md section 3 item 1, in that path): read every `.claude/r5-state-*-completed.json` and print per bead and per lens: iterations run, findings by severity per iteration, model steps on the critical path (reviews + fixes + re-reviews, in dispatch order), and lens order used. Explain the 152-vs-274 discrepancy in the gate description's 2026-08-28 table. Closed when the table in docs/r5-gate.md is replaced by the script's output with the explanation beside it. Owner decision: cover ALL archived records (not the spec's 20). The JSON's per-record `sequential_steps` will feed the replay corpus's meta.json (next bead), so it must be right per record.

The real archives (untracked) are at <workspace>/.claude/r5-state-*-completed.json (88 files, heterogeneous: schema v1 and v2, many ad-hoc event fields). Run the script over them: `python3 <path>/scripts/r5_baseline.py --archive-dir <workspace>/.claude [--json]`. Check its numbers against the records directly for a sample of beads (including ones with design questions, supervisor_blocked, reviewer_dispatch_failed, or passes absent from completed_passes), and check that the 152/274 explanation in docs/r5-gate.md is actually what the records show (the 2026-08-28 table was over the 20 records then present). Verify the model-steps derivation is a correct reading of the critical path for the sequential gate, and say where it is wrong.

Tests: `cd <path> && uv run --no-project --with pytest pytest -q -p no:cacheprovider test/test_r5_baseline.py` (13 passed). Do not modify files.

Report: CRITICAL / IMPORTANT / MINOR bullets with file:line, what's wrong, why, fix. Under 350 words. "No correctness issues found" is valid. End with Verdict: PASS / PASS WITH FIXES / FAIL.