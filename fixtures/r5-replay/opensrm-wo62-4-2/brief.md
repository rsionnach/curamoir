PASS 1 (CORRECTNESS) sequential R5 review on bead opensrm-wo62.4.2.

First, read your focus instructions from ~/.claude/skills/rule-of-five-review/correctness-reviewer.md and follow them; they are your template body.

Scope: THE DIFF only — but the 1062 golden JSON files under tests/golden/data/ are generated output; review the test code and sample goldens only to check they are plausible:
  git -C <workspace>/nthlayer-generate-wo62-4-2 diff f55186c..407bcaa -- tests/golden/_golden.py tests/golden/test_golden_legacy_generators.py tests/golden/test_golden_legacy_cli.py
Affected repos:
nthlayer-generate: base=f55186c, head=407bcaa, path=<workspace>/nthlayer-generate-wo62-4-2

Bead: `cd <workspace>/opensrm && bd show opensrm-wo62.4.2`. Purpose: TESTS ONLY — pin CURRENT behaviour of generate's legacy entry points (6 legacy builders, the wired legacy CLI commands, plan/apply, env-overlay cases) so the later switch to nthlayer-common (wo62.4.3–4.5) shows every change as a golden diff. Background: <workspace>/handoffs/2026-10-27-wo62-4-checkpoint-plan.md.

Correctness questions for a pin suite: does each cell actually invoke the legacy entry point it claims (not the *_from_manifest twin)? Are stubs patched at the use-site lookup path so the real network/client code is never reached, and do they still let the real legacy logic run? Does any normalisation (sorting of set-ordered output, MASK additions `timestamp`/`duration_seconds`, console width/collapsing, cwd changes) hide a real behaviour difference that the switch could change — i.e. would a genuine output change still show up as a diff? Do the env-overlay cases really exercise overlays (the guard)? Does the MASK change alter how existing goldens compare? Are errors pinned in a way that would show if an error became a success or changed type?

Do not modify files. Short foreground commands only (targeted pytest on single cells is fine).

Report: CRITICAL / IMPORTANT / MINOR bullets with file:line and a concrete failure scenario. Under 350 words. "No correctness issues found" is valid.