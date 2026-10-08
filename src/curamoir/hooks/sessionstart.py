"""E8-R3: inject the handoff — and the brief when it is newer — at session start.

This is G2 at its most literal: "reduce re-entry cost after absence to a single
short read". The developer should not have to ask.

The budget is the feature, not a safeguard. An injection that grows without bound
makes every session more expensive forever, and `additionalContext` has a hard
10,000-character ceiling beyond which Claude Code spills the value to a file and
substitutes a path plus a 2,000-character preview — so overrunning does not merely
cost tokens, it silently changes what Claude sees.

Injection is decided by `source`, because re-injecting state a session already
holds is pure cost:

  startup   a fresh session. Inject — this is the case the epic exists for.
  clear     context deliberately wiped, same repo. Inject.
  compact   just compacted, and our PreCompact hook wrote the handoff moments ago
            precisely because context was about to be lost. Inject.
  resume    context largely intact. Inject only after a long gap, because a
            resume hours later is a return whatever the field calls it.
  fork      branched from a live session; context is intact. Skip.

Context is written as factual statements, not instructions. The docs recommend it,
and a hook that issues orders is a hook that fights the user.
"""

import json
import sys
from pathlib import Path

from curamoir.hooks._entry import repo_from, read_payload
from curamoir.repo_config import config_value

# ~4 characters per token: the standard rough approximation. Tokenising properly
# would mean a dependency and a model call to save a few hundred characters.
CHARS_PER_TOKEN = 4
DEFAULT_TOKEN_BUDGET = 1500
# Hard ceiling imposed by Claude Code. Staying well under it is deliberate.
HARD_CHARACTER_LIMIT = 9000
LONG_GAP_SECONDS = 3600
#: The blank lines and "--- label ---" rules `_assemble` joins its parts with.
_JOIN_SLACK = 16

ALWAYS_INJECT = {"startup", "clear", "compact"}


def build_context(repo, token_budget=DEFAULT_TOKEN_BUDGET):
    """The context to inject, or None when there is nothing worth saying."""
    repo = Path(repo)
    handoff = repo / ".curamoir" / "handoff" / "latest.md"
    brief = repo / ".curamoir" / "briefs" / "latest.md"

    parts = []
    if handoff.is_file():
        parts.append(("handoff", ".curamoir/handoff/latest.md", _read(handoff)))
    # "and the away-brief if newer than the handoff" — a stale brief would
    # contradict the handoff and cost tokens to do it.
    if brief.is_file() and (
        not handoff.is_file() or brief.stat().st_mtime > handoff.stat().st_mtime
    ):
        parts.append(("brief", ".curamoir/briefs/latest.md", _read(brief)))

    parts = [part for part in parts if part[2].strip()]
    if not parts:
        return None

    budget = min(token_budget * CHARS_PER_TOKEN, HARD_CHARACTER_LIMIT)
    return _assemble(parts, budget)


def token_budget_for(repo):
    """The configured SessionStart injection budget (E8-R3's "configurable")."""
    budget = config_value(
        repo, "handoff", "session_start_token_budget", default=DEFAULT_TOKEN_BUDGET
    )
    return budget if isinstance(budget, (int, float)) and budget > 0 else DEFAULT_TOKEN_BUDGET


def should_inject(payload):
    source = payload.get("source") or "startup"
    if source in ALWAYS_INJECT:
        return True
    if source == "resume":
        gap = payload.get("seconds_since_last_response")
        return isinstance(gap, (int, float)) and gap >= LONG_GAP_SECONDS
    return False


def _assemble(parts, budget):
    header = "cúramóir state from the previous session:"
    paths = [path for _, path, _ in parts]
    footer = "Full text: " + ", ".join(paths) + "."
    # Reserve room for the footer and the truncation notice, so the pointer to the
    # full files survives truncation. A truncated injection that loses the pointer
    # is silent data loss.
    notice = "(Truncated to fit the session-start budget.)"
    room = budget - len(header) - len(footer) - len(notice) - _JOIN_SLACK

    body, truncated = [], False
    for label, _, text in parts:
        share = max(0, room // len(parts))
        block = f"--- {label} ---\n{text.strip()}"
        if len(block) > share:
            block = block[:share].rstrip()
            truncated = True
        body.append(block)

    sections = [header, ""] + body
    if truncated:
        sections += ["", notice]
    sections += ["", footer]
    assembled = "\n".join(sections)
    return assembled[:budget]


def _read(path):
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def main(stdin=None, stdout=None):
    stdout = stdout or sys.stdout
    payload = read_payload(stdin)
    if payload is None:
        return 0
    try:
        repo = repo_from(payload)
        if not (repo / ".curamoir").is_dir() or not should_inject(payload):
            return 0
        context = build_context(repo, token_budget=token_budget_for(repo))
        if not context:
            return 0
        json.dump(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": context,
                }
            },
            stdout,
        )
    except Exception:
        # Never let a context-loading convenience break session start.
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
