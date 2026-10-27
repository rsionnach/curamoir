"""The sweep's body, first half (cm-fkd.13, cm-fkd.4's ST4): fix_scope's
runner steps 1, 2, 2a, 2b and 2c, "The tier1 field" and "The line's cost".

`run_sweep(repo, run, tools)` is the body `runner.JOBS["sweep"]` will run
behind the guards (fix_scope step 0). It is NOT registered yet: cm-fkd.14
(ST5) adds steps 3 to 10 and registers it. Until then a sweep that gets
past step 2c records a failure saying the fix step is not wired, never a
success, so nothing reads as swept.

**The base** (user decision, 2026-10-23): the default branch's
remote-tracking ref, `refs/remotes/origin/<default>`, as last fetched
(nothing is fetched here), refused when absent, before any sandbox. It is
never the checkout's HEAD or its local default branch, so a sweep never
carries unpushed work.

**Which default branch.** The one publish opens the PR into: GitHub's
answer, never a local guess. resolve_sweep_base, once per sweep and
before the sandbox, takes `gh_repo = publish.origin_gh_repo(repo)` and
then `publish.origin_default_branch(repo, gh_repo)`, which asks `gh repo
view <gh_repo> --json defaultBranchRef` and requires the checkout to
agree (origin/HEAD, when set, names that branch, and
`refs/remotes/origin/<default>` exists). publish.pr_base calls the same
function when it opens the PR, so the branch the sweep builds on and the
branch its PR opens into are named by one rule. The sandbox is pinned at
the commit `refs/remotes/origin/<default>` names. `gh_repo` is also
passed to the open-PR listing (`github.open_sweep_prs(repo,
gh_repo=...)`, step 2b), so the decision reads the PRs of the repository
the PR will open on. Any PublishRefused from these calls is a refusal
(Reason.SWEEP_BASE), before anything is cloned, linted or spent. (Why not
guards.default_branch.detect_default_branch, which the 2026-10-23
decision named: it falls back to local branch names, and trusts a stale
origin/HEAD; publish's rule supersedes it.)

**Refusals**, each with its Reason, and never a `found_delta` or a
snapshot (fix_scope step 2a): an unloadable `curamoir.yaml`
(GUARD_ERROR, as job_guard refuses one); no base (SWEEP_BASE); the lint's
report cannot be acted on, sweep.LintUntrusted (LINT); the sandbox cannot
be made or held to its base, git_guard.GitGuardError and its
ScopeCheckError and tier1.ScanRefused (SANDBOX); the open sweep PRs
unknown, idempotence's UNKNOWN (IDEMPOTENCE, written by Decision.record).
From step 2b(i) on (once the plan is set) every line, refusals included,
carries `**tier1_state.ledger_fields()` (fix_scope, "The tier1 field"),
except UNKNOWN's.

**The line's cost** (fix_scope's section of that name): line_cost_fields,
pure, gives a line's top-level `input_tokens`, `output_tokens`, `model`
and `cost_usd` from the calls that ran on it. They are noted before the
call and corrected after it. Immediately before the scan's `claude`
process starts (run_tier1_scan's `before_call`, after every step that can
fail without a call), `run.note` writes the cautious answer: both counts
budget.UNRECORDED with `model`, and the FAILED `tier1` field of this call
(note_scan_pending), so a run that ends during or right after the call,
however it ends (SIGTERM's Terminated, ^C, an exception inside
run_tier1_scan), still leaves a line the budget counts
as unrecorded spend and tier1.scan_attempts counts as an attempt. Once the
scan returns or is refused, note_scan replaces them with the scan's own
cost fields and `tier1` field, and drops the counts and `model`
(JobRun.unnote) for a scan that made no call (no candidates, or a
`claude` that never started).

Syntax that imports on 3.9, like sweep.py.
"""

import math
import warnings
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Optional

from curamoir import github, publish
from curamoir.budget import UNRECORDED
from curamoir.config import ConfigError
from curamoir.fix_counts import (
    found_delta,
    found_set,
    read_snapshot,
    snapshot_for,
    snapshot_path,
    write_snapshot_or_warn,
)
from curamoir.fix_scope import lint_sandbox
from curamoir.fix_step import FixStepRun
from curamoir.git_guard import GitGuardError, git, prepare_sandbox
from curamoir.guards.reasons import Reason
from curamoir.guards.result import Refusal
from curamoir.idempotence import UNKNOWN, sweep_decision
from curamoir.repo_config import config_value, strict_config
from curamoir.sweep import LintUntrusted
from curamoir.tier1 import (
    SCAN_TIMEOUT,
    ScanRefused,
    Tier1Plan,
    Tier1Scan,
    plan_scan,
    run_tier1_scan,
    with_findings,
)

