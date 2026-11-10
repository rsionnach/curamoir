PASS 1 (CORRECTNESS) sequential R5 review on bead opensrm-yrxm. Read-only: do not modify any files.

Template: read and follow ~/.claude/skills/rule-of-five-review/correctness-reviewer.md.

Scope is THE DIFF: git -C <workspace>/nthlayer-common-yrxm diff b2441b0..9ace047
Do not raise findings outside it. Pre-change: `git -C <path> show b2441b0:<file>`.

Affected repo: nthlayer-common base=b2441b0 head=9ace047 path=<workspace>/nthlayer-common-yrxm

Bead: opensrm-yrxm — "common: stop emitting gen_ai.system; emit gen_ai.provider.name (Addendum A1 P4)". Follow-up from opensrm-ln7w. nthlayer-common telemetry.py emits gen_ai.system, which Addendum A1 §5 P4 forbids (removed from the upstream registry); gen_ai.provider.name identifies the provider.

Scope note: the diff ALSO renames two usage attributes in the same event: gen_ai.usage.cached_tokens -> gen_ai.usage.cache_read.input_tokens and gen_ai.usage.reasoning_tokens -> gen_ai.usage.reasoning.output_tokens. Source for the target names: the OpenSRM spec at <workspace>/opensrm/OPENSRM-TELEMETRY-ADDENDUM-v2.md §6.7 and <workspace>/opensrm/profiles/claude-platform.md (Cost row). Verify those names against the spec text, and that the semantics match (cached_tokens is passed as "Cached input tokens (Anthropic prompt caching)" — check whether that is cache READ, per the docstring and callers, or could include cache writes).

Check callers: emit_llm_event is called from nthlayer-workers (src/nthlayer_workers/respond/agents/base.py ~L114-150, src/nthlayer_workers/correlate/summary.py ~L165) — verify the contract holds and nothing in the ecosystem (workers, core, bench, generate dashboards/recording rules, override-adapter, nthlayer/demo) reads gen_ai.system / gen_ai.usage.cached_tokens / gen_ai.usage.reasoning_tokens, which this rename would silently break. Run tests: cd <path> && PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:cacheprovider -o addopts="" -q tests/test_telemetry.py

Focus: correctness only. Bullet list CRITICAL / IMPORTANT / MINOR with file:line, under 300 words, verdict. "No correctness issues found" is valid.