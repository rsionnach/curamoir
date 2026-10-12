"""E3-R6: the sweep MUST respect the open-PR cap (E0-R6) and the budget (E0-R4).

The enforcement is the guard layer's, not this module's. This is the wiring:
`job_gateway` assembles the guards a job must pass before it does anything,
from the repository's configuration, and `run_guarded` runs the job's whole
body behind them inside a `JobRun`, so a refusal is the job's one `job` line
naming the reason (`pr_cap`, `budget`, ...) and the body never starts. "Before
any work" is therefore structural rather than a promise: the body is what
makes the sandbox clone, runs the fix step and spends tokens, and a refused
body is never called.

cm-fkd.4's runner calls this as step 0 of the pipeline (fix_scope's "What the
runner (cm-fkd.4) must do"), wrapping everything from `prepare_sandbox` on.

**One repository, one ledger.** `run_guarded` takes the repo and nothing
else that locates state: the job line, the gateway's guard lines and the
budget's read all use the ledger at `ledger.LEDGER_PATH` under that root.
WHY not a ledger parameter: the budget reads the repo's own ledger, so a job
line written anywhere else is spend the next run's ceiling never sees.

**Which guards, in E10-R1's order** (kill switch, lock, budget), then the cap:

- `KillSwitch`: the PAUSE file only. The `curamoir:paused` label needs a
  `gh` call, which is cm-fkd.4's to supply alongside the open-PR listing.
- `SupervisorLock`: CHECK ONLY. A live lock refuses; nothing here acquires or
  releases one. Acquisition and release belong to cm-x5a.6 (E0-R2's
  "maintain" half), and until it lands this guard sees only locks a human
  or a test wrote.
- `Budget`: month-to-date spend plus `budget.per_run_ceiling_usd`, the run's
  estimate, against `budget.monthly_ceiling_usd`. WHY the per-run ceiling and
  not 0: with no estimate, a ledger at 95% of the ceiling is still under it,
  and the next run could take the month past it. The per-run ceiling is the
  most a run is allowed to cost, so it is the honest worst case.
- `OpenPrCap`: `sweep.max_open_prs` open PRs under `guardrails.branch_prefix`.
  The listing callable is a parameter: cm-fkd.4 supplies the `gh`-backed one
  it shares with idempotence.sweep_decision (guards.pr_cap's docstring holds
  the contract).

**The config is read strictly.** No `curamoir.yaml` means the defaults, the
normal case. One that exists but cannot be loaded (invalid, unreadable, not
a file) REFUSES, with `guard_error` and the problems named, in place of the
budget and the cap. WHY not `repo_config.effective_config`, which falls back
to the defaults: a `monthly_ceiling_usd: 5` beside an unrelated typo would
then run under the default $15 ceiling, a guard loosened without anybody
being told (lint's `_configured_threshold` refuses the same thing).

**Not `DefaultBranchGuard`.** It inspects the CURRENT branch of
`context.repo`, and the sweep starts from the developer's checkout, which is
usually on the default branch: it would refuse every sweep. The sweep never
writes there; it edits a disposable clone and pushes a `curamoir/sweep-...`
branch. cm-fkd.4 applies `DefaultBranchGuard` at push/PR time, against that
branch, which is where E0-R1's write happens.

**Check, then act.** Until cm-x5a.6 acquires the lock before these checks
and holds it until the body returns, the cap and the budget are
check-then-act: two sweeps started together each see the same open PRs and
the same spend, and each can overshoot the cap or the ceiling by one run.
"""

# Unsorted on purpose, so I001 is silenced for this block: `curamoir.guards`
# must load before `curamoir.budget`, and through the package's front door.
# `curamoir.budget` imports `curamoir.guards.context`, which runs
# `guards/__init__`, which imports `Budget` back from the half-loaded
# `curamoir.budget` (cm-g3c). Sorted, this module cannot be imported first.
import os  # noqa: I001
import sys
from copy import deepcopy
from pathlib import Path

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
from curamoir.config import DEFAULTS, ConfigError, load_config_text
from curamoir.job_run import JobRun
from curamoir.ledger import repo_ledger

CONFIG_PATH = (".curamoir", "curamoir.yaml")


