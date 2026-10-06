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
from curamoir.hooks._entry import run


def _handle(_payload, repo):
    write_handoff(repo)


def main():
    return run(_handle)


if __name__ == "__main__":
    sys.exit(main())
