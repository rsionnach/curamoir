"""The PreCompact hook: write the full handoff before context is discarded.

This is the event that matters most. A session that compacts has already lost
what the handoff preserves, and unlike SessionEnd there is a real timeout budget
here, because compaction is infrequent.

It never blocks. PreCompact *can* block compaction, via exit 2 or
`decision: "block"`, and blocking it in order to write a note would be an absurd
trade.
"""

import sys

from curamoir.handoff import record_turn, write_handoff
from curamoir.hooks._entry import run

#: Well inside PreCompact's 30s, and generous: losing this one costs the most.
PRE_COMPACT_BUDGET_SECONDS = 15


def _handle(payload, repo):
    trigger = payload.get("trigger") or "unknown"
    record_turn(repo, f"Context compacted ({trigger} trigger).")
    # hooks.json allows PreCompact 30s: this is the write that matters most, so
    # it gets room. Still bounded, because a hung git must not spend the lot.
    write_handoff(repo, deadline_seconds=PRE_COMPACT_BUDGET_SECONDS)


def main():
    return run(_handle)


if __name__ == "__main__":
    sys.exit(main())
