PASS 1 (CORRECTNESS) sequential R5 review on bead opensrm-z1kq.

Template: read ~/.claude/skills/rule-of-five-review/correctness-reviewer.md and follow it. SINGLE FOCUS: correctness.

Scope is THE DIFF:
  git -C <workspace>/nthlayer diff 7f9d0a1..adfc87a
Files:
- meta-package/pyproject.toml (the dependency ranges)
- meta-package/README.md
- CLAUDE.md (the Pinning bullet)
- .github/dependabot.yml (versioning-strategy)
- .github/workflows/ci.yml (new step: `uv pip compile` of the meta-package)

Bead: the nthlayer meta-package pinned core, workers and bench ==1.6.0 and generate ==1.1.0, several majors behind PyPI. The ruling (Rob, 2026-11-02) is compatible ranges: floor at the current release, ceiling at the next major. The supervisor checked that the ranges resolve, to core 4.0.10, workers 4.0.2, bench 1.7.17, generate 4.0.10 and common 5.10.1, and that the built wheel's Requires-Dist carries them.

Verify the following:
- the floors are the current PyPI releases. Check with `curl -s https://pypi.org/pypi/<pkg>/json`.
- every tier's own `nthlayer-common` constraint is compatible.
- the console scripts across the four tiers don't collide, e.g. generate's `nthlayer` vs any other.
- dependabot's `uv` ecosystem supports `versioning-strategy: increase-if-necessary`, and behaves sensibly with no uv.lock in /meta-package.
- the CI step works from the repo root: does python-lint have uv installed, and is the path right?
- the README and CLAUDE.md claims are accurate. The README's CLI section and its "4.x" statements are part of this.
- release-please will cut a meta release, 1.0.3, from this `fix(meta):` commit. See release-please-config.json.

Report a bullet list of CRITICAL / IMPORTANT / MINOR. Keep it under 300 words. "No correctness issues found" is a valid result.