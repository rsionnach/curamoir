"""A scheduler's stop signal, turned into an exception for the length of a run.

WHY: systemd, CI cancellation and `kill` send SIGTERM, and a closed terminal
sends SIGHUP. Python's default for both ends the process on the spot,
without running a single `finally`. A run that dies that way leaves its
supervisor lock behind (E0-R2) and no job line, and every later run is then
refused until a human clears the lock. `job_guard.run_guarded` therefore
runs each job inside `TerminationSignals`, in two phases:

1. **Raising.** The first SIGTERM or SIGHUP raises `Terminated` where the
   job is, so the run unwinds through its `finally`: the job line is
   written and the lock released, as for any other crash.
2. **Held.** From `hold()` on, a signal (SIGINT, ^C, included) is only
   recorded. The job line's write and the lock's release are the steps an
   interruption must not cut short. `HoldOnExit` switches to this phase as
   the job's block exits.

On exit the handlers that were there before come back, and the first
signal that arrived is sent again under them. A process that would have
died by that signal still does, after the cleanup, so the scheduler sees
the signal it sent. This holds even when a body caught `Terminated` and
carried on.

Limits: only from the main thread (Python sets handlers nowhere else). A
forked child exiting through it restores the handlers but never re-sends a
signal its parent received. Never over a signal the process was started
ignoring (`nohup`), or over a handler not set from Python, which could not
be put back. SIGKILL and a power cut run no code at all; E0-R2's
stale-lock check is the backstop for those.
"""

import os
import signal
import threading


class Terminated(BaseException):
    """SIGTERM or SIGHUP arrived while `run_guarded` was running a job.

    A BaseException, as KeyboardInterrupt is, so a body's `except Exception`
    does not swallow a scheduler's stop."""

    def __init__(self, signum):
        self.signum = signum
        super().__init__(f"received {signal.Signals(signum).name}")


#: What a scheduler or a terminal sends to stop a run. SIGINT is already
#: KeyboardInterrupt; SIGKILL cannot be caught.
TERMINATING_SIGNALS = tuple(
    getattr(signal, name) for name in ("SIGTERM", "SIGHUP") if hasattr(signal, name))


class TerminationSignals:
    """While entered, TERMINATING_SIGNALS go through this object, in two
    phases (the module docstring).

    Raising, from entry: the first signal raises Terminated, once.
    Held, from `hold()`: signals, SIGINT included, are only recorded.

    On exit the replaced handlers are restored. If a signal arrived, the
    FIRST one is sent again, only when its handler was one this object
    replaced (a signal it never handled cannot be pending) and only in the
    process that received it (not in a forked child that inherited it).
    """

    def __init__(self):
        #: signum -> the handler this object replaced, to restore on exit.
        self._previous = {}
        self._raising = True
        self._pending = None
        #: The pid that received `_pending`. A forked child inherits this
        #: object, pending signal included, and must not re-send a signal
        #: that was its parent's.
        self._pending_pid = None

    def __enter__(self):
        if threading.current_thread() is not threading.main_thread():
            return self
        for signum in TERMINATING_SIGNALS:
            self._take(signum)
        return self

    def _take(self, signum):
        previous = signal.getsignal(signum)
        # SIG_IGN: started ignoring it (nohup), so it stops nothing. None: a
        # handler not set from Python, which could not be put back.
        if previous is signal.SIG_IGN or previous is None:
            return
        self._previous[signum] = previous
        signal.signal(signum, self._handle)

    def _handle(self, signum, _frame):
        # The first signal wins: it is the one that stopped the run, and the
        # one the process is to die by. A later one (a second SIGTERM, a ^C
        # while held) is a repeat of the same request. Recorded before
        # raising, so a body that catches Terminated and carries on still
        # has it sent again on exit.
        if self._pending is None:
            self._pending, self._pending_pid = signum, os.getpid()
        if self._raising:
            self._raising = False
            raise Terminated(signum)

    def hold(self):
        """Enter the held phase: from now on signals only wait for exit.
        Idempotent. Takes SIGINT too, so ^C cannot cut the line's write or
        the release short."""
        self._raising = False
        if (threading.current_thread() is threading.main_thread()
                and signal.SIGINT not in self._previous):
            self._take(signal.SIGINT)

    def __exit__(self, _type, _exc, _traceback):
        # Always restored, in a forked child too: handlers are per process,
        # and a child left with `_handle` would only record every later
        # SIGTERM and never die by one.
        for signum, previous in self._previous.items():
            signal.signal(signum, previous)
        if (self._pending is not None and self._pending in self._previous
                and self._pending_pid == os.getpid()):
            os.kill(os.getpid(), self._pending)
        return False


class HoldOnExit:
    """`signals.hold()` as this context exits. `run_guarded` lists it after
    the JobRun in one `with`, so it exits first. The held phase then starts
    before JobRun's exit writes the job line, so a signal during the write
    waits rather than leaving the run unrecorded while the release frees the
    repository."""

    def __init__(self, signals):
        self._signals = signals

    def __enter__(self):
        return self

    def __exit__(self, _type, _exc, _traceback):
        self._signals.hold()
        return False
