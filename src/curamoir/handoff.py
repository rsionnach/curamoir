"""E8-R1/R2/R5: the session handoff.

Purpose, from the spec: eliminate the first forty minutes of every return.

The deterministic path is the PRIMARY path, not a fallback. A handoff earns its
keep precisely on the sessions that ended badly — the crash, the context
exhaustion, the abandoned evening — so it is built from git state and bead state,
both local and both always available. A model call only ever condenses what is
already written down.

Hook wiring (see hooks/hooks.json, and the correction recorded on E8-R1):

- **Stop** fires at the end of EVERY TURN, not at session end. It appends one
  cheap journal line, with no model call. `last_assistant_message` gives us the
  turn's final assistant text for free, and the docs say to prefer it over
  reading the transcript, which lags the in-memory conversation.
- **PreCompact** fires before context is discarded, which is the moment that
  matters most: a session that compacts has already lost what a handoff
  preserves. It writes the full handoff.
- **SessionEnd** finalises on exit. It shares a 1.5-second budget with every
  other SessionEnd hook, so it does deterministic work only.
"""

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from curamoir.redaction import write_artefact

WORD_LIMIT = 400
# Below this many journal notes, condensing is pure waste: a model call to
# restate one sentence costs money and buys nothing.
CONDENSE_THRESHOLD = 4
CONDENSE_TIMEOUT_SECONDS = 20
DEFAULT_KEEP_HISTORY = 10
MAX_LISTED_CHANGES = 12
MAX_LISTED_BEADS = 6
UNAVAILABLE = "unavailable"


def build_handoff(repo, condense=None, condense_timeout=CONDENSE_TIMEOUT_SECONDS):
    """Everything the handoff reports, as plain data.

    `condense` is an optional callable that turns many journal notes into one to
    three sentences. It is strictly an enhancement: it is only attempted when
    there is enough journal to be worth condensing, it is bounded by
    `condense_timeout`, and any failure or overrun falls back to the deterministic
    last note. That ordering is E8-R2 — a handoff is most valuable on the
    sessions that went wrong, which are exactly the sessions where a model call
    is least likely to succeed.
    """
    repo = Path(repo)
    return {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "branch": _branch(repo),
        "uncommitted": _uncommitted(repo),
        "in_progress": _in_progress_beads(repo),
        "last_decision": _decide_last_decision(repo, condense, condense_timeout),
        "next_step": _next_step(repo),
        "note": None,
    }


def _decide_last_decision(repo, condense, timeout):
    """The deterministic note, optionally condensed. Never raises, never hangs."""
    notes = [entry["note"] for entry in _journal(repo) if entry.get("note")]
    deterministic = notes[-1] if notes else ""

    if condense is None or len(notes) < CONDENSE_THRESHOLD:
        return deterministic

    try:
        condensed = _with_timeout(condense, notes, timeout)
    except Exception:
        return deterministic
    return condensed.strip() if isinstance(condensed, str) and condensed.strip() else deterministic


def _with_timeout(function, argument, seconds):
    """Run `function(argument)`, abandoning it after `seconds`.

    A thread rather than a signal: hooks may run on any thread, and SIGALRM is
    not available on Windows. The worker is left to finish into nothing — we
    stop WAITING for it, which is what non-blocking means here.
    """
    import threading

    outcome = {}

    def worker():
        try:
            outcome["value"] = function(argument)
        except BaseException as error:  # noqa: BLE001 - recorded, then discarded
            outcome["error"] = error

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        raise TimeoutError(f"condenser exceeded {seconds}s")
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("value")


def render_handoff(handoff):
    """Markdown, bounded to WORD_LIMIT words.

    The cap is an acceptance criterion, and it is also the point: a handoff you
    skim is a handoff that worked.
    """
    lines = ["# cúramóir handoff", ""]

    # A deliberate note leads. The developer states the next step rather than the
    # model inferring it, which makes it the most valuable line in the file.
    if handoff.get("note"):
        lines += ["> " + handoff["note"], ""]

    lines += [
        f"- **when:** {handoff['at']}",
        f"- **branch:** {handoff['branch']}",
    ]

    changes = handoff["uncommitted"]
    if changes == UNAVAILABLE:
        lines.append("- **uncommitted:** unavailable (not a git repository, or git failed)")
    elif not changes:
        lines.append("- **uncommitted:** clean")
    else:
        shown = changes[:MAX_LISTED_CHANGES]
        more = len(changes) - len(shown)
        lines.append(f"- **uncommitted:** {len(changes)} change(s)")
        lines.extend(f"  - {change}" for change in shown)
        if more:
            lines.append(f"  - ...and {more} more")

    if handoff["in_progress"]:
        shown = handoff["in_progress"][:MAX_LISTED_BEADS]
        more = len(handoff["in_progress"]) - len(shown)
        lines.append("- **in progress:**")
        lines.extend(f"  - {bead['id']}: {bead['title']}" for bead in shown)
        if more:
            lines.append(f"  - ...and {more} more")
    else:
        lines.append("- **in progress:** nothing claimed")

    if handoff["last_decision"]:
        lines += ["", "## Last decision", "", handoff["last_decision"]]
    if handoff["next_step"]:
        lines += ["", "## Intended next step", "", handoff["next_step"]]

    rendered = "\n".join(lines)
    return _cap_words(rendered, WORD_LIMIT)


