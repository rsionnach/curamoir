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
import re
import subprocess
import time
from contextlib import suppress
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
#: The developer's own note gets more room than a journal-derived decision —
#: they chose to write it — but still a bound, because WORD_LIMIT applies to
#: the whole file and the structural fields must survive it.
MAX_NOTE_SENTENCES = 6
MAX_NOTE_WORDS = 120
#: Room reserved for `_cap_words`' own "truncated" line.
_TRUNCATION_NOTICE_WORDS = 15
DEFAULT_KEEP_HISTORY = 10
MAX_LISTED_CHANGES = 12
MAX_LISTED_BEADS = 6
UNAVAILABLE = "unavailable"
#: `_next_step`'s fallback when there is nothing to resume. Named, because
#: `is_informative` has to be able to tell it from a real next step, and
#: comparing against a prose literal spelled out in two places would drift.
NO_THREAD_TO_PICK_UP = (
    "Nothing was claimed, so there is no obvious thread to pick up. "
    "`/curamoir:brief` will suggest a first task."
)


def build_handoff(repo, deadline_seconds=None, note=None):
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
    deadline = _Deadline(deadline_seconds)
    # Read ONCE and shared. `_last_decision` and `_next_step` each used to parse
    # the whole journal, which only ever grows: at 161 MB that was two passes
    # inside a 1.5s budget. The deadline reaches the file readers too, because
    # bounding only the git calls bounds only half the work.
    journal = _journal(repo, deadline=deadline)
    in_progress = _in_progress_beads(repo, deadline=deadline)
    return {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "branch": _branch(repo, deadline=deadline),
        "uncommitted": _uncommitted(repo, deadline=deadline),
        "in_progress": in_progress,
        "last_decision": _last_decision(journal, excluding=note),
        "next_step": _next_step(journal, in_progress),
        "note": None,
    }


def _last_decision(journal, excluding=None):
    """The most recent journal note, bounded.

    Free: the Stop hook hands us `last_assistant_message` each turn, so this
    needs no model call and no transcript parsing.

    `excluding` is the note THIS write is rendering. `write_handoff` journals
    the developer's note first, so that it survives the next automatic write —
    and then this read handed the same text straight back, so the note appeared
    twice in a file bounded at 400 words whose point is being skimmable. The
    semantics were the worse half: "Last decision" means what the PREVIOUS turn
    concluded, and echoing something typed thirty seconds ago crowds out the
    thing the section exists for. Found by the first real use of
    `/curamoir:handoff`, on cúramóir itself.
    """
    notes = [entry["note"] for entry in journal if entry.get("note")]
    if excluding is not None:
        wanted = excluding.strip()
        notes = [note for note in notes if note.strip() != wanted]
    return _first_sentences(notes[-1], MAX_DECISION_SENTENCES) if notes else ""


def _first_sentences(text, limit, word_limit=MAX_DECISION_WORDS):
    """The first `limit` sentences, and at most `word_limit` words.

    Both bounds are needed. Splitting on terminal punctuation alone leaves
    unpunctuated prose as a single unbounded "sentence", which is the common
    shape of `last_assistant_message`.
    """
    if not text:
        return text
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
        # Bounded like `last_decision`, which was already capped at 60 words
        # while the developer's own note was capped at nothing. `_cap_words`
        # DROPS a line that does not fit, so one long note past about 385 words
        # took the branch, the changes and the explicit next step with it,
        # leaving a title and a truncation notice.
        lines += ["> " + _first_sentences(handoff["note"], MAX_NOTE_SENTENCES,
                                          word_limit=MAX_NOTE_WORDS), ""]

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
        # Bounded like the note. `_bounded` lets 2,000 characters into the
        # journal and `_next_step` returns them verbatim, so an unbounded next
        # step hit `_cap_words`' per-line skip: the short heading survived and
        # its body did not, leaving a handoff that claimed to state a next step
        # and stated nothing — while "Last decision" rendered in full below it.
        lines += ["", "## Intended next step", "",
                  _first_sentences(handoff["next_step"], MAX_NOTE_SENTENCES,
                                   word_limit=MAX_NOTE_WORDS)]
    if handoff["last_decision"]:
        lines += ["", "## Last decision", "", handoff["last_decision"]]

    rendered = _cap_words("\n".join(lines), WORD_LIMIT)
    if not is_informative(handoff):
        # Appended after capping: the marker is what `_file_is_informative`
        # reads next session, so truncation must not be able to drop it.
        rendered += "\n\n" + _CONTENT_FREE_MARKER
    return rendered


