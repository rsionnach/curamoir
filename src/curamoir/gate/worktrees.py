"""R5-R38 to R5-R43: the worktrees the mutation runner works in.

**Where** (R5-R38, d-cm-8ap.19-13). Under the repository's git common
directory, never in its working tree:

    <git-common-dir>/r5-worktrees/<scope>/<run id>/mutation-<n>/

``scope`` is the bead the gate runs for, or ``standalone`` for a run of
``curamoir run mutations`` without one; ``run id`` is unique per run (the
UTC time, the pid and a random suffix), so two runs never share a name.
Each mutation worktree is ``git worktree add --detach <head>``.

**Ownership.** A run holds an exclusive flock on ``owner.lock`` in its run
directory for as long as it lives, and writes ``owner.json`` (pid, start
time, base, head) beside it for a reader. The kernel drops the lock when the
process dies, however it dies, so "the owning run is not alive" is "the lock
can be taken": a run directory whose lock is free is an orphan (R5-R42). A
reused pid cannot make a dead run look alive.

**Lifecycle** (R5-R41). ``RunWorktrees`` is a context manager: on exit,
inside ``TerminationSignals``' held phase so a second signal cannot cut it
short, every worktree it made is removed with ``git worktree remove
--force``, the run directory goes (before the lock is let go), and ``git
worktree prune`` runs only if one of them could not be removed. A run
killed outright (SIGKILL) cannot do that; its directory is then an orphan,
which the next run names on stderr and ``/curamoir:doctor`` reports
(``orphans``, ``ORPHAN_SENTENCE``).

**Refuse, don't degrade** (R5-R43). A worktree that cannot be made is a
WorktreeError; nothing runs in the user's checkout instead.

**The cap** (R5-R40, d-cm-8ap.19-15). ``max_parallel``: on the local
runner the configured ``gate.worktrees.max_parallel`` (default 2); on a
hosted runner ``os.cpu_count()`` capped at 8, or the configured value if
``curamoir.yaml`` sets one and it is lower.
"""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import os
import secrets
import shutil
from pathlib import Path

from curamoir.config import RunnerKind
from curamoir.git_guard import GitGuardError, git
from curamoir.termination import TerminationSignals

ROOT_NAME = "r5-worktrees"
STANDALONE = "standalone"
OWNER_LOCK = "owner.lock"
OWNER_FILE = "owner.json"
HOSTED_CAP = 8
DEFAULT_MAX_PARALLEL = 2

#: doctor's sentence for an orphan, also printed when a run starts.
ORPHAN_SENTENCE = ("orphan R5 worktree run {path} (scope {scope}): its run is not alive; "
                   "remove each worktree in it with `git worktree remove --force <path>`, "
                   "then the directory, and run `git worktree prune`")


class WorktreeError(Exception):
    """A worktree could not be made or removed (R5-R43: refuse, don't degrade)."""


class CleanupError(WorktreeError):
    """The run's worktrees could not all be removed when it ended: what ran
    still stands, and the leftovers are an orphan for doctor."""


def worktrees_root(repo) -> Path:
    """``<git-common-dir>/r5-worktrees`` for the repository at ``repo``."""
    try:
        common = git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    except GitGuardError as error:
        raise WorktreeError(f"{repo} is not a git repository: {error}") from error
    return Path(common.decode("utf-8", "surrogateescape").strip()) / ROOT_NAME


def max_parallel(runner, configured: int, explicit: bool) -> int:
    """The worktree cap for ``runner`` (config.Runner), from the configured
    value and whether curamoir.yaml set it (module docstring, "The cap")."""
    if runner.kind is RunnerKind.HOSTED:
        cap = min(os.cpu_count() or 1, HOSTED_CAP)
        return min(cap, configured) if explicit else cap
    return configured


def _new_run_id() -> str:
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{now}-{os.getpid()}-{secrets.token_hex(3)}"


