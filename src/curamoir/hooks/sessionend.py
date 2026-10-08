"""The SessionEnd hook: finalise the handoff on exit.

SessionEnd hooks SHARE a 1.5-second budget across all of them, so this does
deterministic work only — no model call, and nothing that can stall. Overrunning
does not merely slow exit; it risks the handoff never being written, which is the
one thing this epic exists to produce.

The journal the Stop hook accumulated is already on disk, so finalising is a read
and a write.
"""

import sys

from curamoir.handoff import write_handoff

#: Inside the 1.5s SessionEnd hooks share, with room to write the file.
SESSION_END_BUDGET_SECONDS = 1.2
from curamoir.hooks._entry import run


def _handle(_payload, repo):
    # SessionEnd hooks SHARE a 1.5s budget, and hooks.json allows this one 2s.
    # `build_handoff`'s git calls are 5s each by default, so without a deadline
    # the hook was killed before writing anything — losing the handoff on
    # exactly the session most worth having one for. Measured at 12.1s with a
    # slow git; the deadline is what makes the declared budget real.
    write_handoff(repo, deadline_seconds=SESSION_END_BUDGET_SECONDS)


def main():
    return run(_handle)


if __name__ == "__main__":
    sys.exit(main())
