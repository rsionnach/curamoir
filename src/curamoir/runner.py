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

**The sweep** is curamoir.sweep_job.run_sweep (cm-fkd.14), whose open-PR
listing is `gh pr list` through curamoir.github.open_sweep_prs, bound to
the run's root and to origin's repository (_gh_open_prs). It reads the
run's clock (JobRun.now, `run_job`'s `now`), so its branch and title are
dated by the clock that dates its line (read again as the body starts);
its `claude` and its fix step's timeout are sweep_job.CLAUDE and
sweep_job.FIX_TIMEOUT, the seams tests point at stand-ins
(tests/test_sweep_acceptance.py).

**gh, by default** (cm-fkd.10, cm-fkd.14). With no arguments, `run_job`
gives every job the `curamoir:paused` label check (github.repo_labels,
_gh_labels): the kill switch is per run, not per job, so the brief is
paused by the label as the sweep is. A job that opens PRs gets its
registered listing, a factory the runner binds to the root
(`Job.open_pr_listing_for`). Both are plain callables called when the
guard runs, never cached, and both raise when gh cannot answer, which
degrades the check with a guard line saying so (guards.kill_switch,
guards.pr_cap). `run_job`'s `repo_labels` and `open_pr_listing` replace
them (tests; a caller with its own source).

