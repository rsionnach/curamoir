"""Shared plumbing for cúramóir's hook entry points.

One rule they all obey: **never raise, never block.** A hook that throws adds
noise to every turn, and because command hooks fail open it protects nothing by
dying. A hook that blocks turns a note-taking convenience into an obstacle.
"""

import json
import sys
from pathlib import Path


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
