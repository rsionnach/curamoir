"""What a guard is told about the side effect being attempted."""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class JobContext:
    """One attempted side effect, described well enough to judge it.

    Guards need more than a job name: E0-R1 must find the default branch, E0-R3
    must look for `.curamoir/PAUSE`, E0-R4 must read the ledger. They all need
    the repo. `job` is what the ledger records; `repo` is what guards inspect.

    `repo` is required, and has no default on purpose. Four of the five guards
    read the repo and can say nothing without it, so a context that omitted it
    ran the side effect with the kill switch, the lock, the default-branch check
    and the spend ceiling all disengaged — and recorded nothing. A caller that
    cannot resolve the root must fail closed itself rather than build a context
    that cannot be guarded. `None` remains expressible, so that a caller who
    means it gets four `Degraded` records instead of a silent pass.
    """

    job: str
    repo: Optional[Path]


def unusable_repo(context):
    """Why `context.repo` cannot be inspected, or None if it can.

    One predicate, so that the four repo-reading guards cannot disagree about
    what counts as a repository. Each used to test `repo is None` and nothing
    else, which answered one value of a question with several wrong answers:

    - a path that no longer exists, or that is a regular file, is what an
      unattended scheduled job meets when a repo is moved or deleted between
      being scheduled and being run;
    - `Path("")` resolves to the CURRENT WORKING DIRECTORY, which is what
      `Path(os.environ.get("REPO_ROOT", ""))` yields from an unset variable. The
      kill switch then reported "not paused" after looking for
      `./.curamoir/PAUSE` wherever the process happened to be.

    Each of those produced a clean, unrecorded pass — the outcome `Degraded`
    exists to prevent.

    The `Path("")` case is NOT caught here, and cannot be: pathlib normalises
    it to `.` before any guard sees it, so by this point an unset variable is
    indistinguishable from a deliberate `--repo .`. I tried requiring
    `.curamoir/` to catch it and withdrew the change — it is not an honest
    precondition for all four guards, because `DefaultBranchGuard` reads git and
    the kill switch's label path reads a remote, so it refused seven legitimate
    cases to catch one illegitimate one.

    The place to catch an empty root is where the root is RESOLVED, which is the
    runner (E10) — and `JobContext`'s own docstring already states the rule: a
    caller that cannot resolve the repo must fail closed itself. Recorded on
    bead cm-7ck so the runner's own validation is not left to be rediscovered.
    """
    if context.repo is None:
        return "the repo root is unknown"
    if not Path(context.repo).is_dir():
        return f"{context.repo} is not a directory"
    return None
