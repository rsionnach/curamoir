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


class JobRun:
    """A context manager that cannot leave without writing its ledger line."""

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

    def __enter__(self):
        self._started = self._now()
        return self

    def __exit__(self, exc_type, exc, _traceback):
        if exc is not None:
            # Record the crash, then let it propagate. Swallowing it here would
            # turn a failed run into a clean one.
            self._outcome = "failure"
            self._message = f"{exc_type.__name__}: {exc}"
        elif self._outcome is None:
            # Nobody reported anything. That is a bug in the job, not a success.
            self._outcome = "unknown"
            self._message = (
                "job finished without reporting an outcome; "
                "this is a bug in the job, not a successful run"
            )
        self._write()
        return False

    def succeeded(self, summary="", **fields):
        self._outcome = "success"
        self._message = summary
        self._extra.update(fields)

    def refused(self, refusal, **fields):
        self._outcome = "refused"
        self._reason = refusal.reason
        self._message = refusal.message
        self._extra.update(fields)

    def failed(self, message, **fields):
        self._outcome = "failure"
        self._message = message
        self._extra.update(fields)

    def _write(self):
        try:
            self._write_line()
        except OSError as failure:
            # The ledger is the audit trail, not the job. If it cannot be
            # written — a read-only checkout, a full disk — the operator still
            # needs the exception the job actually died of, so this one is
            # reported and swallowed rather than raised from __exit__ where it
            # would replace the real one.
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
