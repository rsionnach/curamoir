"""E10-R1a (cm-x5a.8): `curamoir run <job>`, the one entry point schedulers call.

A local cron entry and a CI workflow both run `curamoir run <job>`, and both
get the same thing: the job's body run through `job_guard.run_guarded`,
which performs E10-R1's steps in its order (kill switch, lock slot, budget,
the open-PR cap for a job that opens PRs, sync seam, the job, the job's one
ledger line, lock-release slot; job_guard's docstring). One path is what
makes E10-R4 (identical records from both modes) a property rather than a
promise.

**One registry.** `JOBS` maps a job's name to its body and to whether it
opens PRs. A new job is a new entry, never a new code path: that is the
whole of what cm-x5a.1 has left to do for review, smoke, sync and grade,
which `NOT_YET_BUILT` names with the bead that builds each. Running one of
those exits EXIT_NOT_A_JOB, says which bead, and writes nothing: a ledger
line, even a failure, would let a scheduler's log read as if the job ran.

**The sweep is registered with a body that fails.** cm-fkd.4 builds the
pipeline and needs the name now. Until then `curamoir run sweep` writes a
`failure` line naming cm-fkd.4 and exits EXIT_FAILURE: never success, so a
scheduled sweep that does nothing cannot pass for one that found nothing.
Its open-PR listing is the same kind of placeholder, one that raises, so the
cap degrades with a guard line saying it was not enforced; cm-fkd.4 replaces
both with the `gh`-backed listing and the real body.

**Exit codes**, one per GuardedRun outcome a scheduler must tell apart:
EXIT_SUCCESS, EXIT_FAILURE (the body failed, crashed, or reported nothing,
which job_run calls UNKNOWN: a bug, not a success), EXIT_REFUSED (a guard or
the body declined, with a reason), EXIT_UNUSABLE_ROOT (nothing ran and no
line was written), plus EXIT_NOT_A_JOB. Each run reports one sentence naming
the outcome and why, which cli prints on stdout because slash commands relay
stdout (and cli.py is where cúramóir prints).

**An uninitialised repository is an unusable root.** The job line would
`mkdir` `.curamoir/`, after which `/curamoir:init` refuses for good
(cli._uninitialised has the history); like an unusable root, nothing runs
and nothing is written.
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple, Optional

from curamoir.brief import write_brief
from curamoir.job_guard import UNUSABLE_ROOT, run_guarded
from curamoir.job_run import FAILURE, REFUSED, SUCCESS, UNKNOWN

EXIT_SUCCESS = 0
EXIT_FAILURE = 1
#: Not a runnable job: unknown, or not built yet. 2, as argparse uses for a
#: command line it cannot act on.
EXIT_NOT_A_JOB = 2
EXIT_REFUSED = 3
EXIT_UNUSABLE_ROOT = 4

_EXIT_CODES = {
    SUCCESS: EXIT_SUCCESS,
    REFUSED: EXIT_REFUSED,
    FAILURE: EXIT_FAILURE,
    UNKNOWN: EXIT_FAILURE,
    UNUSABLE_ROOT: EXIT_UNUSABLE_ROOT,
}


@dataclass(frozen=True)
class Job:
    """One registered job.

    `body(repo, run)` does the work and reports its outcome on `run` (the
    JobRun writing the job's line). `opens_prs` subjects the job to the
    open-PR cap; `open_pr_listing` is then the listing callable the cap
    counts with (guards.pr_cap's contract).
    """

    body: Callable[[Path, Any], Any]
    #: Required, with no default: a PR-opening job registered without saying
    #: so would skip the cap by omission.
    opens_prs: bool
    open_pr_listing: Optional[Callable[[], Any]] = None


class Ran(NamedTuple):
    """What `run_job` did: the process's exit code and the sentence to print."""

    exit_code: int
    sentence: str


def _brief(repo, run):
    """What `curamoir brief` does: write `.curamoir/briefs/latest.md`."""
    path = write_brief(repo)
    # Relative to the root, so the line is the same from a cron checkout
    # and a CI one (E10-R4).
    run.succeeded(f"wrote {Path(path).relative_to(repo).as_posix()}")
    return path


def _sweep_not_built(_repo, run):
    run.failed("the sweep pipeline is not built until cm-fkd.4, so nothing was "
               "swept; this run did no work")


def _open_prs_not_listed_yet():
    raise NotImplementedError("the gh-backed open-PR listing is not built until cm-fkd.4")


JOBS = {
    "brief": Job(body=_brief, opens_prs=False),
    "sweep": Job(body=_sweep_not_built, opens_prs=True,
                 open_pr_listing=_open_prs_not_listed_yet),
}

#: E10-R1's other jobs, with the bead that builds each; cm-x5a.1 registers
#: them in JOBS once they exist.
NOT_YET_BUILT = {
    "review": "Epic 5 (cm-8ap)",
    "smoke": "cm-5f3.1",
    "sync": "cm-eqp.3",
    "grade": "cm-16x.1",
}


def run_job(repo, name, *, open_pr_listing=None, repo_labels=None, now=None):
    """Run the job registered as `name` against `repo` and return a Ran.

    `open_pr_listing`, when given, replaces the job's registered listing
    (tests; cm-fkd.4 registers the real one). It is ignored for a job that
    opens no PRs, which the cap does not apply to. `repo_labels` and `now`
    are run_guarded's.
    """
    job = JOBS.get(name)
    if job is None:
        return Ran(EXIT_NOT_A_JOB, _not_a_job(name))

    root = Path(repo)
    if root.is_dir() and not (root / ".curamoir").is_dir():
        # Not only "uninitialised": --repo may be a subdirectory of a
        # repository that is, or `.curamoir` may be a regular file.
        return Ran(EXIT_UNUSABLE_ROOT,
                   f"cúramóir did not run {name!r}: there is no .curamoir/ directory "
                   f"at {root}, so there is no ledger to record the run in. Point "
                   "--repo at the repository root, or run /curamoir:init.")

    try:
        result = run_guarded(
            root, name,
            job.open_pr_listing if open_pr_listing is None else open_pr_listing,
            lambda run: job.body(root, run),
            now=now, repo_labels=repo_labels, opens_prs=job.opens_prs,
        )
    except (Exception, SystemExit) as crash:
        # SystemExit too: a body calling sys.exit(0) has a `failure` line
        # from JobRun, and exiting 0 would contradict it. KeyboardInterrupt
        # is left to propagate (JobRun has recorded it, and run_guarded has
        # released the slot): an operator's ^C stops the runner.
        # This can fire before JobRun has entered (the clock raised, say),
        # so whether a ledger line was written is not ours to claim: a body
        # that raised has one from JobRun, an earlier raise has none. Either
        # way a sentence, not a traceback a slash command would relay as is.
        return Ran(EXIT_FAILURE, f"cúramóir did not complete {name!r}: "
                                 f"{_sentence(f'{type(crash).__name__}: {crash}')}")

    if result.outcome == UNUSABLE_ROOT:
        sentence = (f"cúramóir did not run {name!r}: unusable root (the reason is "
                    "on stderr), so no ledger line was written.")
    else:
        reason = f" ({result.reason})" if result.reason is not None else ""
        sentence = f"cúramóir ran {name!r}: {result.outcome}{reason}."
        if result.message.strip():
            sentence += f" {_sentence(result.message)}"
    return Ran(_EXIT_CODES[result.outcome], sentence)


def _not_a_job(name):
    if name in NOT_YET_BUILT:
        return (f"cúramóir cannot run {name!r} yet: it is built by "
                f"{NOT_YET_BUILT[name]} and registered by cm-x5a.1. Nothing ran.")
    return (f"cúramóir has no job {name!r}. Jobs that run today: "
            f"{', '.join(sorted(JOBS))}. Nothing ran.")


def _sentence(text):
    """`text` ending in exactly one full stop."""
    text = (text or "").strip()
    return text if text.endswith(".") else text + "."