class RunWorktrees:
    """The worktrees of one run (module docstring). ``add(head)`` makes the
    next ``mutation-<n>`` worktree and returns its path; leaving the context
    removes them all."""

    def __init__(self, repo, scope: str | None, *, base: str, head: str):
        self.repo = Path(repo)
        self.scope = scope or STANDALONE
        self.run_id = _new_run_id()
        self.directory = worktrees_root(repo) / self.scope / self.run_id
        self.base, self.head = base, head
        self.created: list = []
        self.removed: list = []
        self._lock = None
        self._count = 0

    def __enter__(self):
        # The lock is taken in a directory nothing else looks in, and the
        # directory is renamed to its place only then, so no reader ever
        # sees a run directory whose owner does not yet hold its lock.
        staging = self.directory.parent / f".{self.run_id}.starting"
        try:
            staging.mkdir(parents=True)
            self._lock = open(staging / OWNER_LOCK, "w")
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.rename(staging, self.directory)
            (self.directory / OWNER_FILE).write_text(json.dumps({
                "pid": os.getpid(), "run_id": self.run_id, "scope": self.scope,
                "base": self.base, "head": self.head,
                "started": datetime.datetime.now(datetime.timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%SZ")}) + "\n", encoding="utf-8")
        except OSError as error:
            self._close_lock()
            shutil.rmtree(staging, ignore_errors=True)
            raise WorktreeError(f"the run directory {self.directory} could not be made: "
                                f"{error}") from error
        return self

    def add(self) -> Path:
        self._count += 1
        path = self.directory / f"mutation-{self._count}"
        try:
            git(self.repo, "worktree", "add", "--detach", str(path), self.head)
        except GitGuardError as error:
            raise WorktreeError(f"git worktree add failed for {path}: {error}") from error
        self.created.append(str(path))
        return path

    def __exit__(self, *exc):
        with TerminationSignals() as signals:
            signals.hold()
            problems = []
            for path in self.created:
                if path in self.removed:
                    continue
                try:
                    git(self.repo, "worktree", "remove", "--force", path)
                    self.removed.append(path)
                except GitGuardError as error:
                    problems.append(f"{path}: {error}")
            if problems:
                # Only when one of this run's own removals failed: a plain
                # prune would also drop the user's worktrees whose
                # directories happen to be missing now.
                with contextlib.suppress(GitGuardError):
                    git(self.repo, "worktree", "prune")
            else:
                # Removed while the lock is still held, so no probe sees a
                # finishing run's directory unlocked. The scope directory
                # stays: removing it could race another run starting there.
                # Renamed out of the probes' sight first (orphans skips a
                # dot name), so no probe sees it with its lock file gone.
                leaving = self.directory.parent / f".{self.run_id}.leaving"
                try:
                    os.rename(self.directory, leaving)
                except OSError:
                    leaving = self.directory
                shutil.rmtree(leaving, ignore_errors=True)
            self._close_lock()
        if problems and exc[0] is None:
            raise CleanupError("worktrees could not be removed: " + "; ".join(problems))
        return False

    def _close_lock(self):
        if self._lock is not None:
            with contextlib.suppress(OSError):
                self._lock.close()
            self._lock = None


def orphans(repo) -> list:
    """[(run directory, scope)] for every run directory under the
    repository's worktrees root whose owner lock is free: its run is not
    alive. A directory whose lock cannot be opened at all is reported too,
    since nothing shows a live owner."""
    try:
        root = worktrees_root(repo)
    except WorktreeError:
        return []
    found = []
    if not root.is_dir():
        return found
    for scope in sorted(p for p in root.iterdir() if p.is_dir()):
        # A ".<run id>.starting" directory is a run taking its lock; it is
        # renamed into place once it holds it.
        for run in sorted(p for p in scope.iterdir()
                          if p.is_dir() and not p.name.startswith(".")):
            if not _alive(run):
                found.append((run, scope.name))
    return found


def _alive(run: Path) -> bool:
    """Whether the run that owns `run` still holds its lock. The lock file
    is opened read-only and never created here: a probe must not make one."""
    try:
        with open(run / OWNER_LOCK) as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(handle, fcntl.LOCK_UN)
            return False
    except OSError:
        return False


def orphan_sentences(repo) -> list:
    return [ORPHAN_SENTENCE.format(path=path, scope=scope) for path, scope in orphans(repo)]
