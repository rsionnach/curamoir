"""E10-R1's guarded run for every job; E3-R6 (the sweep MUST respect the
open-PR cap, E0-R6, and the budget, E0-R4) is the sweep's part of it.

The enforcement is the guard layer's, not this module's. This is the wiring:
`job_gateway` assembles the guards a job must pass before it does anything,
from the repository's configuration, and `run_guarded` runs the job's whole
body behind them inside a `JobRun`, so a refusal is the job's one `job` line
naming the reason (`pr_cap`, `budget`, ...) and the body never starts. "Before
any work" is therefore structural rather than a promise: the body is what
makes the sandbox clone, runs the fix step and spends tokens, and a refused
body is never called.

The sweep's BODY is curamoir.sweep_job.run_sweep (cm-fkd.4), registered
in `runner.JOBS`: `curamoir run sweep` reaches `run_guarded` through the
runner, and the body covers everything from `prepare_sandbox` on
(fix_scope's "What the runner does").

**One repository, one ledger.** `run_guarded` takes the repo and nothing
else that locates state: the job line, the gateway's guard lines and the
budget's read all use the ledger at `ledger.LEDGER_PATH` under that root.
WHY not a ledger parameter: the budget reads the repo's own ledger, so a job
line written anywhere else is spend the next run's ceiling never sees.

**E10-R1's order**, which `run_guarded` performs for every job: kill switch,
lock slot, budget, (the open-PR cap, for a job that opens PRs), sync seam,
the job, the job's ledger line, lock-release slot. `curamoir run <job>`
(curamoir.runner) is the one caller that schedules jobs, so this order is
what every scheduled run gets.

**Which guards, in that order:**

- `KillSwitch`: the PAUSE file, and the `curamoir:paused` label when
  `repo_labels` is given. The label needs a `gh` call:
  curamoir.github.repo_labels, which `runner.run_job` gives every job
  (guards.kill_switch's docstring: a callable that raises degrades the
  label check, it does not pause).
- `LockSlot`: E0-R2's supervisor lock, acquired after the kill switch (a
  paused repo never takes a lock it will not use) and before the budget.
  It runs `SupervisorLock` (a live, stale or unreadable lock refuses, and a
  stale one is reported, never overridden), then writes
  `.curamoir/state/lock` with `guards.lock.create_lock`, whose atomic
  create refuses the loser of two runs that both found the repository free.
  `run_guarded` releases it after the job line is written, on every exit
  path (below, "Released on every exit path").
- `Budget`: month-to-date spend plus `budget.per_run_ceiling_usd`, the run's
  estimate, against `budget.monthly_ceiling_usd`. WHY the per-run ceiling and
  not 0: with no estimate, a ledger at 95% of the ceiling is still under it,
  and the next run could take the month past it. The per-run ceiling is the
  most a run is allowed to cost, so it is the honest worst case.
- `OpenPrCap`: `sweep.max_open_prs` open PRs under `guardrails.branch_prefix`,
  for a job that opens PRs (`opens_prs`, true unless the caller says
  otherwise) and for no other: the cap rations PRs, and refusing the brief
  because three sweep PRs are open would withhold the one artefact that
  tells the developer so. The listing callable is a parameter: the runner
  passes the `gh`-backed one, curamoir.github.open_sweep_prs, which the
  sweep shares with idempotence.sweep_decision (guards.pr_cap's docstring
  holds the contract).

**The sync seam.** E10-R1 puts sync after the budget and before the job.
Sync is cm-eqp.3's and is not built, so `skip_sync` stands in its place and
says so on the job line (`sync: skipped`, with `sync_reason`): a ledger
reader must never take a job that ran on an unsynced checkout for one that
ran on a synced one. A run refused before the seam carries neither field.

**The config is read strictly** (`repo_config.strict_config`). No
`curamoir.yaml` means the defaults, the normal case. One that exists but
cannot be loaded (invalid, unreadable, not a regular file) REFUSES, with
`guard_error` and the problems named, in place of the budget and the cap.
WHY not `repo_config.effective_config`, which falls back to the defaults:
a `monthly_ceiling_usd: 5` beside an unrelated typo would then run under
the default $15 ceiling, a guard loosened without anybody being told
(lint's `_configured_threshold` refuses the same thing).

**Not `DefaultBranchGuard`.** It inspects the CURRENT branch of
`context.repo`, and the sweep starts from the developer's checkout, which is
usually on the default branch: it would refuse every sweep. The sweep never
writes there; it edits a disposable clone and pushes a `curamoir/sweep-...`
branch. The sweep applies `DefaultBranchGuard` at push/PR time, against that
branch (its `target_branch`, in curamoir.publish), which is where E0-R1's
write happens.

**Held across check and act.** The lock is taken before the budget and the
cap and held until the body has returned and the job line is written, so
those two checks and the work they admit happen under one lock: two sweeps
started together cannot both see the same open PRs or the same spend. The
run's own lock is never checked again after it is taken, so it cannot
refuse its own run.

**Released on every exit path.** Success, a refusal by any later guard,
and a body that raises (SystemExit and KeyboardInterrupt included) all
reach `run_guarded`'s `finally`. So do SIGTERM and SIGHUP:
`curamoir.termination` makes them raise `Terminated` during the run. It
also defers them, and SIGINT (^C), while the line is written and the lock
released, then sends the first again so the process still dies by it.
SIGKILL and a power cut run no code. The lock then stays, and E0-R2's
liveness check is the backstop: the lock's pid and host let the next run
judge it stale, and it is reported, not overridden.

**CURAMOIR_JOB for the run (cm-a2s).** The E0-R5 denylist hook knows a job
by this variable, falling back to the lock (hooks/_entry.in_curamoir_job).
The lock is gitignored, so a sandbox clone has none, and there the variable
is the only signal. `run_guarded` is the one place every job passes, so it
sets `CURAMOIR_JOB=<job>` in this process's environment for the run.

It puts back what was there before, a previous value or nothing, on every
exit path the lock is released on, after the release (`run_guarded`'s
Ordering 5a and 5b).

Every process the job starts inherits it: the fix step's `claude -p` gets
git_guard.scrubbed_environment, which drops only GIT_*, and Claude Code
passes its environment on to the hooks it runs ("A hook process inherits
the parent environment", https://code.claude.com/docs/en/hooks). The fix
step's own run is `--restricted`, though, and loads no cúramóir hook today
(cm-60c): there the variable is carried for when the hook is loaded.
CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1 does not take it away: it removes
"credentials", recognised "by its variable
name or by the shape of its value" (https://code.claude.com/docs/en/env-vars,
"What the subprocess environment scrub removes"), and a job's name is
neither. That is the docs' word; no test here runs a real `claude`. WHY the
process environment and not each spawner's: a later spawner cannot forget
it, and this CLI runs one job per process (a nested run restores the outer
job's name).
"""

