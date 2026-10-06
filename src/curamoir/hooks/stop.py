"""The Stop hook: one cheap journal line per turn.

Stop fires when Claude finishes responding, which is EVERY TURN — not at session
end. So this does the least possible work: append `last_assistant_message` to the
journal, with no model call and no git call. The docs direct hooks that need the
turn's final assistant text to use that field rather than reading the transcript,
which is written asynchronously and lags the in-memory conversation.

The full handoff is written by the PreCompact and SessionEnd hooks.
"""

import sys

from curamoir.handoff import record_turn
from curamoir.hooks._entry import run


def _handle(payload, repo):
    record_turn(repo, payload.get("last_assistant_message") or "")


def main():
    return run(_handle)


if __name__ == "__main__":
    sys.exit(main())
