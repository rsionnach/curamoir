"""E0-R2: the supervisor lock.

Exactly one autonomous cúramóir job runs against a repository at a time
(operating assumption 3: no swarms, no worktrees, no concurrent agents).

Stale locks are reported, not silently overridden. A lock left behind by a
crash is evidence; stepping over it automatically is how two agents end up
writing to the same repo at once. The job refuses, says the lock looks stale,
and leaves clearing it to a human — or to E10-R5's crash path, which owns
releasing a lock its own job left behind.

The reader is `read_lock`, whose LockState carries the staleness verdict
for everything that shows the lock (`SupervisorLock`, the guard; status;
doctor), so they never disagree about it (cm-azr). The writer is
`create_lock` (with `lock_bytes`), beside it so the lock's shape is defined
in one module.
`job_guard.LockSlot` is the one caller that acquires and releases
(cm-x5a.6). The shape is a JSON object: `pid` and `host` (liveness on the
lock's own host), `job` (named in the refusal), `started` (an aware
ISO-8601 wall-clock time, for the age check everywhere else), and `token`,
which the reader ignores. The release compares the whole file with the
bytes its run wrote, and the token makes those bytes unique, so a run
removes only the lock it wrote.
"""

import errno
import json
import os
import socket
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from curamoir.guards.context import unusable_repo
from curamoir.guards.reasons import Reason
from curamoir.guards.result import Degraded, Refusal

LOCK_PATH = ("state", "lock")
DEFAULT_TIMEOUT_SECONDS = 6 * 60 * 60

#: Tokens of the locks this process has created and not yet released. A
#: lock naming this process's pid on this host is live only if its token
#: is here: otherwise an earlier process with the same pid wrote it (a
#: container restarted, a pid reused) and it is stale.
_HELD_BY_THIS_PROCESS = set()


def _token(written):
    try:
        return json.loads(written).get("token")
    except (ValueError, AttributeError):
        return None


def lock_file(repo):
    """Where `repo`'s supervisor lock lives."""
    return Path(repo).joinpath(".curamoir", *LOCK_PATH)


def lock_bytes(job):
    """The lock `create_lock` writes for `job`, by this process on this host,
    started now. Unique (the token), so the bytes are the writer's proof
    that a lock is its own.

    `started` is the wall clock, never a run's injected clock: the reader
    ages the lock by the wall clock.
    """
    return (json.dumps({
        "pid": os.getpid(),
        "host": SupervisorLock.this_host(),
        "job": job,
        "started": datetime.now(timezone.utc).isoformat(),
        "token": uuid.uuid4().hex,
    }, sort_keys=True) + "\n").encode("utf-8")


def create_lock(repo, written):
    """Take `repo`'s lock: write `written` (from `lock_bytes`) to it,
    atomically. The caller has the bytes before the file exists, so it can
    recognise its own lock whenever it is interrupted.

    FileExistsError when a lock is already there, whoever wrote it, and
    only then. The create is O_CREAT|O_EXCL, so of two runs that both found
    no lock, the filesystem admits exactly one; checking and then writing
    would admit both. Creates `.curamoir/state/` if it is missing (scaffold
    makes it, but nothing keeps it); a `state` that is not a directory is
    NotADirectoryError, not FileExistsError, because no lock is held. Any
    other OSError leaves no file behind. Records the lock as held by this
    process (`forget_lock` undoes it), which is how the reader tells this
    process's lock from a stale one carrying the same pid.
    """
    path = lock_file(repo)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except FileExistsError as error:
        raise NotADirectoryError(errno.ENOTDIR, "exists and is not a directory",
                                 str(error.filename or path.parent)) from error
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        view = memoryview(written)
        while view:
            view = view[os.write(descriptor, view):]
    except BaseException:
        # Half a lock is an unreadable lock, which refuses every later run.
        os.close(descriptor)
        path.unlink(missing_ok=True)
        raise
    os.close(descriptor)
    _HELD_BY_THIS_PROCESS.add(_token(written))