# Unsorted on purpose, so I001 is silenced for this block: `curamoir.guards`
# must load before `curamoir.budget`, and through the package's front door.
# `curamoir.budget` imports `curamoir.guards.context`, which runs
# `guards/__init__`, which imports `Budget` back from the half-loaded
# `curamoir.budget` (cm-g3c). Sorted, this module cannot be imported first.
import os  # noqa: I001
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple, Optional

from curamoir.guards import (
    Budget,
    Gateway,
    JobContext,
    KillSwitch,
    OpenPrCap,
    Reason,
    Refusal,
    SupervisorLock,
    unusable_repo,
)
from curamoir.guards.lock import create_lock, forget_lock, lock_bytes, lock_file
from curamoir.budget import PriceTable
from curamoir.config import ConfigError
from curamoir.job_run import REFUSED, JobRun
from curamoir.ledger import repo_ledger
from curamoir.repo_config import strict_config
# `Terminated as Terminated`: re-exported, for the runner to catch from here.
from curamoir.termination import HoldOnExit, TerminationSignals
from curamoir.termination import Terminated as Terminated

UNUSABLE_ROOT = "unusable_root"


@dataclass(frozen=True)
class GuardedRun:
    """What `run_guarded` did, for the runner to map to an exit code.

    `outcome` is the job line's outcome as JobRun recorded it
    (job_run.SUCCESS, REFUSED, FAILURE or UNKNOWN), or UNUSABLE_ROOT when
    the repo root could not be inspected, the job did not run and no line
    was written. `reason` is the refusal's Reason when `outcome` is
    REFUSED, whether a guard refused or the body did, else None. `value`
    is what the body returned, None when it never ran: a Refusal there is
    the body's own, never a guard's. `message` is the job line's message.
    """

    outcome: str
    reason: Optional[Reason] = None
    value: Any = None
    #: For the runner's one-sentence report; "" with UNUSABLE_ROOT.
    message: str = ""


class _Held(NamedTuple):
    """What a LockSlot is holding, or was about to: the lock's path, the
    bytes it writes there, whether create_lock has returned, and the pid
    that took it (a forked child is not the holder)."""

    path: Path
    written: bytes
    created: bool
    pid: int