def write_handoff(repo, keep_history=None, note=None, next_step=None,
                  deadline_seconds=None):
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
        handoff = build_handoff(repo, deadline_seconds=deadline_seconds, note=note)
        handoff["note"] = note.strip() if note and note.strip() else None
        rendered = render_handoff(handoff)
    except Exception as error:
        # Marked, like any other content-free handoff. This branch builds its
        # string directly rather than through `render_handoff`, so it did not
        # get the marker — and a handoff that says only "could not be built" is
        # content-free by definition, so the eviction it guards against came
        # back on the one path that exists for things going wrong.
        rendered = (
            "# cúramóir handoff\n\n"
            f"- **when:** {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n"
            f"- **state:** unavailable — the handoff could not be built ({error})\n"
            f"\n{_CONTENT_FREE_MARKER}\n"
        )

    try:
        history.mkdir(parents=True, exist_ok=True)
        if latest.is_file() and _file_is_informative(latest):
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


def is_informative(handoff):
    """Whether this handoff says anything worth keeping a copy of.

    Judged on the DATA, because judging the rendered prose does not work: the
    first version of this tested the markdown for `## Intended next step`, and
    `_next_step` always returns something, so the gate returned True for the
    very handoff it was written to reject and the eviction it was meant to stop
    went on happening. (`"> "` was a false positive on its own too — a renamed
    file renders as `old.py -> new.py`.)

    A handoff is informative if the developer wrote a note, or git told us
    anything, or a bead is claimed, or the next step is a real one rather than
    the "nothing was claimed" fallback.
    """
    if handoff.get("note"):
        return True
    if handoff.get("in_progress"):
        return True
    if handoff.get("branch") not in (None, "", UNAVAILABLE):
        return True
    if handoff.get("uncommitted") not in (None, UNAVAILABLE):
        return True
    if handoff.get("last_decision"):
        return True
    return handoff.get("next_step") not in (None, "", NO_THREAD_TO_PICK_UP)


#: The marker line that records `is_informative`'s verdict IN the file, so that
#: archiving a handoff written by an earlier session does not require parsing
#: its prose back into data. Rendered as an HTML comment: invisible when the
#: markdown is read, and unambiguous when it is matched.
_CONTENT_FREE_MARKER = "<!-- curamoir: content-free -->"


def _file_is_informative(latest):
    """Whether the handoff ALREADY on disk is worth archiving."""
    try:
        return _CONTENT_FREE_MARKER not in latest.read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return False


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
    # Microseconds, not seconds. At second resolution two handoffs from one
    # second collided, and the numbered suffix that fixed that introduced a
    # worse problem: `_prune` deletes files and so FREES low indices, and this
    # loop scans from zero for the first free slot — so a newly archived
    # handoff took the name of a deleted older one and `_prune`, which sorts by
    # name, then discarded the newest. Microseconds make the stamp itself
    # unique and monotonic, and the index below is only a tiebreak.
    stamp = written.strftime("%Y-%m-%dT%H-%M-%S-%f")
    body = latest.read_text(encoding="utf-8")
    for index in range(1000):
        candidate = history / _archive_name(stamp, index)
        try:
            with open(candidate, "x", encoding="utf-8") as handle:
                handle.write(body)
            return candidate
        except FileExistsError:
            continue
    return None


def _archive_name(stamp, index):
    """`<stamp>-<index>.md`, with the index ALWAYS present and zero-padded.

    `_prune` sorts by filename, so the names have to sort the way time does —
    and three separate things had to be true for that to hold. Unpadded
    indices put `-10` before `-2`. Omitting the index for the first entry of a
    second put it AFTER its own successors, because `'-' < '.'`. And a
    second-resolution stamp let `_prune`'s deletions free an index that the
    next write would reuse, giving a new handoff an old name. The stamp carries
    microseconds for that last one, which leaves the index as a tiebreak that
    should never be needed.
    """
    return f"{stamp}-{index:03d}.md"


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


def _branch(repo, deadline=None):
    """The current branch.

    `rev-parse --abbrev-ref HEAD` fails on an unborn ref — a repo that has a
    branch but no commits yet, which is the normal state right after
    `git init` and `/curamoir:init`. `symbolic-ref --short HEAD` answers there,
    so try it before giving up: reporting "unavailable" for a branch that is
    plainly knowable is worse than saying nothing.
    """
    named = _git_stripped(repo, "rev-parse", "--abbrev-ref", "HEAD", deadline=deadline)
    if named == "HEAD":
        # Detached: mid-rebase or mid-bisect, which is exactly the interrupted
        # session a handoff exists for — and "HEAD" reads as a branch name.
        sha = _git_stripped(repo, "rev-parse", "--short", "HEAD", deadline=deadline)
        return f"detached at {sha}" if sha else "detached HEAD"
    return (
        named
        or _git_stripped(repo, "symbolic-ref", "--short", "HEAD", deadline=deadline)
        or UNAVAILABLE
    )


