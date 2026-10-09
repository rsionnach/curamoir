"""Append-only local record of what cúramóir did and refused to do.

Append-only JSONL, committed with the runner's PRs, never leaves the repository
(E11-R3). Tokens and cost are recorded; money is derived at read time from an
editable price table, so correcting a stale price does not require re-running
jobs (E11-R2).

E0-R0 defines only what the guard layer needs: a durable note that a guardrail
fired. E0-R9 owns the one-line-per-job-exit guarantee; E11-R1 owns cost
attribution.
"""

import json
from datetime import datetime, timezone
from pathlib import Path


def parse_ledger_timestamp(timestamp):
    """A ledger `at` value as an aware UTC datetime, or None if unusable.

    Normalised to UTC so month attribution never depends on the writer's offset:
    `2026-11-01T01:00+13:00` is 2026-10-31T12:00Z and belongs to October. A
    ledger merged from CI would otherwise lose and double-count entries across
    the month boundary.
    """
    try:
        when = datetime.fromisoformat(timestamp)
    except (TypeError, ValueError):
        return None
    when = when.replace(tzinfo=timezone.utc) if when.tzinfo is None else when
    return when.astimezone(timezone.utc)


class Ledger:
    """A handle on one `.curamoir/state/ledger.jsonl`."""

    def __init__(self, path):
        self._path = Path(path)
        # Initialised here, not left to `entries()`. `unreadable_lines()`
        # already guarded against being read first with a defensive `getattr`,
        # which is the tell that the ordering is not enforceable — and the
        # newer `note_unaccounted` did not share the guard, so it raised
        # AttributeError on the same object.
        self._unreadable = 0
        self._unaccounted = 0

    def record(self, job, reason, message, kind="job", **fields):
        """Append one entry. Never rewrites or reorders existing lines.

        `kind` separates the two record types that coexist here:
        "job" is the one-per-exit line E0-R9 guarantees; "guard" is one per
        guardrail that fired, which E13-R3's dogfood log reads and of which a
        single run may produce several or none.
        """
        entry = {
            "at": datetime.now(timezone.utc).isoformat(),
            "kind": kind,
            "job": job,
            "reason": str(reason) if reason is not None else None,
            "message": message,
            **fields,
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            # A crash mid-write leaves a line with no terminator. Appending
            # straight onto it would concatenate, so one crash would cost two
            # records: the truncated one AND this one, which is whatever just
            # happened. Terminate it first and the crash costs exactly its own
            # line, which `unreadable_lines()` then reports accurately.
            if handle.tell() and not self._ends_with_newline():
                handle.write("\n")
            # `default=str` so a value that is not JSON — a Path, a datetime —
            # is logged as text rather than raising. `**fields` is the
            # documented extension point, so an unserialisable value there is
            # an ordinary slip; losing the whole audit line over it, or failing
            # a job that had already succeeded, is not a trade worth making.
            handle.write(json.dumps(entry, sort_keys=True, default=str) + "\n")
        return entry

    def _ends_with_newline(self):
        """True for an empty or missing file: there is no partial line to close."""
        try:
            with self._path.open("rb") as handle:
                handle.seek(0, 2)
                if handle.tell() == 0:
                    return True
                handle.seek(-1, 2)
                return handle.read(1) == b"\n"
        except OSError:
            return True

    def note_unaccounted(self, count=1):
        """Record spend a consumer read but could not attribute to a month.

        `unreadable_lines()` answers one question — how much of this file did
        not make it into the total — and an entry carrying token counts with an
        unparseable `at` fails that test just as surely as a truncated line,
        even though it parsed. Folding it into the same counter means the
        existing lower-bound wording covers it without a second vocabulary.
        """
        if self._unaccounted is not None:
            self._unaccounted += count

    def entries(self):
        """Every readable entry, oldest first. Missing file reads as empty.

        An unparseable line is SKIPPED, not fatal. This file is appended to by
        hooks and by the runner, so a crash mid-write will eventually truncate a
        line, and a strict read would turn that into "every job refused, and the
        one command meant to explain why crashes".

        Skipped lines are counted rather than discarded silently: undercounting
        spend is the single failure the budget guard exists to prevent, so the
        count is surfaced via `unreadable_lines()`.
        """
        # Parse failures are recomputed by each read; `_unaccounted` is NOT
        # reset here, because it records what a different pass found in the
        # same file and zeroing it would understate the total silently.
        self._unreadable = 0
        if not self._path.exists():
            return []
        entries = []
        try:
            # errors="replace": a crash mid-write of a multi-byte character
            # leaves invalid UTF-8, and UnicodeDecodeError is a ValueError raised
            # by the ITERATION, outside the per-line guard below.
            with self._path.open(encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        self._unreadable += 1
                        continue
                    # Parsing is not validating. `42`, `null` and `["a", 1]` are
                    # all valid JSON, and reached consumers that call
                    # `entry.get(...)` as an AttributeError — which is not
                    # UnknownModel, so it escaped the degraded path and refused
                    # every future job from a file that is never pruned.
                    if not isinstance(entry, dict):
                        self._unreadable += 1
                        continue
                    entries.append(entry)
        except (OSError, ValueError):
            # UNKNOWN, not zero. With the count left at 0 a caller reads
            # "could not open the ledger" as "no spend recorded".
            self._unreadable = None
            return []
        return entries

    def unreadable_lines(self):
        """Lines the last `entries()` call could not parse.

        `None` means the ledger itself could not be read, which is different
        from zero: a caller that cannot tell those apart will enforce a spend
        ceiling against a total it never obtained.

        The sum of two separately-stored counts: lines that would not parse,
        and entries that parsed but whose spend could not be attributed (see
        `note_unaccounted`). One public number, because a caller only ever
        needs to ask "did any of this file fail to reach the total"; two
        private ones, because only the first is recomputed by a read.
        """
        if self._unreadable is None:
            return None
        return self._unreadable + (self._unaccounted or 0)