class LockSlot:
    """The supervisor lock's slot in E10-R1's order: acquired (as a guard,
    after the kill switch and before the budget) and released (after the
    job line, on every exit path).

    Calling it runs `SupervisorLock`, so a live, stale or unreadable lock
    refuses with `locked`, and otherwise takes the lock with
    `guards.lock.create_lock`. A create that finds a lock after all (another
    run took it between the check and the create) refuses `locked` too, as
    does a lock that cannot be written: a job does not run unlocked. A slot
    interrupted mid-create (a signal raising Terminated) still knows the
    bytes it was writing, so `release` removes the file if it got that far.

    `release` removes the lock this slot wrote, and no other: it runs even
    when the slot was never called (a paused repo) or refused (someone
    else's lock), and then does nothing; and it leaves a lock that is no
    longer the one it wrote (deleted by hand and taken by another run).
    It never raises, so it cannot mask the body's own exception in
    `run_guarded`'s `finally`: a release that fails says so on stderr. In
    a forked child of the run it does nothing: the lock is the parent's.
    """

    def __init__(self):
        self._check = SupervisorLock()
        #: A _Held from just before the create until release: set BEFORE the
        #: file can exist, so a signal at any point after the create still
        #: leaves the slot knowing what to release.
        self._held = None

    def __call__(self, context):
        verdict = self._check(context)
        if verdict is not None:
            return verdict
        path = lock_file(context.repo)
        written = lock_bytes(context.job)
        self._held = _Held(path, written, created=False, pid=os.getpid())
        try:
            create_lock(context.repo, written)
        except FileExistsError:
            self._held = None
            holder = self._check(context)
            if holder is None:
                # The create found a file the check could not see or read as
                # a lock: not a race, and it will not go away by itself.
                return Refusal(
                    reason=Reason.LOCKED,
                    message=(f"a file at {path} is not a readable lock (a symlink?); "
                             "delete it if no cúramóir job is running."),
                )
            return Refusal(
                reason=Reason.LOCKED,
                message=("another cúramóir job took the lock as this one was taking it: "
                         f"{holder.message}"),
            )
        except OSError as error:
            self._held = None
            return Refusal(
                reason=Reason.LOCKED,
                message=(f"could not write the supervisor lock at {path} ({error}); "
                         "refusing rather than running unlocked."),
            )
        self._held = _Held(path, written, created=True, pid=os.getpid())
        return None

    def release(self):
        """Remove the lock this slot wrote, if it is still that lock."""
        held, self._held = self._held, None
        if held is None:
            return
        path, written, created, pid = held
        if pid != os.getpid():
            # A forked child unwinding through run_guarded: the lock is its
            # parent's, which is still running and will release it.
            return
        forget_lock(written)
        try:
            try:
                current = path.read_bytes()
            except FileNotFoundError:
                if not created:
                    # Interrupted before the create finished: there may never
                    # have been a file, and if there was, create_lock removed it.
                    return
                _warn(f"the supervisor lock at {path} was already gone when this run "
                      "finished; something else removed it.")
                return
            if current != written:
                if not created:
                    # Interrupted before this slot's create: the lock there is
                    # another run's, and this slot has nothing to say about it.
                    return
                _warn(f"left {path} in place: it is not the lock this run wrote, so "
                      "another run may hold it.")
                return
            # Between the read and the unlink another process could replace the
            # file only by deleting this run's live lock first, by hand.
            path.unlink(missing_ok=True)
        except Exception as failure:
            # `except Exception`: release runs in a `finally`, where any raise
            # would replace the body's own exception.
            _warn(f"could not release the supervisor lock at {path} ({failure}). "
                  "Delete it once no cúramóir job is running; until then every job "
                  "refuses with `locked`.")


def _warn(message):
    """A lock problem nobody else will report: stderr, as JobRun's ledger
    failures are, because the job line is already written."""
    print(f"cúramóir: {message}", file=sys.stderr)


#: The E0-R5 denylist's primary signal (hooks/_entry.in_curamoir_job):
#: `run_guarded` sets it to the job's name for the run. hooks/_entry.py
#: keeps its own copy of the name (_JOB_VARIABLE), so the hook stays
#: stdlib-only.
JOB_VARIABLE = "CURAMOIR_JOB"


def _require_job_name(job):
    """Raise unless `job` can be JOB_VARIABLE's value and still mean "in a
    job" to the hook: a non-empty str with no NUL (run_guarded's docstring)."""
    if not isinstance(job, str):
        raise TypeError(f"a job's name must be a str, not {type(job).__name__}: {job!r}")
    if not job or "\0" in job:
        raise ValueError(f"a job's name must be non-empty and contain no NUL: {job!r}")


