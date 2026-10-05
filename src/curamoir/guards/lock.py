"""E0-R2: the supervisor lock.

Exactly one autonomous cúramóir job runs against a repository at a time
(operating assumption 3: no swarms, no worktrees, no concurrent agents).

Stale locks are reported, not silently overridden. A lock left behind by a
crash is evidence; stepping over it automatically is how two agents end up
writing to the same repo at once. The job refuses, says the lock looks stale,
and leaves clearing it to a human — or to E10-R5's crash path, which owns
releasing a lock its own job left behind.
"""

import json
import os
import socket
from datetime import datetime, timezone

from curamoir.guards.reasons import Reason
from curamoir.guards.result import Refusal

LOCK_PATH = ("state", "lock")
DEFAULT_TIMEOUT_SECONDS = 6 * 60 * 60


class SupervisorLock:
    """Refuses a job while another holds the lock.

    `now` and `timeout_seconds` are injected so liveness is testable without
    sleeping, and so a deployment can widen the window for a slow job.
    """

    def __init__(self, timeout_seconds=DEFAULT_TIMEOUT_SECONDS, now=None):
        self._timeout_seconds = timeout_seconds
        self._now = now or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def this_host():
        return socket.gethostname()

    def __call__(self, context):
        if context.repo is None:
            return None
        path = context.repo.joinpath(".curamoir", *LOCK_PATH)
        if not path.exists():
            return None

        try:
            held = json.loads(path.read_text(encoding="utf-8"))
            pid = int(held["pid"])
            host = str(held["host"])
            job = str(held.get("job", "unknown"))
            started = held["started"]
        except (ValueError, KeyError, TypeError, OSError) as error:
            # An unreadable lock is not an absent lock. We cannot show it is
            # safe to proceed, so we do not.
            return Refusal(
                reason=Reason.LOCKED,
                message=(
                    f"lock at {path} could not be read ({error}); refusing rather "
                    "than assuming the repository is free. Inspect and delete it "
                    "if no cúramóir job is running."
                ),
            )

        stale_because = self._staleness(pid, host, started)
        held_by = f"job {job!r}, pid {pid} on {host!r}, started {started}"
        if stale_because:
            return Refusal(
                reason=Reason.LOCKED,
                message=(
                    f"lock looks STALE ({stale_because}): {held_by}. Reported, not "
                    f"overridden. Delete {path} if you are sure no job is running."
                ),
            )
        return Refusal(
            reason=Reason.LOCKED,
            message=f"another cúramóir job holds the lock: {held_by}.",
        )

    def _staleness(self, pid, host, started):
        """Why this lock looks stale, or None if it looks live."""
        if host == self.this_host() and not self._pid_alive(pid):
            return f"pid {pid} is not running on this host"

        age = self._age_seconds(started)
        if age is None:
            return f"unparseable start time {started!r}"
        if age > self._timeout_seconds:
            return f"age {int(age)}s exceeds timeout {self._timeout_seconds}s"
        return None

    def _age_seconds(self, started):
        try:
            when = datetime.fromisoformat(started)
        except (ValueError, TypeError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return (self._now() - when).total_seconds()

    @staticmethod
    def _pid_alive(pid):
        """Whether a pid is running. Only meaningful on the lock's own host."""
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            # Running, owned by someone else.
            return True
        except OSError:
            return True
        return True
