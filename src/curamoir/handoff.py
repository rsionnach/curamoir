"""E8-R1/R2/R5: the session handoff.

Purpose, from the spec: eliminate the first forty minutes of every return.

The deterministic path is the PRIMARY path, not a fallback. A handoff earns its
keep precisely on the sessions that ended badly — the crash, the context
exhaustion, the abandoned evening — so it is built from git state and bead state,
both local and both always available.

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

from curamoir.redaction import redact, write_artefact
from curamoir.repo_config import config_value

#: Bounds the WHOLE handoff file, not the last-decision field below it.
WORD_LIMIT = 400
# E8-R1 asks for "one to three sentences". Bounded by BOTH, because
# `last_assistant_message` is raw prose — bullet lists, code blocks, a trailing
# colon — and unpunctuated text is one "sentence" however long it runs. The word
# cap is what actually stops a long final turn consuming the file budget.
MAX_DECISION_SENTENCES = 3
MAX_DECISION_WORDS = 60
#: Room reserved for `_cap_words`' own "truncated" line.
_TRUNCATION_NOTICE_WORDS = 15
DEFAULT_KEEP_HISTORY = 10
MAX_LISTED_CHANGES = 12
MAX_LISTED_BEADS = 6
UNAVAILABLE = "unavailable"


def build_handoff(repo):
    """Everything the handoff reports, as plain data.

    No model call. DEC-8 (owner, 2026-10-07) settled that the last decision is
    taken deterministically from the turn's final assistant message: the docs
    direct hooks needing that text to prefer `last_assistant_message` over the
    transcript, which lags the in-memory conversation, so the deterministic
    source is better rather than a compromise — and a model call inside a hook
    would add spend on every compaction and a failure mode on the one path that
    must never block.
    """
    repo = Path(repo)
    in_progress = _in_progress_beads(repo)
    return {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "branch": _branch(repo),
        "uncommitted": _uncommitted(repo),
        "in_progress": in_progress,
        "last_decision": _last_decision(repo),
        "next_step": _next_step(repo, in_progress),
        "note": None,
    }


def _last_decision(repo):
    """The most recent journal note, bounded.

    Free: the Stop hook hands us `last_assistant_message` each turn, so this
    needs no model call and no transcript parsing.
    """
    notes = [entry["note"] for entry in _journal(repo) if entry.get("note")]
    return _first_sentences(notes[-1], MAX_DECISION_SENTENCES) if notes else ""


def _first_sentences(text, limit, word_limit=MAX_DECISION_WORDS):
    """The first `limit` sentences, and at most `word_limit` words.

    Both bounds are needed. Splitting on terminal punctuation alone leaves
    unpunctuated prose as a single unbounded "sentence", which is the common
    shape of `last_assistant_message`.
    """
    if not text:
        return text
    import re

    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    kept = " ".join(parts[:limit]).strip()
    truncated = len(parts) > limit

    words = kept.split()
    if len(words) > word_limit:
        kept = " ".join(words[:word_limit])
        truncated = True

    if truncated and not kept.endswith(("…", "...")):
        kept += " …"
    return kept


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

    # Intended next step first, deliberately. It is the most actionable field in
    # the file, so it must not be the one truncation discards.
    if handoff["next_step"]:
        lines += ["", "## Intended next step", "", handoff["next_step"]]
    if handoff["last_decision"]:
        lines += ["", "## Last decision", "", handoff["last_decision"]]

    rendered = "\n".join(lines)
    return _cap_words(rendered, WORD_LIMIT)


def write_handoff(repo, keep_history=None, note=None, next_step=None):
    """Write `handoff/latest.md`, rolling the previous one into history.

    `note` is the developer's own words, from `/curamoir:handoff` (E8-R4). It is
    journalled as well as rendered, so it survives the next automatic write
    rather than being lost the moment a hook fires.

    Never raises. A hook that throws adds noise to every turn and, because hooks
    fail open, protects nothing by dying.
    """
    repo = Path(repo)
    if keep_history is None:
        keep_history = config_value(
            repo, "handoff", "keep_history", default=DEFAULT_KEEP_HISTORY
        )
    if (note and note.strip()) or (next_step and next_step.strip()):
        record_turn(repo, note or "", next_step=next_step)
    directory = repo / ".curamoir" / "handoff"
    history = directory / "history"
    latest = directory / "latest.md"

    try:
        handoff = build_handoff(repo)
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
            _archive(latest, history)
        _prune(history, keep_history)
    except OSError:
        # Rolling is a nicety; losing it must not cost us the handoff itself.
        pass

    # Through the redacting write path (E0-R7), so E8-R5 is structural.
    try:
        return write_artefact(latest, rendered)
    except OSError:
        # The docstring's "never raises" has to cover the write too, not only
        # the build: a read-only handoff directory — which doctor has a whole
        # check for — reached /curamoir:handoff as a traceback, and reached the
        # hooks as a silent exit 0. None says "no handoff was written", which
        # the CLI reports in one sentence.
        return None


def _archive(latest, history):
    """Copy `latest` into `history` under a name no other copy can take.

    The name came from `latest`'s mtime at SECOND resolution, written with
    `write_text`. PreCompact followed by SessionEnd lands in the same second
    routinely, and the second copy then overwrote the first — so the oldest
    handoff was gone from `latest.md` AND from the archive that exists to keep
    it, in a store the spec calls append-only. Microseconds make a collision
    unlikely; `O_EXCL` makes losing one impossible.
    """
    written = datetime.fromtimestamp(latest.stat().st_mtime, tz=timezone.utc)
    stamp = written.strftime("%Y-%m-%dT%H-%M-%S")
    body = latest.read_text(encoding="utf-8")
    for suffix in ("", *(f"-{n}" for n in range(1, 1000))):
        candidate = history / f"{stamp}{suffix}.md"
        try:
            with open(candidate, "x", encoding="utf-8") as handle:
                handle.write(body)
            return candidate
        except FileExistsError:
            continue
    return None


def record_turn(repo, note, at=None, next_step=None):
    """Append one journal line. Called by the Stop hook, so it must be cheap.

    One short append per turn, no model call, no git call.

    Redacted and bounded here rather than at render time. This is the one
    artefact that does not go through `write_artefact`, because it is an append
    and not a rewrite — so E0-R7's redaction has to be applied explicitly, or a
    token the agent mentioned in passing is scrubbed from latest.md and written
    in full one directory away. The bound is for the same reason the renderer
    has one: the Stop hook passes the whole assistant message, every turn, and
    one real call wrote a 99,046-character line into a file nothing prunes.
    """
    repo = Path(repo)
    if not (note and note.strip()) and not (next_step and next_step.strip()):
        return None
    journal = repo / ".curamoir" / "handoff" / "journal.jsonl"
    entry = {
        "at": at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": _bounded(note),
    }
    if next_step and next_step.strip():
        entry["next_step"] = _bounded(next_step)
    try:
        journal.parent.mkdir(parents=True, exist_ok=True)
        with journal.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
    except OSError:
        return None
    return journal


#: Longest note stored per turn. The renderer never reads past
#: MAX_DECISION_WORDS, so anything beyond this is weight the file carries for
#: the life of the repo without a reader.
MAX_JOURNAL_NOTE_CHARS = 2_000


def _bounded(text):
    """`text` redacted, trimmed, and cut to a length a reader will use."""
    trimmed = redact((text or "").strip())
    if len(trimmed) <= MAX_JOURNAL_NOTE_CHARS:
        return trimmed
    return trimmed[:MAX_JOURNAL_NOTE_CHARS] + "… [truncated]"


def _branch(repo):
    """The current branch.

    `rev-parse --abbrev-ref HEAD` fails on an unborn ref — a repo that has a
    branch but no commits yet, which is the normal state right after
    `git init` and `/curamoir:init`. `symbolic-ref --short HEAD` answers there,
    so try it before giving up: reporting "unavailable" for a branch that is
    plainly knowable is worse than saying nothing.
    """
    return (
        _git_stripped(repo, "rev-parse", "--abbrev-ref", "HEAD")
        or _git_stripped(repo, "symbolic-ref", "--short", "HEAD")
        or UNAVAILABLE
    )


def _uncommitted(repo):
    """Change summaries, never content. `--porcelain` gives status plus path."""
    # _git_raw, NOT _git_stripped: porcelain's first column is a space for an
    # unstaged change, and stripping the output shifts every such line left by
    # one, corrupting both the label and the path.
    raw = _git_raw(repo, "status", "--porcelain")
    if raw is None:
        return UNAVAILABLE
    return [_describe_porcelain(line) for line in raw.splitlines() if line.strip()]


_PORCELAIN = {
    "M": "modified", "A": "added", "D": "deleted", "R": "renamed",
    "C": "copied", "T": "type changed", "U": "conflicted", "?": "untracked",
    "!": "ignored",
}


def _describe_porcelain(line):
    """One porcelain v1 line as "<description>: <path>".

    Parsed BY POSITION — two status characters, a space, then the path — never by
    splitting on whitespace: for an UNSTAGED change the index column is a space,
    and modified-but-unstaged is the overwhelmingly common state of the
    interrupted session a handoff exists for.
    """
    # rstrip only: the position slice has already removed the separator, and
    # stripping both ends discards a leading space that is part of the filename.
    code, path = line[:2], line[3:].rstrip()
    index, worktree = code[0], code[1] if len(code) > 1 else " "

    if index == "?" or worktree == "?":
        description = "untracked"
    elif index == "U" or worktree == "U":
        description = "conflicted"
    else:
        # Prefer the worktree column: it is what is on disk right now, and the
        # index column is a space precisely when the change is unstaged.
        character = worktree if worktree != " " else index
        description = _PORCELAIN.get(character, "changed")
    return f"{description}: {path or line.strip()}"


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


def _next_step(repo, in_progress):
    """The intended next step — one of E8-R1's six required contents.

    Resolution order, most authoritative first:
      1. whatever the developer stated via `/curamoir:handoff --next`, journalled
         so it survives the next automatic write;
      2. the bead they had claimed, which is the best available evidence of what
         they were doing;
      3. an explicit statement that there is nothing claimed.

    It always returns something: E8-R1 lists this field as required, and a
    required field no code path can produce is indistinguishable, to the reader,
    from an absent one.
    """
    for entry in reversed(_journal(repo)):
        if entry.get("next_step"):
            return entry["next_step"]

    if in_progress:
        first = in_progress[0]
        more = f" (and {len(in_progress) - 1} other claimed bead(s))" if len(in_progress) > 1 else ""
        return f"Resume {first['id']}: {first['title']}{more}."

    return (
        "Nothing was claimed, so there is no obvious thread to pick up. "
        "`/curamoir:brief` will suggest a first task."
    )


def _journal(repo):
    path = repo / ".curamoir" / "handoff" / "journal.jsonl"
    if not path.is_file():
        return []
    entries = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for line in lines:
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            # Skip the torn line, keep the rest. Returning [] on any
            # ValueError meant one crash mid-append cost every entry —
            # including the next step, which E8-R1 calls required.
            continue
        if isinstance(entry, dict):
            entries.append(entry)
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
        if used + count > limit - _TRUNCATION_NOTICE_WORDS:
            break
        kept.append(line)
        used += count
    kept += ["", f"_Truncated at {limit} words. Full state: `/curamoir:status`._"]
    return "\n".join(kept)


def _git_raw(repo, *args):
    """git stdout with leading whitespace preserved, or None on failure."""
    try:
        result = subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.rstrip("\n") if result.returncode == 0 else None


def _git_stripped(repo, *args):
    try:
        result = subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None
