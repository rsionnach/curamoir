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

cm-fkd.4 supplies the sweep's BODY, registered in `runner.JOBS`:
`curamoir run sweep` reaches `run_guarded` through the runner, and the body
covers everything from `prepare_sandbox` on (fix_scope's "What the runner
(cm-fkd.4) must do").

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
  `repo_labels` is given. The label needs a `gh` call, which is cm-fkd.4's
  to supply alongside the open-PR listing (guards.kill_switch's docstring:
  a callable that raises degrades the label check, it does not pause).
- `LockSlot`: the lock's place in the order, after the kill switch (a
  paused repo never takes a lock it will not use) and before the budget.
  Today it is CHECK ONLY: it runs `SupervisorLock`, a live or stale lock refuses,
  and nothing acquires or releases one. `run_guarded` calls its `release`
  after the job line is written, on every exit path, so cm-x5a.6 (E0-R2's
  "maintain" half) fills in `LockSlot` and nothing else. Until it lands,
  the check sees only locks a human or a test wrote.
- `Budget`: month-to-date spend plus `budget.per_run_ceiling_usd`, the run's
  estimate, against `budget.monthly_ceiling_usd`. WHY the per-run ceiling and
  not 0: with no estimate, a ledger at 95% of the ceiling is still under it,
  and the next run could take the month past it. The per-run ceiling is the
  most a run is allowed to cost, so it is the honest worst case.
- `OpenPrCap`: `sweep.max_open_prs` open PRs under `guardrails.branch_prefix`,
  for a job that opens PRs (`opens_prs`, true unless the caller says
  otherwise) and for no other: the cap rations PRs, and refusing the brief
  because three sweep PRs are open would withhold the one artefact that
  tells the developer so. The listing callable is a parameter: cm-fkd.4
  supplies the `gh`-backed one it shares with idempotence.sweep_decision
  (guards.pr_cap's docstring holds the contract).

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
branch. cm-fkd.4 applies `DefaultBranchGuard` at push/PR time, against that
branch, which is where E0-R1's write happens.

**Check, then act.** Until cm-x5a.6 makes `LockSlot` acquire the lock before these checks
and holds it until the body returns, the cap and the budget are
check-then-act: two sweeps started together each see the same open PRs and
the same spend, and each can overshoot the cap or the ceiling by one run.
"""

# Unsorted on purpose, so I001 is silenced for this block: `curamoir.guards`
# must load before `curamoir.budget`, and through the package's front door.
# `curamoir.budget` imports `curamoir.guards.context`, which runs
# `guards/__init__`, which imports `Budget` back from the half-loaded
# `curamoir.budget` (cm-g3c). Sorted, this module cannot be imported first.
import sys  # noqa: I001
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

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
from curamoir.budget import PriceTable
from curamoir.config import ConfigError
from curamoir.job_run import REFUSED, JobRun
from curamoir.ledger import repo_ledger
from curamoir.repo_config import strict_config

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


class LockSlot:
    """The supervisor lock's slot in E10-R1's order: acquired (as a guard,
    after the kill switch and before the budget) and released (after the
    job line, on every exit path).

    CHECK ONLY until cm-x5a.6: calling it runs `SupervisorLock`, so a live
    or stale lock refuses, and `release` has nothing to release. cm-x5a.6
    makes the call write the lock and `release` remove the lock this slot
    wrote, and no other: `release` runs even when the slot was never
    called (a paused repo) or refused (someone else's lock).
    """

    def __init__(self):
        self._check = SupervisorLock()

    def __call__(self, context):
        return self._check(context)

    def release(self):
        """Nothing to release: this slot has never acquired (cm-x5a.6)."""


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
    required: the caller must release the slot the gateway acquires (once
    cm-x5a.6 makes it acquire), so the gateway never makes one of its own
    that nobody would release.
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

    A repo root that cannot be inspected stops the job here rather than
    running it (outcome UNUSABLE_ROOT): every repo-reading guard would only
    degrade, and the job would then start with the kill switch, the lock and
    the ceiling all unchecked. `JobContext`'s docstring makes this the
    caller's duty. There is no ledger to write to without a root (and
    writing one would recreate a moved repository's directory), so it is
    said on stderr.
    """
    context = JobContext(job=job, repo=None if repo is None else Path(repo))
    unusable = unusable_repo(context)
    if unusable is not None:
        print(f"cúramóir did not run {job!r}: {unusable}, so its guards cannot look "
              "and there is no ledger to record it in.", file=sys.stderr)
        return GuardedRun(outcome=UNUSABLE_ROOT)

    lock = LockSlot()
    try:
        with JobRun(repo_ledger(context.repo), job, now=now) as run:
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
                # Decided by whether the body ran, not by the type of what came
                # back: a body may return anything, including a Refusal of its own.
                run.refused(performed)
    finally:
        # After the JobRun block, so the line is written before the lock is
        # let go: a crash between the two then leaves a held lock beside a
        # written line, never a free repository with the run unrecorded.
        # Not a guarantee the line is on disk: a ledger that cannot be
        # written does not fail the job (E0-R9, JobRun._write reports it on
        # stderr), and the release still runs. cm-x5a.6 must not read the
        # release as proof of a line.
        lock.release()
    # Read after the block: a body that reported nothing is UNKNOWN only
    # once JobRun has exited.
    return GuardedRun(
        outcome=run.outcome,
        reason=run.reason if run.outcome == REFUSED else None,
        value=performed if body_ran else None,
        message=run.message,
    )