#: The failure a sweep records once it gets past step 2c, until cm-fkd.14
#: wires steps 3 to 10.
NOT_WIRED = "fix step not wired (cm-fkd.4 ST5)"


def _utc_now():
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class SweepTools:
    """What run_sweep calls out to, so tests can stand in for each.

    `claude` is the `claude` executable for the model calls (fix_step's
    seam; tests pass a stand-in script). `open_sweep_prs(repo, gh_repo=...)`
    is the open-PR listing, github.open_sweep_prs by default; run_sweep
    binds it to the repository and origin's gh_repo itself (fix_scope step
    0 (iii)), so it is asked afresh, never cached. `now` is the clock, an
    aware UTC datetime; unused until cm-fkd.14 (ST5) dates the sweep branch
    with it. `lint_options` are sweep.work_list's keyword arguments
    (`lint`, `timeout`, `grace`), for lint_sandbox and, in ST5, the
    re-lint. `scan_timeout` bounds the tier-1 call (tier1.SCAN_TIMEOUT).
    `sandbox_parent` is where the sandbox is made (the system temporary
    directory when None)."""

    claude: str = "claude"
    open_sweep_prs: Callable = github.open_sweep_prs
    now: Callable[[], datetime] = _utc_now
    lint_options: dict = field(default_factory=dict)
    scan_timeout: float = SCAN_TIMEOUT
    sandbox_parent: Optional[str] = None


@dataclass(frozen=True)
class SweepBase:
    """Where a sweep starts (module docstring, "The base"): origin's GitHub
    repository `gh_repo` (HOST/OWNER/REPO), its default branch
    `default_branch` as GitHub names it, that branch's remote-tracking
    `ref`, and the commit `sha` the ref named when resolved."""

    gh_repo: str
    default_branch: str
    ref: str
    sha: str


def resolve_sweep_base(repo):
    """The SweepBase for the checkout at `repo`, resolved once, before any
    sandbox (module docstring, "Which default branch"). PublishRefused when
    origin names no GitHub repository, gh cannot say which branch is its
    default, or the checkout disagrees with it or has no remote-tracking
    ref for it. Nothing is fetched."""
    gh_repo = publish.origin_gh_repo(repo)
    default = publish.origin_default_branch(repo, gh_repo)
    ref = f"refs/remotes/origin/{default}"
    try:
        sha = git(repo, "rev-parse", "--verify", "--quiet", "--end-of-options",
                  f"{ref}^{{commit}}", ok=(0, 1, 128)).decode("ascii", "replace").strip()
    except GitGuardError as error:
        raise publish.PublishRefused(f"could not read {ref}: {error}") from None
    if not sha:
        raise publish.PublishRefused(f"{ref} names no commit: run `git fetch origin`, then "
                                     "sweep again")
    return SweepBase(gh_repo, default, ref, sha)


def run_sweep(repo, run, tools=None):
    """The sweep's body for `repo` (the user's checkout) on `run` (the
    JobRun writing its ledger line): fix_scope's steps 1 to 2c (module
    docstring). Records exactly one outcome on `run`, or raises and
    job_guard.run_guarded records the crash."""
    _Sweep(Path(repo), run, tools or SweepTools()).execute()


