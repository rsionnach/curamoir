PASS 1 (CORRECTNESS) sequential R5 review on bead opensrm-5vil. Read-only: do not modify any files.

Template: read and follow ~/.claude/skills/rule-of-five-review/correctness-reviewer.md.

Scope is THE DIFF: git -C <workspace>/nthlayer-common-5vil diff fa584ab..b9a0e20
Do not raise findings outside it. Pre-change: `git -C <path> show fa584ab:<file>`.

Affected repo: nthlayer-common base=fa584ab head=b9a0e20 path=<workspace>/nthlayer-common-5vil

Bead: opensrm-5vil — "common: model required_traces[].emitted_by (Addendum A1 §8)". nthlayer-common parser/v2.py built RequiredTrace(span_name, required_attributes) and dropped emitted_by; models.py had no field. The spec schema (<workspace>/opensrm/spec/v2/schema.json, definitions.Instrumentation.properties.required_traces.items.properties.emitted_by) is enum [application, provider], default application; Addendum A1 §8 (<workspace>/opensrm/OPENSRM-TELEMETRY-ADDENDUM-v2.md) defines it, and §10.1 requires an implementation to "Accept emitted_by on required_traces items and verify provider spans at the destination (§8)". Bead text: add the field (default application), parse it, cover both values plus the default from the spec examples. Verification at the destination is NOT in this bead (common is a model/parser library) — judge whether anything here claims otherwise.

Check: the parser's strictness choice (out-of-enum or non-string raises OpenSRMV2ParseError) vs the rest of v2.py's conventions and vs the schema (does the schema reject null / wrong case / non-string? does the parser agree?); every other place that constructs RequiredTrace or serialises/deserialises Instrumentation (e.g. any to_dict/from_dict, v1 or legacy loaders, migration v1→v2, manifest round-trips, snapshot/golden tests) — does emitted_by survive or get silently dropped/defaulted there?; consumers in sibling repos (<workspace>/nthlayer-{generate,core,workers,bench,override-adapter}/src) that construct RequiredTrace positionally or compare dataclasses. Run: cd <path> && PYTHONDONTWRITEBYTECODE=1 uv run pytest -p no:cacheprovider -o addopts="" -q

Focus: correctness only. Bullet list CRITICAL / IMPORTANT / MINOR with file:line, under 350 words, verdict. "No correctness issues found" is valid.