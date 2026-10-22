"""E0-R9: one ledger line per job exit, whatever the exit looks like.

An unattended agent that dies silently is indistinguishable from one that had
nothing to do. The line is written on every path out of the block — success,
refusal, crash, and the programmer error of reporting nothing at all.

E11-R1 extends the line with token and cost attribution; this module owns only
the guarantee that there is always exactly one.
"""

import sys
import uuid
from datetime import datetime, timezone

# The job line's `outcome` values, exactly as the ledger holds them: other
# modules compare ledger lines against these (fix_counts, tier1) and the
# runner maps them to exit codes (job_guard.GuardedRun), so they never
# change. job_guard.UNUSABLE_ROOT is a GuardedRun outcome, never a line's.

#: The job reported success (JobRun.succeeded).
SUCCESS = "success"
#: The job declined to act, with a reason (JobRun.refused).
REFUSED = "refused"
#: The job reported a failure (JobRun.failed), or raised.
FAILURE = "failure"
#: The job exited without reporting an outcome: a bug in the job.
UNKNOWN = "unknown"


#: Fields the line writes itself (`_write_line` and `Ledger.record`). Passed
#: again through `**fields` they raise "multiple values for keyword argument"
#: as the line is written, which `_write` reports on stderr while the run
#: still reports its outcome: a job with no line. So they are refused when
#: given, where the mistake is. `at` is not here: `record` lets a field
#: replace it, and that is used.
RESERVED_FIELDS = frozenset(
    {"kind", "job", "reason", "message", "run_id", "outcome", "started", "ended"})


def _checked(fields):
    reserved = sorted(RESERVED_FIELDS.intersection(fields))
    if reserved:
        raise ValueError(f"{', '.join(reserved)}: the job line writes these itself, "
                         "so a job cannot set them as fields")
    return fields


class JobRun:
    """A context manager that cannot leave without writing its ledger line.

    `now` (default: the wall clock) dates the line's `started` and `ended`.
    It must return an aware UTC datetime and must not raise. A raise in
    `__enter__` happens before there is anything to exit, so no line is
    written; a naive value is not checked, and would be written without an
    offset.
    """

    def __init__(self, ledger, job, run_id=None, now=None):
        self._ledger = ledger
        self._job = job
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self._started = None
        self._outcome = None
        self._reason = None
        self._message = ""
        self._extra = {}

    @property
    def ledger(self):
        """The ledger this run's line goes to, for reading history from
        (`ledger.entries()`). Read-only, so a job cannot point its line
        somewhere other than where the history it read came from."""
        return self._ledger

    @property
    def outcome(self):
        """What the run recorded: SUCCESS, REFUSED, FAILURE, or UNKNOWN
        once it exited without reporting; None until then."""
        return self._outcome

    @property
    def reason(self):
        """The reason `refused` recorded, or None if it was never called
        or a later outcome replaced the refusal."""
        return self._reason

    @property
    def message(self):
        """The message the line carries (or will): the refusal's, the
        summary, the failure, the crash, or the unknown-outcome warning."""
        return self._message

    def __enter__(self):
        self._started = self._now()
        return self

    def __exit__(self, exc_type, exc, _traceback):
        if exc is not None:
            # Record the crash, then let it propagate. Swallowing it here would
            # turn a failed run into a clean one.
            self._outcome = FAILURE
            self._reason = None
            self._message = f"{exc_type.__name__}: {exc}"
        elif self._outcome is None:
            # Nobody reported anything. That is a bug in the job, not a success.
            self._outcome = UNKNOWN
            self._message = (
                "job finished without reporting an outcome; "
                "this is a bug in the job, not a successful run"
            )
        self._write()
        return False

    def note(self, **fields):
        """Fields for the line that are not an outcome, from a step that
        runs before the job reports one (job_guard.skip_sync). Kept whatever
        outcome follows; an outcome's own fields of the same name win.
        ValueError for a RESERVED_FIELDS key, as for the outcomes."""
        self._extra.update(_checked(fields))

    def succeeded(self, summary="", **fields):
        # The line carries the outcome last recorded, and a reason only when
        # that outcome is a refusal. Left over from an earlier `refused`, the
        # reason would write `success` with reason `budget`, a line that
        # contradicts itself in the brief.
        _checked(fields)
        self._outcome = SUCCESS
        self._reason = None
        self._message = summary
        self._extra.update(fields)

    def refused(self, refusal, **fields):
        _checked(fields)
        self._outcome = REFUSED
        self._reason = refusal.reason
        self._message = refusal.message
        self._extra.update(fields)

    def failed(self, message, **fields):
        # As in `succeeded`: a failure has no refusal reason.
        _checked(fields)
        self._outcome = FAILURE
        self._reason = None
        self._message = message
        self._extra.update(fields)

    def _write(self):
        try:
            self._write_line()
        except Exception as failure:
            # `except Exception`, and the breadth is the point rather than an
            # oversight. The invariant this method holds is directional and
            # type-agnostic — NO ledger problem may fail the job it audits —
            # and an exception allowlist cannot express that. `default=str` in
            # `record()` closes the value-encoding path, but not the others:
            # `sort_keys=True` sorts before `default` is consulted, so a dict
            # keyed by both ints and strings raises TypeError; a circular
            # reference raises ValueError; a value whose `__str__` raises
            # raises that. Each one failed a job that had already SUCCEEDED,
            # with no line written — E0-R9 broken in the one direction nothing
            # else catches. Not silent: it goes to stderr.
            print(
                f"cúramóir could not write the ledger line for {self._job!r}: "
                f"{failure}",
                file=sys.stderr,
            )

    def _write_line(self):
        ended = self._now()
        self._ledger.record(
            kind="job",
            job=self._job,
            reason=self._reason,
            message=self._message,
            run_id=self.run_id,
            outcome=self._outcome,
            started=self._started.isoformat(),
            ended=ended.isoformat(),
            **self._extra,
        )
