"""Gate mode (Epic 5): the R5 review gate, run as code.

``findings`` is the shared Finding contract (the schema, fingerprints, the
open set, prose_unfiled) and the deterministic merge of one review round.
It is standard library only and Python 3.9 compatible, because the bridge
(``.claude/hooks/lib/r5_findings.py``) imports it under whatever ``python3``
the session finds.

``record`` is the round record at ``.curamoir/state/r5/<bead>/record.json``
and the tree-keyed verdict check (R5-R48, R5-R49); ``digest`` computes the
verdict's inputs and ``input_digest`` (R5-R75). Both run git through
``curamoir.git_guard`` and need the package's own Python; the bridge does
not import them.
"""
