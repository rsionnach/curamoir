PASS 1 (CORRECTNESS) sequential R5 review on bead opensrm-wo62.4.1.

First, read your focus instructions from ~/.claude/skills/rule-of-five-review/correctness-reviewer.md and follow them; they are your template body.

Scope: THE DIFF only:
  git -C <workspace>/nthlayer-common-wo62-4-1 diff 12a9fcc..a2c8b22
Pre-change: `git -C <path> show 12a9fcc:<file>`; post-change: read files directly in the path.

Affected repos:
nthlayer-common: base=12a9fcc, head=a2c8b22, path=<workspace>/nthlayer-common-wo62-4-1

Bead: `cd <workspace>/opensrm && bd show opensrm-wo62.4.1` (DESIGN section is authoritative). Summary: for the LEGACY manifest format only, nthlayer-common's load_manifest now applies env overlays, expands `template:` (built-in templates as package data + .nthlayer/templates), substitutes ${var}, and maps three new PagerDutyConfig keys — porting the semantics of nthlayer-generate's legacy stack: <workspace>/nthlayer-generate/src/nthlayer_generate/specs/{parser.py,environments.py,environment_merger.py,variable_substitution.py,template.py,templates.py,template_loader.py,custom_templates.py}. The reference for "correct" is that legacy behaviour.

Rulings (decided; don't relitigate): exact legacy parity, including overlay-before-template, whole-resource replacement of template resources (no spec deep-merge), spec-only overlay merge; custom template dir via common's _find_template_dir walk-up from the manifest path (deliberate change from generate's cwd walk-up); implementer decisions listed in <workspace>/handoffs/2026-10-27-wo62-4-1-checkpoint.md. Do check the code actually achieves parity with the legacy semantics, that srm/v1 and v2 paths are unchanged, and package data actually ships.

Focus: correctness only. Do not modify files. Short foreground commands only (targeted `uv run pytest -q <file>` from the path and small `uv run python -c` probes are fine; you may compare against generate's legacy parse_service_file by running it from <workspace>/nthlayer-generate with `uv run --no-sync python -c ...` — read-only).

Report: CRITICAL / IMPORTANT / MINOR bullets with file:line and a concrete failure scenario. Under 350 words. "No correctness issues found" is valid.