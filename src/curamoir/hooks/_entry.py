"""Shared plumbing for cúramóir's hook entry points.

One rule they all obey: **never raise, never block.** A hook that throws adds
noise to every turn, and because command hooks fail open it protects nothing by
dying. A hook that blocks turns a note-taking convenience into an obstacle.
"""

import json
import sys
from pathlib import Path

#: Where the supervisor lock lives, relative to `.curamoir/`.
_LOCK = ("state", "lock")


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


def in_curamoir_job(env, repo=None):
    """Whether we are running inside a cúramóir job.

    Two signals, per DEC-5. The env var is primary: hook processes inherit the
    parent environment, so whatever spawns a job's `claude -p` is to export
    CURAMOIR_JOB. Nothing does yet; the one `claude -p` cúramóir starts
    today, the sweep's fix step, has no Bash tool. The supervisor lock is the
    fallback, because CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1 strips extra
    variables and we would otherwise stop enforcing silently.

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
    if env.get("CURAMOIR_JOB"):
        return True
    if repo is not None:
        return repo.joinpath(".curamoir", *_LOCK).exists()
    return False