class _Sweep:
    """One sweep's state, so every refusal can carry `tier1_state`."""

    def __init__(self, repo, run, tools):
        self.repo = repo
        self.run = run
        self.tools = tools
        #: Set by execute before the sandbox: guardrails.branch_prefix, and
        #: the open-PR listing bound to origin's gh_repo.
        self.branch_prefix = None
        self.listing = None
        #: fix_scope's ONE object ("The tier1 field"): None until step 2b(i),
        #: then the Tier1Plan, then the Tier1Scan or the ScanRefused.
        self.tier1_state = None

    def refuse(self, reason, message):
        """Record a refusal, with the tier1 field once step 2b(i) set it."""
        fields = {} if self.tier1_state is None else self.tier1_state.ledger_fields()
        self.run.refused(Refusal(reason=reason, message=str(message)), **fields)

    def execute(self):
        # The prefix is read first, from the checkout, exactly as the cap
        # read it (job_guard.job_gateway): a config that cannot be loaded is
        # refused before gh is asked anything or a sandbox is made.
        try:
            self.branch_prefix = strict_config(self.repo)["guardrails"]["branch_prefix"]
        except ConfigError as error:
            self.refuse(Reason.GUARD_ERROR, f"the sweep could not read its config: {error}")
            return
        try:
            base = resolve_sweep_base(self.repo)
        except publish.PublishRefused as error:
            self.refuse(Reason.SWEEP_BASE, f"the sweep has no base: {error}")
            return
        # Step 0 (iii)'s binding, with origin's repository: a zero-argument
        # callable, asked afresh by sweep_decision.
        self.listing = partial(self.tools.open_sweep_prs, self.repo, gh_repo=base.gh_repo)
        try:
            # Step 1, pinned at the commit resolved above.
            with prepare_sandbox(self.repo, base.sha,
                                 parent=self.tools.sandbox_parent) as sandbox:
                self.sweep_in(sandbox)
        except LintUntrusted as error:
            self.refuse(Reason.LINT, error)
        except GitGuardError as error:
            if self.run.outcome is None:
                # ScanRefused never reaches here (sweep_in records it), but
                # every other ScopeCheckError and GitGuardError does: the
                # clone, the pristine checks, the plan's reads.
                self.refuse(Reason.SANDBOX, error)
            else:
                # The line is recorded and only the sandbox's removal
                # failed (Sandbox.__exit__): that does not change what the
                # sweep did, so it is reported, not recorded over it.
                warnings.warn(f"the sweep's sandbox was not removed: {error}",
                              RuntimeWarning, stacklevel=2)

    def note_scan_pending(self, model, pending):
        """run_tier1_scan's `before_call`, called immediately before the
        `claude` process starts: note what the line must say if the run
        ends before the call's outcome is known (a signal, ^C, a crash
        inside run_tier1_scan): a call that may have spent with no usage
        (both counts budget.UNRECORDED, and `model`), and `pending`, the
        FAILED scan of this call, which tier1.scan_attempts counts as an
        attempt. Nothing is noted when the scan fails before its call.
        note_scan replaces it once the scan returns or is refused."""
        self.run.note(input_tokens=UNRECORDED, output_tokens=UNRECORDED, model=model,
                      **pending.ledger_fields())

    def note_scan(self, model, scan):
        """Replace note_scan_pending's fields with the scan's own: its cost
        fields (line_cost_fields; none at all for a scan that made no call)
        and its tier1 field, which every later outcome also passes."""
        cost = line_cost_fields(model, scan=scan)
        self.run.unnote(*(COST_FIELDS - cost.keys()))
        self.run.note(**cost, **scan.ledger_fields())

    def sweep_in(self, sandbox):
        """Steps 2 to 2c in `sandbox`; records the line on every path."""
        run, repo = self.run, self.repo
        # Step 2: the base's work list, and what was found.
        lint_sandbox(sandbox, **self.tools.lint_options)
        current = found_set(sandbox.work_list, sandbox.base)
        previous = snapshot_for(repo, sandbox.base, read_snapshot(snapshot_path(repo)))
        delta = found_delta(previous, current)

        def write_snapshot():
            # Step 2a, after a non-refusal line.
            write_snapshot_or_warn(snapshot_path(repo), current, sandbox=sandbox)

        def record_terminal(decision):
            # Steps 2b (iii) and 2c: a terminal decision's line, then the
            # snapshot, except after UNKNOWN's refusal.
            decision.record(run, delta, **self.tier1_state.ledger_fields())
            if decision.kind != UNKNOWN:
                write_snapshot()

        # Step 2b (i): plan tier 1 on the pristine sandbox.
        plan = plan_scan(sandbox, run.ledger.entries())
        self.tier1_state = plan
        # Step 2b (ii): decide.
        decision = sweep_decision(sandbox.work_list, sandbox.base, run.ledger.entries(),
                                  self.listing, branch_prefix=self.branch_prefix,
                                  tier1=plan.will_scan)
        # Step 2b (iii): a terminal decision ends the sweep.
        if decision.terminal:
            record_terminal(decision)
            return
        # Step 2b (iv): held files leave the work list.
        decision.apply_to(sandbox)

        # Step 2c: tier 1.
        if plan.will_scan:
            model = config_value(str(sandbox.path), "models", "cheap")
            try:
                # Noted just before the call, corrected after it ("The
                # line's cost" in the module docstring).
                scan = run_tier1_scan(sandbox, plan, decision.held_paths,
                                      run.ledger.entries(), model=model,
                                      timeout=self.tools.scan_timeout,
                                      claude=self.tools.claude,
                                      before_call=partial(self.note_scan_pending, model))
            except ScanRefused as error:
                self.tier1_state = error
                self.note_scan(model, error.scan)
                self.refuse(Reason.SANDBOX, error)
                return
            self.tier1_state = scan
            self.note_scan(model, scan)
            sandbox.work_list = with_findings(sandbox.work_list, scan)
            decision = decision.after_tier1(sandbox.work_list, scan)
            if decision.terminal:
                record_terminal(decision)
                return

        run.failed(NOT_WIRED, **decision.ledger_fields(), found_delta=delta,
                   **self.tier1_state.ledger_fields())
        write_snapshot()


