"""E0-R1: cúramóir never writes to the repository's default branch.

All changes go to a branch carrying the configured prefix and are opened as a
PR. Enforced in code, not prompt.

The default branch is detected, never assumed to be `main`: a repo on `master`,
`trunk` or `develop` deserves the same protection, and getting this wrong means
the one guardrail everything else rests on silently does nothing.
"""

import subprocess

from curamoir.guards.reasons import Reason
from curamoir.guards.result import Refusal

DEFAULT_PREFIX = "curamoir/"
COMMON_DEFAULTS = ("main", "master", "trunk", "develop")


class DefaultBranchGuard:
    """Refuses any side effect attempted from the default branch, or from a
    branch outside the configured prefix.

    `detect_default` is injectable so the detection strategy can be replaced
    (and tested) without touching the refusal logic.
    """

    def __init__(self, branch_prefix=DEFAULT_PREFIX, detect_default=None):
        self._prefix = branch_prefix
        self._detect_default = detect_default or detect_default_branch

    def __call__(self, context):
        if context.repo is None:
            return None

        current = _git(context.repo, "rev-parse", "--abbrev-ref", "HEAD")
        default = self._detect_default(context.repo)

        if default is not None and current == default:
            return Refusal(
                reason=Reason.DEFAULT_BRANCH,
                message=(
                    f"refusing to write on the default branch {current!r}. "
                    f"cúramóir works on {self._prefix}* branches and opens a PR."
                ),
            )

        if not current.startswith(self._prefix):
            return Refusal(
                reason=Reason.DEFAULT_BRANCH,
                message=(
                    f"branch {current!r} is outside the {self._prefix!r} prefix. "
                    f"cúramóir's own work belongs on {self._prefix}* so the open-PR "
                    "cap and sweep branch naming stay meaningful."
                ),
            )

        return None


def detect_default_branch(repo):
    """The repository's default branch, or None if it cannot be determined.

    Tried in order of trustworthiness. Returning None rather than guessing
    matters: the prefix check still applies, so an undetectable default
    degrades to "must be on the prefix" rather than to "anything goes".
    """
    remote_head = _git(repo, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    if remote_head:
        return remote_head.rsplit("/", 1)[-1]

    # Explicit per-repo override only. `init.defaultBranch` is deliberately NOT
    # consulted: it says what NEWLY created repos get named and is usually
    # inherited from global or system config, so on a repo whose branch is
    # `trunk` it cheerfully reports `main` — detection that is worse than none.
    configured = _git(repo, "config", "--local", "--get", "curamoir.defaultBranch")
    if configured:
        return configured

    branches = _git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads").split()
    if len(branches) == 1:
        return branches[0]
    for candidate in COMMON_DEFAULTS:
        if candidate in branches:
            return candidate
    return None


def _git(repo, *args):
    """Run git, returning stripped stdout, or "" if the command fails.

    Failure is normal here: `symbolic-ref` and `config --get` exit non-zero when
    the thing simply is not set.
    """
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=False
    )
    return result.stdout.strip() if result.returncode == 0 else ""