**Which repository gh is asked about** (cm-fkd.14). gh picks a
repository from its own default or the remotes (upstream first), but the
sweep pushes to origin and opens its PR there (curamoir.publish, "Which
repository"). So both defaults name origin's repository with `--repo`,
publish.origin_gh_repo(root), resolved when the guard calls them (not
when the run starts: a raise there would end the run before any line).
The gateway's checks run before the body exists, so they cannot share the
body's one resolution (sweep_job.resolve_sweep_base, used for the
decision's listing and for publish). The cap's listing, the label check
and that resolution all read origin's URL from the same git config, and
publish refuses to push unless the value it is given is still origin's,
so they cannot come to name different repositories without the sweep
refusing.
- The open-PR cap's listing always names origin's repository: the PRs
  counted are those of the repository the PR opens on, as the sweep's
  own decision lists them. An origin that names no GitHub repository
  raises, and the cap is degraded; the sweep then refuses for want of a
  base (Reason.SWEEP_BASE) before anything is cloned or spent.
- The label check reads origin's labels too, so in a fork the fork's
  owner pauses cúramóir with a label on the fork, the repository the PR
  opens on, never upstream's. When origin names no GitHub repository at
  all (publish.PublishRefused: a local path, no origin), it falls back to
  gh's own resolution, the cm-fkd.10 behaviour, so the brief on such a
  repository is checked as before. Known cost: an origin URL through an
  SSH host alias (`git@work:owner/repo`) names a host gh does not know,
  so the label check degrades there (a guard line says so) where gh's
  own resolution may have found the repository.

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
from typing import Any, Optional

from curamoir import github, publish
from curamoir.brief import write_brief
from curamoir.job_guard import UNUSABLE_ROOT, Terminated, run_guarded
from curamoir.job_run import FAILURE, REFUSED, SUCCESS, UNKNOWN
from curamoir.sweep_job import run_sweep

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
    open-PR cap; `open_pr_listing_for(root)` then returns the listing
    callable the cap counts with (guards.pr_cap's contract: no arguments),
    for the repository at `root`. A factory, because the registry is built
    before any run knows its repository.
    """

    body: Callable[[Path, Any], Any]
    #: Required, with no default: a PR-opening job registered without saying
    #: so would skip the cap by omission.
    opens_prs: bool
    open_pr_listing_for: Optional[Callable[[Path], Callable[[], Any]]] = None


@dataclass(frozen=True)
class RunReport:
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


def _gh_open_prs(root):
    """The open-PR listing for the repository at `root`: gh's, of origin's
    repository, both asked afresh on every call (module docstring, "Which
    repository gh is asked about")."""

    def listing():
        return github.open_sweep_prs(root, gh_repo=publish.origin_gh_repo(root))

    return listing


def _gh_labels(root):
    """The label check's callable for the repository at `root`: gh's labels
    of origin's repository, or of the repository gh resolves itself when
    origin names none (module docstring, "Which repository gh is asked
    about")."""

    def labels():
        try:
            gh_repo = publish.origin_gh_repo(root)
        except publish.PublishRefused:
            gh_repo = None
        return github.repo_labels(root, gh_repo=gh_repo)

    return labels


JOBS = {
    "brief": Job(body=_brief, opens_prs=False),
    "sweep": Job(body=run_sweep, opens_prs=True, open_pr_listing_for=_gh_open_prs),
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
    """Run the job registered as `name` against `repo` and return a RunReport.

    `open_pr_listing`, when given, replaces the job's registered listing
    (`Job.open_pr_listing_for`, bound to `repo`). It is ignored for a job
    that opens no PRs, which the cap does not apply to. `repo_labels`, when
    given, replaces the default label check (_gh_labels, bound to `repo`),
    which every job gets (module docstring, "gh, by default").
    `now` is run_guarded's: the run's one clock, which the sweep's body
    reads too (module docstring, "The sweep").
    """
    job = JOBS.get(name)
    if job is None:
        return RunReport(EXIT_NOT_A_JOB, _not_a_job(name))

    root = Path(repo)
    if root.is_dir() and not (root / ".curamoir").is_dir():
        # Not only "uninitialised": --repo may be a subdirectory of a
        # repository that is, or `.curamoir` may be a regular file.
        return RunReport(EXIT_UNUSABLE_ROOT,
                         f"cúramóir did not run {name!r}: there is no .curamoir/ "
                         f"directory at {root}, so there is no ledger to record the run "
                         "in. Point --repo at the repository root, or run /curamoir:init.")

    if open_pr_listing is None and job.open_pr_listing_for is not None:
        open_pr_listing = job.open_pr_listing_for(root)
    if repo_labels is None:
        repo_labels = _gh_labels(root)

    try:
        result = run_guarded(
            root, name, open_pr_listing,
            lambda run: job.body(root, run),
            now=now, repo_labels=repo_labels, opens_prs=job.opens_prs,
        )
    except (Exception, SystemExit, Terminated) as crash:
        # SystemExit too: a body calling sys.exit(0) has a `failure` line
        # from JobRun, and exiting 0 would contradict it.
        #
        # Terminated reaches here only when the handler that was there before
        # the run (an embedding program's) returned instead of ending the
        # process (curamoir.termination): that program decides what a signal
        # means, so this is EXIT_FAILURE with a sentence, as for any run
        # stopped part-way, not a traceback.
        #
        # KeyboardInterrupt is left to propagate (JobRun has recorded it, and
        # run_guarded has released the slot): an operator's ^C stops the
        # runner.
        #
        # This can fire before JobRun has entered (the clock raised, say),
        # so whether a ledger line was written is not ours to claim: a body
        # that raised has one from JobRun, an earlier raise has none. Either
        # way a sentence, not a traceback a slash command would relay as is.
        return RunReport(EXIT_FAILURE, f"cúramóir did not complete {name!r}: "
                                       f"{_sentence(f'{type(crash).__name__}: {crash}')}")

    if result.outcome == UNUSABLE_ROOT:
        sentence = (f"cúramóir did not run {name!r}: unusable root (the reason is "
                    "on stderr), so no ledger line was written.")
    else:
        reason = f" ({result.reason})" if result.reason is not None else ""
        sentence = f"cúramóir ran {name!r}: {result.outcome}{reason}."
        if result.message.strip():
            sentence += f" {_sentence(result.message)}"
    return RunReport(_EXIT_CODES[result.outcome], sentence)


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