def _restore_job_variable(before):
    """Put JOB_VARIABLE back as `run_guarded` found it: `before`, or absent
    when it was None. Never raises (it runs in a `finally`)."""
    if before is None:
        os.environ.pop(JOB_VARIABLE, None)
        # A signal inside the set can land after its putenv() and before
        # os.environ records the key; pop() then never calls unsetenv(), and
        # a child started with env=None would still inherit the job's name.
        os.unsetenv(JOB_VARIABLE)
    else:
        os.environ[JOB_VARIABLE] = before


#: What `skip_sync` writes on the job line, in the line's own fields.
SYNC_SKIPPED = "skipped"
SYNC_NOT_BUILT = ("sync is not built yet (cm-eqp.3), so the job ran on the "
                  "checkout as it stood, unsynced")


def skip_sync(run):
    """E10-R1's sync step, which cm-eqp.3 will build: until then, a skip
    that the job line states."""
    run.note(sync=SYNC_SKIPPED, sync_reason=SYNC_NOT_BUILT)


def job_gateway(repo, open_pr_listing, *, lock, now=None, repo_labels=None,
                opens_prs=True):
    """The gateway a job passes before it starts, built from `repo`'s config.

    `repo` must be an inspectable root (run_guarded checks it first). Guard
    lines go to the repo's own ledger. `open_pr_listing` is the open-PR
    callable (guards.pr_cap's contract), read only when `opens_prs`: a job
    that opens no PRs is not subject to the cap and may pass None. `now` is
    the budget's clock, injectable so month attribution is testable.
    `repo_labels` is the kill switch's label callable (guards.kill_switch);
    None checks the PAUSE file alone. `lock` is the run's LockSlot, and is
    required: the caller must release the slot the gateway acquires, so the
    gateway never makes one of its own that nobody would release.
    """
    kill_switch_and_lock = [KillSwitch(repo_labels), lock]
    try:
        config = strict_config(repo)
    except ConfigError as invalid:
        return Gateway(
            guards=[*kill_switch_and_lock,
                    _refuse_invalid_config(invalid, opens_prs=opens_prs)],
            ledger=repo_ledger(repo),
        )
    budget = config["budget"]
    guards = [
        *kill_switch_and_lock,
        Budget(
            prices=PriceTable(budget["prices"]),
            monthly_ceiling_usd=budget["monthly_ceiling_usd"],
            per_run_estimate_usd=budget["per_run_ceiling_usd"],
            on_ceiling=budget["on_ceiling"],
            now=now,
        ),
    ]
    if opens_prs:
        guards.append(OpenPrCap(
            open_pr_listing,
            max_open_prs=config["sweep"]["max_open_prs"],
            branch_prefix=config["guardrails"]["branch_prefix"],
        ))
    return Gateway(guards=guards, ledger=repo_ledger(repo))


#: The most config problems one guard_error message names. The message
#: lands on a ledger line and in a terminal, and a config that is wrong in
#: hundreds of places must not make either unreadable; /curamoir:doctor
#: lists them all.
MAX_PROBLEMS_SHOWN = 20


def _refuse_invalid_config(invalid, opens_prs=True):
    shown = list(invalid.problems[:MAX_PROBLEMS_SHOWN])
    hidden = len(invalid.problems) - len(shown)
    problems = "; ".join(shown) + (f" (+{hidden} more)" if hidden > 0 else "")
    problems = problems or "the config is invalid"
    # The cap only for a job it applies to: the brief is not refused for an
    # open-PR cap it is exempt from.
    configured = "The budget and the open-PR cap cannot" if opens_prs else "The budget cannot"

    def invalid_config(_context):
        return Refusal(
            reason=Reason.GUARD_ERROR,
            message=(
                f"{problems}. {configured} be configured from it, "
                "so the job does not run. Fix it, or run /curamoir:doctor."
            ),
        )

    return invalid_config