def _uncommitted(repo, deadline=None):
    """Change summaries, never content. `--porcelain` gives status plus path."""
    # _git_raw, NOT _git_stripped: porcelain's first column is a space for an
    # unstaged change, and stripping the output shifts every such line left by
    # one, corrupting both the label and the path.
    raw = _git_raw(repo, "status", "--porcelain", deadline=deadline)
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


def _in_progress_beads(repo, deadline=None):
    """In-progress beads, read from the JSONL export rather than by calling bd.

    Calling `bd` from a per-turn hook would be slow and could contend with the
    Dolt server the developer's own session is using.
    """
    path = repo / ".beads" / "issues.jsonl"
    if not path.is_file() or (deadline and deadline.remaining() <= 0):
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


def _next_step(journal, in_progress):
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
    for entry in reversed(journal):
        if entry.get("next_step"):
            return entry["next_step"]

    if in_progress:
        first = in_progress[0]
        more = f" (and {len(in_progress) - 1} other claimed bead(s))" if len(in_progress) > 1 else ""
        return f"Resume {first['id']}: {first['title']}{more}."

    return NO_THREAD_TO_PICK_UP


def _journal(repo, deadline=None):
    path = repo / ".curamoir" / "handoff" / "journal.jsonl"
    if not path.is_file() or (deadline and deadline.remaining() <= 0):
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
        # suppress(OSError): pruning is housekeeping. A file that cannot be
        # removed — a permission change, a racing process — must not take the
        # handoff down with it.
        with suppress(OSError):
            stale.unlink()


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
            # Skip this line and keep going, rather than stopping here. A
            # `break` meant one over-long line discarded every SHORTER line
            # after it — and the structural fields are all short, so a long
            # note cost the branch, the changes and the next step.
            continue
        kept.append(line)
        used += count
    kept = _without_empty_headings(kept)
    kept += ["", f"_Truncated at {limit} words. Full state: `/curamoir:status`._"]
    return "\n".join(kept)


def _without_empty_headings(lines):
    """`lines` with any heading whose body was skipped removed.

    Skipping an over-long line rather than stopping at it keeps the short
    structural fields, which is the point — but it can leave a `## Intended
    next step` with nothing under it. A heading that promises the most
    actionable field in the file and delivers nothing is worse than no heading.
    """
    kept = []
    for index, line in enumerate(lines):
        if line.startswith("##"):
            body = [later for later in lines[index + 1:] if later.strip()]
            if not body or body[0].startswith("##"):
                continue
        kept.append(line)
    while kept and not kept[-1].strip():
        kept.pop()
    return kept


class _Deadline:
    """How much time is left for the git calls a handoff makes.

    Per-call timeouts do not add up to a bound: `_branch` makes two calls when
    the first fails, `_uncommitted` makes another, and at 5s each that is up to
    15s against the 2s hooks.json gives SessionEnd — which itself SHARES a
    1.5s budget with every other SessionEnd hook. A slow-but-not-down git (a
    cold network mount, a hung credential helper) therefore lost the handoff on
    exactly the session most worth having one for. One deadline for the whole
    build bounds it; a call with no time left is simply not made, and its field
    reports unavailable like any other git failure.
    """

    def __init__(self, seconds):
        self._until = None if seconds is None else time.monotonic() + seconds

    def remaining(self):
        if self._until is None:
            return _GIT_TIMEOUT
        return min(_GIT_TIMEOUT, self._until - time.monotonic())


#: Per-call ceiling when no deadline is given, e.g. from the CLI where the
#: developer is waiting and would rather have the answer than a fast nothing.
_GIT_TIMEOUT = 5


def _run_git(repo, args, deadline):
    left = (deadline or _Deadline(None)).remaining()
    if left <= 0:
        return None
    try:
        return subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True, timeout=left
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _git_raw(repo, *args, deadline=None):
    """git stdout with leading whitespace preserved, or None on failure."""
    result = _run_git(repo, args, deadline)
    if result is None:
        return None
    return result.stdout.rstrip("\n") if result.returncode == 0 else None


def _git_stripped(repo, *args, deadline=None):
    result = _run_git(repo, args, deadline)
    if result is None:
        return None
    return result.stdout.strip() if result.returncode == 0 else None