def job_gateway(repo, open_pr_listing, *, now=None):
    """The gateway a job passes before it starts, built from `repo`'s config.

    `repo` must be an inspectable root (run_guarded checks it first). Guard
    lines go to the repo's own ledger. `open_pr_listing` is the open-PR
    callable (guards.pr_cap's contract). `now` is the budget's clock,
    injectable so month attribution is testable.
    """
    kill_switch_and_lock = [KillSwitch(), SupervisorLock()]
    try:
        config = strict_config(repo)
    except ConfigError as invalid:
        return Gateway(
            guards=[*kill_switch_and_lock, _refuse_invalid_config(invalid)],
            ledger=repo_ledger(repo),
        )
    budget = config["budget"]
    return Gateway(
        guards=[
            *kill_switch_and_lock,
            Budget(
                prices=PriceTable(budget["prices"]),
                monthly_ceiling_usd=budget["monthly_ceiling_usd"],
                per_run_estimate_usd=budget["per_run_ceiling_usd"],
                on_ceiling=budget["on_ceiling"],
                now=now,
            ),
            OpenPrCap(
                open_pr_listing,
                max_open_prs=config["sweep"]["max_open_prs"],
                branch_prefix=config["guardrails"]["branch_prefix"],
            ),
        ],
        ledger=repo_ledger(repo),
    )


def strict_config(repo):
    """`repo`'s config merged over the defaults; the defaults when there is
    no config file. ConfigError when there is one that cannot be loaded."""
    path = Path(repo).joinpath(*CONFIG_PATH)
    if not os.path.lexists(path):
        return deepcopy(DEFAULTS)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as unreadable:
        # A directory, a dangling symlink, bytes that are not UTF-8: something
        # is there, and whatever limits it was meant to set cannot be read.
        raise ConfigError([f"{path} cannot be read: {unreadable}"]) from None
    try:
        return load_config_text(text)
    except ConfigError:
        raise
    except Exception as error:  # a YAML syntax error is PyYAML's own type
        # Caught here so it reaches the gateway as a ConfigError, refused
        # after the kill switch and the lock like any other invalid config,
        # rather than escaping run_guarded as a failure with no reason.
        raise ConfigError([f"{path}: not valid YAML: {error}"]) from None


def _refuse_invalid_config(invalid):
    def invalid_config(_context):
        return Refusal(
            reason=Reason.GUARD_ERROR,
            message=(
                "invalid curamoir.yaml: " + "; ".join(invalid.problems)
                + ". The budget and the open-PR cap cannot be configured from it, "
                "so the job does not run. Fix it, or run /curamoir:doctor."
            ),
        )

    return invalid_config


def run_guarded(repo, job, open_pr_listing, body, *, now=None):
    """Run `body(run)` for `job` behind `job_gateway(repo, ...)`, inside one
    `JobRun` writing to the repo's own ledger.

    `body` receives the `JobRun` and reports its own outcome on it. When a
    guard refuses, `body` is never called and the job line is `refused` with
    the guard's reason. Returns the refusal, whatever `body` returned, or
    None when the repo root cannot be inspected. A body may return None too,
    so a None return does not say the root was unusable; the stderr line
    below is the signal.

    A repo root that cannot be inspected stops the job here rather than
    running it: every repo-reading guard would only degrade, and the job
    would then start with the kill switch, the lock and the ceiling all
    unchecked. `JobContext`'s docstring makes this the caller's duty. There
    is no ledger to write to without a root (and writing one would recreate
    a moved repository's directory), so it is said on stderr.
    """
    context = JobContext(job=job, repo=None if repo is None else Path(repo))
    unusable = unusable_repo(context)
    if unusable is not None:
        print(f"cúramóir did not run {job!r}: {unusable}, so its guards cannot look "
              "and there is no ledger to record it in.", file=sys.stderr)
        return None

    with JobRun(repo_ledger(context.repo), job) as run:
        gateway = job_gateway(context.repo, open_pr_listing, now=now)
        body_ran = False

        def action():
            nonlocal body_ran
            body_ran = True
            return body(run)

        outcome = gateway.perform(context, action)
        if not body_ran:
            # Decided by whether the body ran, not by the type of what came
            # back: a body may return anything, including a Refusal of its own.
            run.refused(outcome)
        return outcome
