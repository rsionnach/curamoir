"""Shared plumbing for cúramóir's hook entry points.

One rule they all obey: **never raise, never block.** A hook that throws adds
noise to every turn, and because command hooks fail open it protects nothing by
dying. A hook that blocks turns a note-taking convenience into an obstacle.
"""

import json
import os
import sys
from pathlib import Path
from typing import NamedTuple, Optional

#: Where the supervisor lock lives, relative to `.curamoir/`. Mirrors
#: guards.lock.LOCK_PATH, not imported, to keep the hook stdlib-light.
_LOCK = ("state", "lock")

#: Same name as job_guard.JOB_VARIABLE; not imported, to keep the hook
#: stdlib-only.
_JOB_VARIABLE = "CURAMOIR_JOB"


def read_payload(stream=None):
    """The hook payload, or None if it cannot be parsed."""
    try:
        return json.load(stream or sys.stdin)
    except (ValueError, OSError):
        return None


def repo_from(payload):
    """The repository the hook is firing in, per the payload's `cwd`."""
    return Path(payload.get("cwd") or ".")


def run(handler):
    """Run `handler(payload, repo)`, swallowing everything. Always exits 0."""
    payload = read_payload()
    if payload is None:
        return 0
    try:
        repo = repo_from(payload)
        if not (repo / ".curamoir").is_dir():
            # cúramóir is not installed in this repo. Do nothing, quietly: the
            # developer works in places we have no business writing to.
            return 0
        handler(payload, repo)
    except Exception:
        return 0
    return 0


class JobSignal(NamedTuple):
    """Which signal said we are inside a cúramóir job: `job`, the name in
    CURAMOIR_JOB, when the variable did; else `lock`, the lock file that did.
    Always truthy (a non-empty tuple)."""

    job: Optional[str]
    lock: Optional[Path]


def in_curamoir_job(env, repo=None):
    """Whether we are running inside a cúramóir job: a JobSignal saying
    which signal fired, or None.

    Two signals, per DEC-5. The env var is primary: hook processes inherit the
    parent environment, and `job_guard.run_guarded` sets CURAMOIR_JOB for
    every job's whole run (cm-a2s), so every `claude -p` a job starts, and
    every hook that `claude` runs, carries it. CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1
    is documented to strip credentials only, by name or value shape, which a
    job's name is not; the supervisor lock is the fallback all the same, so
    that a variable lost on the way does not stop enforcement silently in the
    repository itself.

    The fix step runs `claude -p` with `--restricted`, which loads only
    managed settings and `--settings` (fix_step's docstring), so no plugin
    hook runs there except this one, which its `--settings` register
    (fix_step.pretooluse_settings, cm-60c). In its lockless sandbox clone
    the variable is the only signal.

    The lock is the one `curamoir run` holds for the whole job
    (job_guard.LockSlot, cm-x5a.6), so the fallback fires for a hook whose
    `cwd` is the repository the job is running against, while it runs. Two
    consequences: any session in that repository is treated as inside the
    job meanwhile, the developer's own included (the denylist errs towards
    enforcing); and a lock left by a killed run (stale, E0-R2) keeps it
    enforcing until a human clears it. The lock is machine-local and
    gitignored, so a clone of the repository (a sweep's sandbox) does not
    carry it: there the env var is the only signal.
    """
    if env.get(_JOB_VARIABLE):
        return JobSignal(job=env[_JOB_VARIABLE], lock=None)
    if repo is not None:
        lock = repo.joinpath(".curamoir", *_LOCK)
        # lexists, as the guard reads it: a dangling symlink the guard refuses
        # on is a lock here too.
        if os.path.lexists(lock):
            return JobSignal(job=None, lock=lock)
    return None