@dataclass(frozen=True)
class LockState:
    """A repository's lock as `read_lock` found it.

    `present` is whether anything is at `path` (lexists: a dangling symlink
    is present). When present, either `unreadable` says why the holder
    cannot be read, or the holder fields are set and `stale_because` says
    why the lock looks stale (None: it looks live). `age_seconds` is None
    when the start time cannot be parsed, and negative when it is ahead of
    this clock."""

    path: Path
    present: bool
    unreadable: Optional[str] = None
    job: Optional[str] = None
    pid: Optional[int] = None
    host: Optional[str] = None
    started: Optional[str] = None
    age_seconds: Optional[float] = None
    stale_because: Optional[str] = None


def read_lock(repo, timeout_seconds=DEFAULT_TIMEOUT_SECONDS, now=None):
    """`repo`'s lock and the guard's verdict on it (LockState). Never
    raises for what is in the file; OSError from `repo` itself only."""
    return SupervisorLock(timeout_seconds=timeout_seconds, now=now).read(repo)


def forget_lock(written):
    """This process no longer holds the lock `written` (its release)."""
    _HELD_BY_THIS_PROCESS.discard(_token(written))


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
        unusable = unusable_repo(context)
        if unusable is not None:
            return Degraded(
                reason=Reason.LOCKED,
                message=(
                    f"{unusable}, so the supervisor-lock check did NOT run. "
                    "Resolve the repository root before attempting the job."
                ),
            )
        state = self.read(context.repo)
        path = state.path
        if not state.present:
            return None
        if state.unreadable is not None:
            # An unreadable lock is not an absent lock. We cannot show it is
            # safe to proceed, so we do not.
            return Refusal(
                reason=Reason.LOCKED,
                message=(
                    f"lock at {path} could not be read ({state.unreadable}); refusing "
                    "rather than assuming the repository is free. Inspect and delete "
                    "it if no cúramóir job is running."
                ),
            )

        held_by = (f"job {state.job!r}, pid {state.pid} on {state.host!r}, "
                   f"started {state.started}")
        if state.stale_because:
            return Refusal(
                reason=Reason.LOCKED,
                message=(
                    f"lock looks STALE ({state.stale_because}): {held_by}. Reported, "
                    f"not overridden. Delete {path} if you are sure no job is running."
                ),
            )
        age = state.age_seconds
        clock_note = (" (its start time is in the future: this host's clock has "
                      "gone back since it was written)") if age is not None and age < 0 else ""
        return Refusal(
            reason=Reason.LOCKED,
            message=f"another cúramóir job holds the lock: {held_by}{clock_note}.",
        )

    def read(self, repo):
        """`repo`'s lock as a LockState, judged with this guard's clock and
        timeout (read_lock)."""
        path = lock_file(repo)
        # lexists: a dangling symlink there is not a free repository; reading
        # it fails below, and the lock is unreadable, not absent.
        if not os.path.lexists(path):
            return LockState(path=path, present=False)
        try:
            held = json.loads(path.read_text(encoding="utf-8"))
            pid = int(held["pid"])
            host = str(held["host"])
            job = str(held.get("job", "unknown"))
            started = held["started"]
            token = held.get("token")
        except (ValueError, KeyError, TypeError, OSError) as error:
            # TypeError: valid JSON that is not an object ([], "x", 7) cannot
            # be indexed by a key.
            return LockState(path=path, present=True,
                             unreadable=str(error) or type(error).__name__)
        return LockState(path=path, present=True, job=job, pid=pid, host=host,
                         started=started, age_seconds=self._age_seconds(started),
                         stale_because=self._staleness(pid, host, started, token))

    def _staleness(self, pid, host, started, token=None):
        """Why this lock looks stale, or None if it looks live."""
        same_host = host == self.this_host()
        if same_host and pid == os.getpid() and token not in _HELD_BY_THIS_PROCESS:
            return f"its pid {pid} is this process, which does not hold it"
        if same_host and not self._pid_alive(pid):
            return f"pid {pid} is not running on this host"

        age = self._age_seconds(started)
        if age is None:
            return f"unparseable start time {started!r}"
        if age < 0 and same_host:
            # The pid is alive on this host, so the lock is live whatever the
            # clock says: this host's clock went back (NTP, a VM restore).
            return None
        if age < 0:
            # A negative age is never greater than the timeout, so a lock
            # stamped in the future never aged out: a CI machine or VM whose
            # clock runs ahead blocked the repo indefinitely, and the message
            # gave no hint that clearing it was safe.
            return (
                f"start time {started!r} is in the future, so the clock that "
                "wrote this lock disagrees with this one"
            )
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