def run_guarded(repo, job, open_pr_listing, body, *, now=None, repo_labels=None,
                opens_prs=True):
    """Run `body(run)` for `job` behind `job_gateway(repo, ...)`, inside one
    `JobRun` writing to the repo's own ledger, and return a GuardedRun.

    The steps are E10-R1's, in its order (the module docstring): the
    gateway's guards, the sync seam, `body`, the job line as the JobRun
    exits, and the lock slot's release last, whether the run succeeded,
    was refused or raised. `opens_prs` is job_gateway's: False exempts the
    job from the open-PR cap, and `open_pr_listing` may then be None.

    `body` receives the `JobRun`, reports its own outcome on it, and reads
    the ledger's history through `run.ledger`. When a guard refuses, `body`
    is never called and the job line is `refused` with the guard's reason.
    `now` is the run's one clock: the budget's month and the job line's
    `started` and `ended` (the ledger's own `at` stays the wall clock). It
    must return an aware UTC datetime and must not raise: a raise as the
    run starts writes no job line, and a naive value is not checked.
    `repo_labels` is passed to the kill switch (job_gateway).

    `job` must be a non-empty str with no NUL: it becomes CURAMOIR_JOB, and
    the denylist hook takes an empty value for "not in a job" while
    os.environ refuses a NUL. Anything else is a caller's error (runner.JOBS
    cannot produce one): TypeError or ValueError, raised before anything
    runs, so no signal handler, lock, variable or job line is touched.

    A repo root that cannot be inspected stops the job here rather than
    running it (outcome UNUSABLE_ROOT): every repo-reading guard would only
    degrade, and the job would then start with the kill switch, the lock and
    the ceiling all unchecked. `JobContext`'s docstring makes this the
    caller's duty. There is no ledger to write to without a root (and
    writing one would recreate a moved repository's directory), so it is
    said on stderr.
    """
    _require_job_name(job)
    context = JobContext(job=job, repo=None if repo is None else Path(repo))
    unusable = unusable_repo(context)
    if unusable is not None:
        print(f"cúramóir did not run {job!r}: {unusable}, so its guards cannot look "
              "and there is no ledger to record it in.", file=sys.stderr)
        return GuardedRun(outcome=UNUSABLE_ROOT)

    # Ordering (do not reorder):
    # 1. TerminationSignals is outermost, so the replaced handlers come back,
    #    and a pending signal is sent again, only after the release.
    # 2. HoldOnExit is listed after JobRun in one `with`, so it exits first:
    #    the held phase starts before JobRun's exit writes the job line.
    # 3. The release is in the `finally` outside the JobRun block, so the
    #    line is written before the lock goes.
    # 4. hold() and release() are a nested try/finally, so a signal landing
    #    before hold() takes effect still reaches the release.
    # 5a. CURAMOIR_JOB (JOB_VARIABLE) is set as the first statement of the
    #     `try`, before the guards: their callables (the kill switch's label
    #     lookup, the open-PR listing) are the first things that can start a
    #     process, and every `claude -p` comes after them. Its previous value
    #     is read BEFORE the `try`, so a signal landing at any point after
    #     leaves the finally something true to put back.
    # 5b. It is restored in a `finally` nested inside the release's, so after
    #     the lock is gone and still in the held phase: for the whole run the
    #     denylist hook sees at least one of its two signals
    #     (hooks/_entry.in_curamoir_job), and a signal cannot cut the restore
    #     short.
    lock = LockSlot()
    # Ordering 5a.
    before = os.environ.get(JOB_VARIABLE)
    with TerminationSignals() as signals:
        try:
            # Ordering 5a.
            os.environ[JOB_VARIABLE] = job
            # HoldOnExit AFTER JobRun: it exits first (Ordering 2).
            with JobRun(repo_ledger(context.repo), job, now=now) as run, \
                    HoldOnExit(signals):
                gateway = job_gateway(context.repo, open_pr_listing, now=now,
                                      repo_labels=repo_labels, lock=lock,
                                      opens_prs=opens_prs)
                body_ran = False

                def action():
                    nonlocal body_ran
                    # cm-eqp.3 replaces skip_sync with the real sync at this one
                    # call site; tests patch the module global to observe the step.
                    skip_sync(run)
                    body_ran = True
                    return body(run)

                performed = gateway.perform(context, action)
                if not body_ran:
                    # Decided by whether the body ran, not by the type of what
                    # came back: a body may return anything, including a
                    # Refusal of its own.
                    run.refused(performed)
        finally:
            # Ordering 3: a crash between line and release leaves a held lock
            # beside a written line, never a free repository with the run
            # unrecorded. The release is no proof of a line: a ledger that
            # cannot be written does not fail the job (E0-R9, JobRun._write
            # reports it on stderr), and the release still runs.
            try:
                # Again (idempotent), for an exit that never reached HoldOnExit.
                signals.hold()
            finally:
                # Ordering 4.
                try:
                    lock.release()
                finally:
                    # Ordering 5b.
                    _restore_job_variable(before)
    # Read after the block: a body that reported nothing is UNKNOWN only
    # once JobRun has exited.
    return GuardedRun(
        outcome=run.outcome,
        reason=run.reason if run.outcome == REFUSED else None,
        value=performed if body_ran else None,
        message=run.message,
    )
