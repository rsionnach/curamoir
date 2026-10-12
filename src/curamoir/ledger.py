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
import re
from datetime import datetime, timezone
from pathlib import Path

#: The seconds and fraction of an ISO time: `...T12:00:00.5`. "Any separator"
#: is the date/time one at position 11 (`T`, a space, ...), which the bare `.`
#: after `.{10}` matches; the `\.` before the digits is the fraction's own.
_FRACTION = re.compile(r"^(.{10}.\d{2}:\d{2}:\d{2})\.(\d+)(.*)$")


def parse_ledger_timestamp(timestamp):
    """A ledger `at` value as an aware UTC datetime, or None if unusable.

    Normalised to UTC so month attribution never depends on the writer's offset:
    `2026-11-01T01:00+13:00` is 2026-10-31T12:00Z and belongs to October. A
    ledger merged from CI would otherwise lose and double-count entries across
    the month boundary.

    A trailing `Z` and a fraction of other than 6 digits are normalised
    first (to `+00:00`, and padded or cut to microseconds), because the
    fallback interpreter's (3.9) fromisoformat rejects both and 3.11+
    accepts them: the same ledger must read the same under either. Other
    forms only 3.11+ reads (`20261012`, week dates) stay unread on 3.9.
    """
    if not isinstance(timestamp, str):
        return None
    if timestamp[-1:] in ("Z", "z"):
        timestamp = timestamp[:-1] + "+00:00"
    fraction = _FRACTION.match(timestamp)
    if fraction:
        head, digits, tail = fraction.groups()
        timestamp = f"{head}.{digits[:6].ljust(6, '0')}{tail}"
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
        # None, not 0: "nobody has read this file yet" is not "this file had
        # no unreadable lines". Initialised here rather than left to
        # `entries()` because the call ordering is not enforceable, which the
        # defensive `getattr` this replaces was the tell for.
        self._unreadable = None

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

        Meaningful only after `entries()`. Returns `None` if that has not been
        called, so a caller that asks first gets "unknown" rather than a
        confident zero.

        Counts lines, and nothing else. Spend that parsed but could not be
        attributed to a month is `Spend.unaccounted`, reported by
        `month_to_date` to whoever asked — not recorded here, because a count
        with two producers on one field has no reset that is right for both.
        """
        return self._unreadable