# --- the line's cost -------------------------------------------------------------------

#: Every top-level field line_cost_fields may write.
COST_FIELDS = frozenset({"input_tokens", "output_tokens", "model", "cost_usd"})


def line_cost_fields(model, scan=None, fix_run=None):
    """The top-level cost fields of a sweep line on which `scan` and
    `fix_run` ran, priced at `model` (fix_scope, "The line's cost"). Pure.

    `scan` is a tier1.Tier1Scan (a ScanRefused's `.scan`), a tier1.Tier1Plan
    (a plan is not a call: nothing was spent), or None; `fix_run` is
    fix_step's FixStepRun, or None when the fix step was not invoked.
    TypeError for anything else, rather than a guess at what it spent.

    - No call ran: `scan` is None, a plan, or a scan that is not `called`;
      AND `fix_run` is None or did not start. Then {}: no `input_tokens`,
      `output_tokens` or `model`, never zeros.
    - Every call that ran reported numeric `input_tokens` and
      `output_tokens` in its usage: both counts are the SUM of the calls',
      with `model`.
    - Any call that ran did not (its usage is missing, as after a timeout
      or crash, or a count is not a finite non-negative number): both
      counts are budget.UNRECORDED, with `model`, whatever the other call
      reported; budget.month_to_date counts the line as `unrecorded`.
    - `cost_usd` is the sum of the calls' reported dollar figures (the fix
      step's `total_cost_usd` plus the scan's `cost_usd`), and is left out
      when a call that ran reported none, so a known part is never written
      as the whole. It is kept for the record; the ceiling prices tokens.

    Known limit: one `model` prices both calls (both run on
    `models.cheap`)."""
    if not (scan is None or isinstance(scan, (Tier1Scan, Tier1Plan))):
        raise TypeError(f"scan must be a Tier1Scan, a Tier1Plan or None, not "
                        f"{type(scan).__name__}")
    if not (fix_run is None or isinstance(fix_run, FixStepRun)):
        raise TypeError(f"fix_run must be a FixStepRun or None, not {type(fix_run).__name__}")
    calls = []
    if isinstance(scan, Tier1Scan) and scan.called:
        calls.append((scan.usage, scan.cost_usd))
    if fix_run is not None and fix_run.started:
        payload = fix_run.payload() or {}
        calls.append((payload.get("usage"), payload.get("total_cost_usd")))
    if not calls:
        return {}
    counts = [_counts(usage) for usage, _cost in calls]
    if any(count is None for count in counts):
        fields = {"input_tokens": UNRECORDED, "output_tokens": UNRECORDED}
    else:
        fields = {"input_tokens": sum(count[0] for count in counts),
                  "output_tokens": sum(count[1] for count in counts)}
    fields["model"] = model
    costs = [cost for _usage, cost in calls]
    if all(_number(cost) for cost in costs):
        fields["cost_usd"] = sum(costs)
    return fields


def _counts(usage):
    """(input, output) tokens from a call's `usage`, or None when it has no
    usable pair."""
    if not isinstance(usage, dict):
        return None
    pair = (usage.get("input_tokens"), usage.get("output_tokens"))
    return pair if all(_number(value) for value in pair) else None


def _number(value):
    """A non-negative int or float, not a bool, that is a usable count or
    cost: one that converts to a finite float. WHY the conversion: an int
    too large for a float (10**400) makes math.isfinite raise
    OverflowError, and budget.PriceTable would raise the same when it
    prices it, so it is as unusable as NaN, and is UNRECORDED rather than
    a crash that loses the line's spend."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value)) and value >= 0
    except OverflowError:
        return False
