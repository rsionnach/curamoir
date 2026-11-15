"""Gate mode (Epic 5): the R5 review gate, run as code.

``findings`` is the shared Finding contract (the schema, fingerprints, the
open set, prose_unfiled) and the deterministic merge of one review round.
It is standard library only and Python 3.9 compatible, because the bridge
(``.claude/hooks/lib/r5_findings.py``) imports it under whatever ``python3``
the session finds.
"""