def write_handoff(repo, keep_history=DEFAULT_KEEP_HISTORY, note=None, condense=None):
    """Write `handoff/latest.md`, rolling the previous one into history.

    `note` is the developer's own words, from `/curamoir:handoff` (E8-R4). It is
    journalled as well as rendered, so it survives the next automatic write
    rather than being lost the moment a hook fires.

    Never raises. A hook that throws adds noise to every turn and, because hooks
    fail open, protects nothing by dying.
    """
    repo = Path(repo)
    if note and note.strip():
        record_turn(repo, note.strip())
    directory = repo / ".curamoir" / "handoff"
    history = directory / "history"
    latest = directory / "latest.md"

    try:
        handoff = build_handoff(repo, condense=condense)
        handoff["note"] = note.strip() if note and note.strip() else None
        rendered = render_handoff(handoff)
    except Exception as error:
        rendered = (
            "# cúramóir handoff\n\n"
            f"- **when:** {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n"
            f"- **state:** unavailable — the handoff could not be built ({error})\n"
        )

    try:
        history.mkdir(parents=True, exist_ok=True)
        if latest.is_file():
            stamp = datetime.fromtimestamp(
                latest.stat().st_mtime, tz=timezone.utc
            ).strftime("%Y-%m-%dT%H-%M-%S")
            (history / f"{stamp}.md").write_text(
                latest.read_text(encoding="utf-8"), encoding="utf-8"
            )
        _prune(history, keep_history)
    except OSError:
        # Rolling is a nicety; losing it must not cost us the handoff itself.
        pass

    # Through the redacting write path (E0-R7), so E8-R5 is structural.
    return write_artefact(latest, rendered)


def record_turn(repo, note, at=None):
    """Append one journal line. Called by the Stop hook, so it must be cheap.

    One short append per turn, no model call, no git call.
    """
    repo = Path(repo)
    if not note or not note.strip():
        return None
    journal = repo / ".curamoir" / "handoff" / "journal.jsonl"
    entry = {
        "at": at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": note.strip(),
    }
    try:
        journal.parent.mkdir(parents=True, exist_ok=True)
        with journal.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
    except OSError:
        return None
    return journal


def _branch(repo):
    """The current branch.

    `rev-parse --abbrev-ref HEAD` fails on an unborn ref — a repo that has a
    branch but no commits yet, which is the normal state right after
    `git init` and `/curamoir:init`. `symbolic-ref --short HEAD` answers there,
    so try it before giving up: reporting "unavailable" for a branch that is
    plainly knowable is worse than saying nothing.
    """
    return (
        _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
        or _git(repo, "symbolic-ref", "--short", "HEAD")
        or UNAVAILABLE
    )


def _uncommitted(repo):
    """Change summaries, never content. `--porcelain` gives status plus path."""
    raw = _git(repo, "status", "--porcelain")
    if raw is None:
        return UNAVAILABLE
    changes = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        code, _, path = line.partition(" ")
        changes.append(f"{_describe(code)}: {path.strip() or line[3:].strip()}")
    return changes


def _describe(code):
    code = code.strip()
    return {
        "M": "modified", "A": "added", "D": "deleted", "R": "renamed",
        "C": "copied", "??": "untracked", "U": "conflicted",
    }.get(code, code or "changed")


def _in_progress_beads(repo):
    """In-progress beads, read from the JSONL export rather than by calling bd.

    Calling `bd` from a per-turn hook would be slow and could contend with the
    Dolt server the developer's own session is using.
    """
    path = repo / ".beads" / "issues.jsonl"
    if not path.is_file():
        return []
    beads = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            bead = json.loads(line)
            if bead.get("status") == "in_progress":
                beads.append({"id": bead.get("id", "?"), "title": bead.get("title", "")})
    except (ValueError, OSError):
        return []
    return beads


def _next_step(repo):
    """Whatever the developer recorded as next, if anything did."""
    for entry in reversed(_journal(repo)):
        if entry.get("next_step"):
            return entry["next_step"]
    return ""


def _journal(repo):
    path = repo / ".curamoir" / "handoff" / "journal.jsonl"
    if not path.is_file():
        return []
    entries = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                entries.append(json.loads(line))
    except (ValueError, OSError):
        return []
    return entries


def _prune(history, keep):
    files = sorted(history.glob("*.md"))
    for stale in files[: max(0, len(files) - keep)]:
        try:
            stale.unlink()
        except OSError:
            pass


def _cap_words(text, limit):
    words = text.split()
    if len(words) <= limit:
        return text
    # Truncate on a line boundary so the markdown stays well-formed, and say so:
    # a silently cut handoff is worse than a short one.
    kept, used = [], 0
    for line in text.splitlines():
        count = len(line.split())
        if used + count > limit - 15:
            break
        kept.append(line)
        used += count
    kept += ["", f"_Truncated at {limit} words. Full state: `/curamoir:status`._"]
    return "\n".join(kept)


def _git(repo, *args):
    try:
        result = subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None
