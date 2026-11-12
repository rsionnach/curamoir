PASS 1 (CORRECTNESS) sequential R5 review on bead opensrm-e9ts (checkpoint 1: the nthlayer-common slice).

Template: read ~/.claude/skills/rule-of-five-review/correctness-reviewer.md and follow it. SINGLE FOCUS: correctness only.

Your scope is THE DIFF:
  git -C <workspace>/nthlayer-common-e9ts-legacy-slo-type diff 25a347d..36e5709
Do not raise findings outside it. Read the callers of `_infer_legacy_slo_type` (src/nthlayer_common/manifest/parser/loader.py) and of `_legacy_threshold_ms`.

Affected repo: nthlayer-common, base=25a347d, head=36e5709, path=<workspace>/nthlayer-common-e9ts-legacy-slo-type

Bead opensrm-e9ts (P2): the legacy loader inferred slo_type only from a singular spec.indicator.type, then from name keywords, and otherwise defaulted to "availability". Real legacy manifests (nthlayer-generate examples/services/*.yaml, readable at ../nthlayer-generate) declare a plural `indicators:` list. So `stream-lag` and `queue-lag` (indicators: [{type: custom}]) and `freshness-p95` (threshold_ms only) loaded as availability, and generate wrote them an error budget.

Rulings (Rob, 2026-11-02):
1. The types in an indicators: list must agree, or the load fails with ManifestLoadError naming the SLO. Untyped entries are ignored.
2. A typeless SLO with a latency threshold (legacy.LATENCY_THRESHOLD_KEYS) is latency.

The intended inference order is: singular type, then plural type, then name keywords, then latency threshold, then availability.

Verify:
- the order and the ruling semantics are implemented correctly;
- `_legacy_threshold_ms` can now raise during inference, so check that the error path and messages still make sense, and that it doesn't change which manifests load. It was already called when the SLODefinition is constructed;
- non-list or non-dict `indicators` input doesn't crash;
- unhashable type values (e.g. a list) in an entry don't crash the set comprehension;
- judgment SLO detection, which is name-keyed and separate, is unaffected.

To see how the change moves the real corpus, load every legacy manifest under ../nthlayer-generate/examples and ../nthlayer-generate/tests/golden/fixtures with this branch's loader, and compare each SLO's slo_type against main (git -C ../nthlayer-common). Report any SLO whose type changed beyond the three expected ones.

Report a bullet list of CRITICAL / IMPORTANT / MINOR. Keep it under 350 words. "No correctness issues found" is a valid result.