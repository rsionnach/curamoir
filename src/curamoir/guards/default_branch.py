"""E0-R1: cúramóir never writes to the repository's default branch.

All changes go to a branch carrying the configured prefix and are opened as a
PR. Enforced in code, not prompt.

The default branch is detected, never assumed to be `main`: a repo on `master`,
`trunk` or `develop` deserves the same protection, and getting this wrong means
the one guardrail everything else rests on silently does nothing.
"""

import subprocess

from curamoir.guards.context import unusable_repo
from curamoir.guards.reasons import Reason
from curamoir.guards.result import Degraded, Refusal

DEFAULT_PREFIX = "curamoir/"
COMMON_DEFAULTS = ("main", "master", "trunk", "develop")


class DefaultBranchGuard:
    """Refuses any side effect attempted from the default branch, or from a
    branch outside the configured prefix.

    `detect_default` is injectable so the detection strategy can be replaced
    (and tested) without touching the refusal logic.

    `target_branch`, when given, is the branch judged instead of the one
    checked out: the sweep never checks its branch out, it pushes a commit
    to `<prefix>sweep-...` from a checkout that may be on the default
    branch (curamoir.publish). An empty target is a Refusal, not the
    Degraded an unknown HEAD gets: HEAD is git's to report and may fail to;
    a target is what the caller is about to write, and a write to a branch
    nobody named must not happen.
    """

    def __init__(self, branch_prefix=DEFAULT_PREFIX, detect_default=None, target_branch=None):
        self._prefix = branch_prefix
        self._detect_default = detect_default or detect_default_branch
        self._target = target_branch

    def __call__(self, context):
        unusable = unusable_repo(context)
        if unusable is not None:
            return Degraded(
                reason=Reason.DEFAULT_BRANCH,
                message=(
                    f"{unusable}, so the default-branch check did NOT run. "
                    "Resolve the repository root before attempting the job."
                ),
            )

        if self._target is not None and not (isinstance(self._target, str) and self._target):
            return Refusal(
                reason=Reason.DEFAULT_BRANCH,
                message=(f"the target branch {self._target!r} names no branch, so nothing "
                         "is written to it"),
            )
        current = (self._target if self._target is not None
                   else _git(context.repo, "rev-parse", "--abbrev-ref", "HEAD"))
        default = self._detect_default(context.repo)

        if not current:
            # `_git` returns "" for any non-zero exit: a repo with no commits,
            # a corrupt .git, a cwd that is not a work tree. An unknown current
            # branch is not a safe branch, and with an empty configured prefix
            # `"".startswith("")` was True — so the protection was conditional
            # on detection succeeding, which the docstring says it is not.
            return Degraded(
                reason=Reason.DEFAULT_BRANCH,
                message=(
                    "the current branch could not be determined, so the "
                    "default-branch check did NOT run"
                ),
            )

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
        # Strip the known prefix rather than splitting on the last slash, which
        # turned refs/remotes/origin/release/main into "main" — so the
        # default-branch comparison never fired for a slashed branch name.
        prefix = "refs/remotes/origin/"
        return remote_head[len(prefix):] if remote_head.startswith(prefix) else remote_head.rsplit("/", 1)[-1]

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
