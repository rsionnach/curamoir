PASS 1 (CORRECTNESS) sequential R5 review on bead opensrm-zo0o, checkpoint 1 (nthlayer-common).

SINGLE FOCUS: correctness — only what is wrong, broken or produces incorrect results. Check every caller/consumer of changed code, including in sibling repos.

Scope is THE DIFF: git -C <workspace>/nthlayer-common-zo0o-good-total diff e598835..99a924d
Path: <workspace>/nthlayer-common-zo0o-good-total.

Bead: ruling Z1 (Rob, 2026-11-03) — every classical SLO is good/total against its objective, as OpenSLO defines it; nthlayer-generate will (CP2) write slo:error_budget_remaining:ratio for each from the SLO's own good/total, and workers measure (CP3) will evaluate every classical SLO from that budget. CP1 makes the parsers keep what they dropped: (1) OpenSLO v1 ratioMetric with `bad` + `total` → new SLODefinition.bad_query (OpenSLO: good and bad are mutually exclusive → both is now an OpenSLOParseError); (2) the legacy format's plural `indicators[]` success_ratio {total_query, good_query} → good_query/total_query from the first entry that has one. indicator_query is deliberately unchanged in both (generate's Sloth path reads it).

Check specifically:
- Does the new OpenSLOParseError for good+bad break any manifest that loads today (grep the opensrm spec examples, generate examples, common/workers/generate test fixtures for ratioMetric with both)? Is it raised inside load_manifest's error handling the same way as other OpenSLO errors?
- Consumers that now see new non-None good_query/total_query for legacy SLOs: nthlayer-workers observe/slo/collector.py:206 (composes (good)/(total) when there's no query), observe/cli.py:217, observe/verification/extractor.py:61, observe/worker.py:303 (rebuilds SLODefinition from a core /manifests dict — does core serialise SLODefinition, and does bad_query need to round-trip?), nthlayer-generate (sloth generator, recording rules), nthlayer-core (manifest serialisation / API schema), nthlayer-bench. Would any of their behaviour change incorrectly once they pick up this release?
- Any equality/serialisation of SLODefinition (asdict, to_dict, schema completeness tests, the v1→v2 converters v1_compat.py / opensrm_v1.py) that must now include bad_query.
- legacy success_ratio with non-string or missing values; indicators entries that are not dicts.

Report: CRITICAL / IMPORTANT / MINOR with file:line, under 350 words. "No correctness issues found" is valid. Do not modify files.