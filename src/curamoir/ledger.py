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
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
        return entry

    def entries(self):
        """Every readable entry, oldest first. Missing file reads as empty.

        An unparseable line is SKIPPED, not fatal. This file is appended to by
        hooks and by the runner, so a crash mid-write will eventually truncate a
        line — and a strict read turned that into "every job refused, and the one
        command meant to explain why crashes".

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
            # by the ITERATION, outside the per-line guard — so a torn "cúramóir"
            # in a message field reproduced the original symptom exactly: every
            # job refused, /curamoir:status crashing.
            with self._path.open(encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    try:
                        entries.append(json.loads(line))
                    except ValueError:
                        self._unreadable += 1
        except (OSError, ValueError):
            # UNKNOWN, not zero. Returning [] with the count still 0 let the
            # caller read "could not open the ledger" as "no spend recorded",
            # so round 2's fix turned a loud crash into a silent $0 ceiling.
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
        """
        return getattr(self, "_unreadable", None)
