"""The sweep's body (cm-fkd.13 and cm-fkd.14, cm-fkd.4's ST4 and ST5):
fix_scope's runner steps 1 to 10, "The tier1 field" and "The line's cost".

`run_sweep(repo, run, tools)` is `runner.JOBS["sweep"]`'s body, run behind
the guards (fix_scope step 0): `curamoir run sweep` reaches it through
runner.run_job, never by calling job_guard.run_guarded here.

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

**Refusals before step 3**, each with its Reason, and never a
`found_delta` or a snapshot (fix_scope step 2a): an unloadable
`curamoir.yaml` (GUARD_ERROR, as job_guard refuses one); no base
(SWEEP_BASE); the lint's report cannot be acted on, sweep.LintUntrusted
(LINT); the sandbox cannot be made or held to its base,
git_guard.GitGuardError and its ScopeCheckError and tier1.ScanRefused
(SANDBOX); the open sweep PRs unknown, idempotence's UNKNOWN (IDEMPOTENCE,
written by Decision.record); the base sets `sweep.max_files_per_pr: 0`, so
no PR may carry a file (FILE_CAP, after step 2c and before step 3: no
remedy is applied and no fix call made); a clock that gives no UTC day
(GUARD_ERROR, first of all). From step 2b(i) on (once the plan is set)
every line, refusals included, carries `**tier1_state.ledger_fields()`
(fix_scope, "The tier1 field"), except UNKNOWN's.

**Steps 3 to 10** (fix_scope's "What the runner does", in its order):
the remedies, then the fix step on what is left for the agent, its review
(verify_fix with the fix step's own snapshot), the trim to
`sweep.max_files_per_pr` and the test gate, the fix records, the patch of
exactly `gate.paths_for_pr`, the PR's text (sweep_pr: final_body, the
branch, the title and the commit message), and ONE publish.publish. The
fix agent runs once, in the sandbox, with fix_step.FIX_TOOLS (no shell),
and never in the user's checkout; nothing is ever re-run there.

**What each outcome writes.** A refusal carries no `found_delta`,
decision fields, `fixes` or snapshot (fix_scope step 2a); it keeps the
tier1 field and the cost fields of any call already made (note_cost).
The scope fields (cm-9ab): `scope` is the FIRST review's verdict, on a
SCOPE refusal and on the SANDBOX refusal of a file that could not be
dropped; `scope_after_drop` is the second review's, on a SCOPE refusal
after files were dropped; `scope_dropped` (the dropped breaches) is on
every line of a run that dropped files, whatever its outcome. Every other
line from step 3 on carries the decision's fields (Decision.ledger_fields),
`found_delta`, the tier1 field and, once the gate ran, its `test_gate`;
the snapshot follows it.

- Refusals, each with its Reason: the remedies or the review cannot
  answer, a git_guard.GitGuardError or its ScopeCheckError, a file that
  could not be dropped among them (SANDBOX); the verdict still has
  breaches once the breaching files are dropped, or nothing is left after
  dropping them (SCOPE); the gate or the patch raises
  green_gate.GateError (TEST_GATE); the fixes cannot be
  counted, record_fixes' or record_tier1_fixes' error or a ValueError from
  fix_counts.ledger_fields (FIX_RECORDS); publish.PublishRefused, nothing
  pushed (PUBLISH).
- Failures: the fix step did not finish (it could not start, timed out,
  exited non-zero, or its payload is missing, `is_error`, or has a
  `subtype` starting with "error", as `error_max_turns` does): no review,
  since a stopped agent may have left a file half-written and gave no
  reply to credit tier-1 fixes by; the fix step changed nothing
  (NOTHING_CHANGED: work remains), or, when the remedies left the agent
  nothing and no fix step ran, every remedy was skipped
  (NOTHING_REMEDIED, with `remedies_skipped`); the gate abandoned the PR
  (gate.render() is the message); the PR's text cannot be written
  (sweep_pr's ValueError, its SweepBodyError included: files with no fix,
  a body that will not fit or whose redaction or marker does not check),
  before any push; publish.PushFailed and publish.PrCreateFailed (no
  `pr`; the latter says whether the branch was deleted again);
  publish.PrOutcomeUnknown, whose message starts with PR_MAY_EXIST.
  While publish runs, the line carries PUBLISH_PENDING (the branch, and
  that the branch or PR may exist), so a run that ends inside it
  (SIGTERM's Terminated, a crash) says so; it is dropped once publish
  returns or raises an outcome that says where the branch is
  (PublishRefused, PushFailed, PrCreateFailed, Published).
- The PR opened (publish.Published): `pr` (idempotence.pr_field) is
  noted on the line at once, so whatever comes next, a crash included,
  the line keeps it and the next sweep holds the PR's files. Then the
  success, with `fixes` (fix_counts.ledger_fields). The one failure
  after that point: GitHub opened the PR into a branch other than the
  default the sweep was built on (Published.base_branch is checked
  against SweepBase.default_branch), recorded with `pr` and without
  `fixes`.

**The date** of the branch, the title and the commit message is one
datetime.date, the UTC day of `tools.now()`, read once as the sweep
starts (sweep_day); a naive clock is refused then (GUARD_ERROR), before
anything is asked, cloned or spent, rather than read in some time zone.
When run_sweep makes its own SweepTools (the registered body does),
`now` is the run's clock (JobRun.now: run_job's and run_guarded's `now`,
else the wall clock), so the branch, the title and the job line's
`started` are dated by the same clock. It is read twice (`started` as the
run opens, the day as the body starts), so near midnight UTC the two can
fall on different days.

**The line's cost** (fix_scope's section of that name): line_cost_fields,
pure, gives a line's top-level `input_tokens`, `output_tokens`, `model`
and `cost_usd` from the calls that ran on it, on `models.cheap` (read
once, after step 2b(iv)). They are noted before each call and corrected
after it, by one method, _Sweep.note_cost. Immediately before a call's
`claude` process starts (run_tier1_scan's and run_fix_step's
`before_call`, after every step that can fail without a call), it writes
the cautious answer: both counts budget.UNRECORDED with `model`, no
`cost_usd` (the scan's alone would read as the whole), and the tier1
field, which for the scan is the FAILED field of this call (tier1's
`pending`). So a run that ends during or right after a call, however it
ends (SIGTERM's Terminated, ^C, an exception), still leaves a line the
budget counts as unrecorded spend, and a scan that tier1.scan_attempts
counts as an attempt. Once the call returns (or the scan is refused),
note_cost replaces them with line_cost_fields of every call so far, and
drops the fields that leaves out (JobRun.unnote): a scan that made no
call (no candidates, or a `claude` that never started) leaves none.

**Which `claude`.** `tools.claude`, by default CLAUDE, a module attribute
read when SweepTools is made (as github.GH is read when gh runs), so the
runner's registered body, which makes its SweepTools itself, can be
pointed at a stand-in by setting `sweep_job.CLAUDE` (tests/conftest.py
points it, and PATH's `claude`, at one that refuses for every test).
`tools.fix_timeout` is read the same way, from FIX_TIMEOUT, so a test of
the registered body can make the fix step time out in seconds.

Syntax that imports on 3.9, like sweep.py.
"""

import warnings
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Optional

from curamoir import github, publish
from curamoir.budget import (
    CACHE_READ_FIELD,
    CACHE_WRITE_1H_FIELD,
    CACHE_WRITE_5M_FIELD,
    CACHE_WRITE_FIELD,
    UNRECORDED,
)
from curamoir.config import ConfigError
from curamoir.fix_counts import (
    found_delta,
    found_set,
    read_snapshot,
    record_fixes,
    snapshot_for,
    snapshot_path,
    write_snapshot_or_warn,
)
from curamoir.fix_counts import ledger_fields as fix_ledger_fields
from curamoir.fix_scope import (
    ScopeCheckError,
    base_principles,
    drop_breaching_paths,
    lint_sandbox,
    nfc,
    verify_fix,
)
from curamoir.fix_step import (
    DEFAULT_TIMEOUT,
    FixStepRun,
    fix_prompt,
    run_fix_step,
    usable_number,
)
from curamoir.git_guard import GitGuardError, git, prepare_sandbox, snapshot_git_state
from curamoir.green_gate import ABANDONED, GateError, run_test_gate
from curamoir.guards.reasons import Reason
from curamoir.guards.result import Refusal
from curamoir.idempotence import UNKNOWN, pr_field, sweep_decision
from curamoir.remedies import apply_file_remedies
from curamoir.repo_config import config_value, strict_config
from curamoir.sweep import LintUntrusted
from curamoir.sweep_pr import commit_message, final_body, pr_title, sweep_branch
from curamoir.tier1 import (
    SCAN_TIMEOUT,
    ScanRefused,
    Tier1Plan,
    Tier1Scan,
    plan_scan,
    record_tier1_fixes,
    run_tier1_scan,
    trim_pr_paths,
    with_findings,
)

#: The `claude` executable a SweepTools made with no `claude` uses (module
#: docstring, "Which `claude`"): the seam the registered body is pointed at
#: a stand-in through.
CLAUDE = "claude"

#: The fix step's timeout in seconds a SweepTools made with no
#: `fix_timeout` uses (module docstring, "Which `claude`"): the seam a test
#: of the registered body shortens it through.
FIX_TIMEOUT = DEFAULT_TIMEOUT

#: How the failure line of a PrOutcomeUnknown starts: the PR may be open.
PR_MAY_EXIST = "the PR may exist"

#: The failure of a fix step that changed nothing (fix_scope step 7).
NOTHING_CHANGED = ("the fix step changed nothing, so nothing was fixed and no PR was "
                   "opened; the work list's violations remain")

#: The failure when no fix step ran (the remedies left the agent nothing)
#: and every remedy was skipped: `remedies_skipped` on the line says why.
NOTHING_REMEDIED = ("no fix step ran (the work list held only file remedies) and every "
                    "remedy was skipped, so nothing was fixed and no PR was opened; "
                    "remedies_skipped says why")

#: The field noted while publish.publish runs, and dropped once it returns
#: or raises an outcome that says where the branch is: on a line whose run
#: ended inside publish, it says the branch and the PR may exist.
PUBLISH_PENDING = "publish_pending"

#: How a FIX_RECORDS refusal's message starts.
FIXES_UNCOUNTED = "the PR's fixes could not be counted, so no PR was opened"

#: The refusal of a sweep whose base sets `sweep.max_files_per_pr: 0`.
NO_FILES_ALLOWED = ("sweep.max_files_per_pr is 0, so no PR may carry a file: no remedy "
                    "was applied and no fix step was run")


def _utc_now():
    return datetime.now(timezone.utc)


def _claude():
    return CLAUDE


def _fix_timeout():
    return FIX_TIMEOUT


@dataclass(frozen=True)
class SweepTools:
    """What run_sweep calls out to, so tests can stand in for each.

    `claude` is the `claude` executable for both model calls (fix_step's
    seam), CLAUDE when not given. `open_sweep_prs(repo, gh_repo=...)` is the
    open-PR listing, github.open_sweep_prs by default; _Sweep.execute binds
    it to the repository and origin's gh_repo itself (fix_scope step 0 (iii)),
    so it is asked afresh, never cached. `now` is the clock, returning an
    aware datetime; its UTC day dates the sweep branch, the PR title and
    the commit message (sweep_day). `lint_options` are sweep.work_list's
    keyword arguments (`lint`, `timeout`, `grace`), for lint_sandbox and
    the re-lint (fix_counts.record_fixes). `scan_timeout` bounds the tier-1
    call (tier1.SCAN_TIMEOUT), `fix_timeout` the fix step (FIX_TIMEOUT
    when not given, fix_step.DEFAULT_TIMEOUT when None). `sandbox_parent`
    is where the sandbox is made (the system temporary directory when
    None)."""

    claude: str = field(default_factory=_claude)
    open_sweep_prs: Callable = github.open_sweep_prs
    now: Callable[[], datetime] = _utc_now
    lint_options: dict = field(default_factory=dict)
    scan_timeout: float = SCAN_TIMEOUT
    fix_timeout: Optional[float] = field(default_factory=_fix_timeout)
    sandbox_parent: Optional[str] = None


def sweep_day(moment):
    """The UTC day of `moment`, an aware datetime, as the datetime.date
    sweep_pr dates a sweep with. ValueError for a naive one (its time zone
    would be a guess) or anything that is not a datetime."""
    if not isinstance(moment, datetime) or moment.utcoffset() is None:
        raise ValueError(f"the sweep's clock must give an aware datetime, not {moment!r}")
    return moment.astimezone(timezone.utc).date()


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
    JobRun writing its ledger line): fix_scope's steps 1 to 10 (module
    docstring). Records exactly one outcome on `run`, or raises and
    job_guard.run_guarded records the crash. `runner.JOBS["sweep"]`.
    With no `tools`, SweepTools() on the run's clock (`run.now`; module
    docstring, "The date")."""
    _Sweep(Path(repo), run, tools or SweepTools(now=run.now)).execute()


class _Sweep:
    """One sweep's state, so every refusal can carry `tier1_state`."""

    def __init__(self, repo, run, tools):
        self.repo = repo
        self.run = run
        self.tools = tools
        #: Set by execute before the sandbox: guardrails.branch_prefix, the
        #: SweepBase (origin's gh_repo, for the listing and publish; not to
        #: be confused with `sandbox.base`, the base commit's SHA), and the
        #: open-PR listing bound to that gh_repo.
        self.branch_prefix = None
        self.sweep_base = None
        #: The sweep's one date (sweep_day), read first by execute.
        self.day = None
        self.listing = None
        #: fix_scope's ONE object ("The tier1 field"): None until step 2b(i),
        #: then the Tier1Plan, then the Tier1Scan (its pending FAILED one
        #: while the call runs) or the ScanRefused.
        self.tier1_state = None

    def refuse(self, reason, message, **fields):
        """Record a refusal, with the tier1 field once step 2b(i) set it."""
        tier1 = {} if self.tier1_state is None else self.tier1_state.ledger_fields()
        self.run.refused(Refusal(reason=reason, message=str(message)), **fields, **tier1)

    def execute(self):
        # The day is read first, once, so a clock that cannot give a UTC day
        # is refused before anything is asked, cloned or spent (module
        # docstring, "The date").
        try:
            self.day = sweep_day(self.tools.now())
        except ValueError as error:
            self.refuse(Reason.GUARD_ERROR, f"the sweep has no date: {error}")
            return
        # The prefix is read next, from the checkout, exactly as the cap
        # read it (job_guard.job_gateway): a config that cannot be loaded is
        # refused before gh is asked anything or a sandbox is made.
        try:
            self.branch_prefix = strict_config(self.repo)["guardrails"]["branch_prefix"]
        except ConfigError as error:
            self.refuse(Reason.GUARD_ERROR, f"the sweep could not read its config: {error}")
            return
        try:
            self.sweep_base = resolve_sweep_base(self.repo)
        except publish.PublishRefused as error:
            self.refuse(Reason.SWEEP_BASE, f"the sweep has no base: {error}")
            return
        # Step 0 (iii)'s binding, with origin's repository: a zero-argument
        # callable, asked afresh by sweep_decision.
        self.listing = partial(self.tools.open_sweep_prs, self.repo,
                               gh_repo=self.sweep_base.gh_repo)
        try:
            # Step 1, pinned at the commit resolved above.
            with prepare_sandbox(self.repo, self.sweep_base.sha,
                                 parent=self.tools.sandbox_parent) as sandbox:
                self.sweep_in(sandbox)
        except LintUntrusted as error:
            if self.run.outcome is None:
                self.refuse(Reason.LINT, error)
            else:
                # Not reachable today (sweep_in records the re-lint's own
                # LintUntrusted), but a line already recorded is never
                # recorded over: it says what the sweep did.
                warnings.warn(f"the sweep's lint failed after its line was recorded: "
                              f"{error}", RuntimeWarning, stacklevel=2)
        except GitGuardError as error:
            if self.run.outcome is None:
                # ScanRefused never reaches here (sweep_in records it), but
                # every other ScopeCheckError and GitGuardError does: the
                # clone, the pristine checks, the plan's reads, the
                # remedies, the review.
                self.refuse(Reason.SANDBOX, error)
            else:
                # The line is recorded and only the sandbox's removal
                # failed (Sandbox.__exit__): that does not change what the
                # sweep did, so it is reported, not recorded over it.
                warnings.warn(f"the sweep's sandbox was not removed: {error}",
                              RuntimeWarning, stacklevel=2)

    def note_cost(self, model, fix_run=None, pending=False):
        """Note the line's cost fields and the tier1 field (module
        docstring, "The line's cost"). `pending`: a call is about to start
        (a `before_call`), so both counts are budget.UNRECORDED with
        `model`, and `cost_usd` is dropped: if the run ends now, a known
        part (the scan's cost) must not stand as the whole. Otherwise the
        line_cost_fields of the calls so far, the scan in `tier1_state` and
        `fix_run`, with every cost field they leave out dropped."""
        if pending:
            cost = {"input_tokens": UNRECORDED, "output_tokens": UNRECORDED, "model": model}
        else:
            state = self.tier1_state
            cost = line_cost_fields(model, scan=state.scan if isinstance(state, ScanRefused)
                                    else state, fix_run=fix_run)
        self.run.unnote(*(COST_FIELDS - cost.keys()))
        self.run.note(**cost, **self.tier1_state.ledger_fields())

    def note_scan_pending(self, model, pending):
        """run_tier1_scan's `before_call`: `pending`, the FAILED scan of this
        call, becomes `tier1_state` (an attempt, if the run ends now), and
        the call's cost is noted as pending."""
        self.tier1_state = pending
        self.note_cost(model, pending=True)

    def sweep_in(self, sandbox):
        """Steps 2 to 10 in `sandbox`; records the line on every path."""
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
        # The model both calls run on, the base's `models.cheap`.
        model = config_value(str(sandbox.path), "models", "cheap")

        # Step 2c: tier 1.
        if plan.will_scan:
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
                self.note_cost(model)
                self.refuse(Reason.SANDBOX, error)
                return
            self.tier1_state = scan
            self.note_cost(model)
            sandbox.work_list = with_findings(sandbox.work_list, scan)
            decision = decision.after_tier1(sandbox.work_list, scan)
            if decision.terminal:
                record_terminal(decision)
                return

        _Fix(self, sandbox, decision, delta, model, write_snapshot).execute()


class _Fix:
    """Steps 3 to 10 for one sweep (`owner`, the _Sweep) in `sandbox`, after
    a WORK `decision`: every line they write carries the decision's fields,
    `found_delta` (`delta`) and the tier1 field, except a refusal (module
    docstring, "What each outcome writes")."""

    def __init__(self, owner, sandbox, decision, delta, model, write_snapshot):
        self.owner = owner
        self.run = owner.run
        self.sandbox = sandbox
        self.decision = decision
        self.delta = delta
        self.model = model
        self.write_snapshot = write_snapshot

    def fail(self, message, **fields):
        """A failure line with the sweep's fields, then the snapshot."""
        self.run.failed(message, **self.decision.ledger_fields(), found_delta=self.delta,
                        **fields, **self.owner.tier1_state.ledger_fields())
        self.write_snapshot()

    def execute(self):
        owner, sandbox, tools = self.owner, self.sandbox, self.owner.tools
        # A PR may carry no file at all (`sweep.max_files_per_pr: 0`, the
        # base's): refused before step 3, so no remedy is applied and no
        # fix call is paid for (fix_scope, "A cap of 0", before step 3).
        cap = config_value(str(sandbox.path), "sweep", "max_files_per_pr")
        if cap == 0:
            owner.refuse(Reason.FILE_CAP, NO_FILES_ALLOWED)
            return
        # Step 3: the runner's own remedies, then the agent's work list.
        remedies = apply_file_remedies(sandbox)
        agent_work = remedies.for_agent(sandbox.work_list)
        # Steps 4 and 5: the fix step, then its review.
        if agent_work.items or agent_work.tier1:
            prompt = fix_prompt(agent_work, base_principles(sandbox))
            options = {} if tools.fix_timeout is None else {"timeout": tools.fix_timeout}
            fix_run = run_fix_step(sandbox, prompt, model=self.model, claude=tools.claude,
                                   before_call=partial(owner.note_cost, self.model,
                                                       pending=True),
                                   **options)
            owner.note_cost(self.model, fix_run=fix_run)
            unfinished = _unfinished(fix_run)
            if unfinished:
                self.fail(unfinished)
                return
            git_state = fix_run.git_state
        else:
            # The remedies left the agent nothing: no call is made, and the
            # review holds the sandbox to a snapshot taken here, as
            # run_fix_step would have taken it.
            fix_run, git_state = None, snapshot_git_state(sandbox)
        # Step 5 (step 6, a GitGuardError here, is _Sweep.execute's refusal).
        first = verify_fix(sandbox, git_state=git_state)
        # cm-9ab (user decision 2026-10-28): drop and re-gate. Every file a
        # breach names is put back as it was at the base and the review runs
        # again; what is left goes on to the gate. Not when a breach says the
        # sandbox itself cannot be trusted (fix_scope.WHOLE_RUN_KINDS).
        try:
            verdict, dropped = drop_breaching_paths(sandbox, first, git_state=git_state,
                                                    removed=remedies.deleted)
        except ScopeCheckError as error:
            # Refused here rather than left to step 6, so the line keeps
            # `scope`: what the agent breached, which the generic refusal
            # would lose.
            owner.refuse(Reason.SANDBOX, f"a file outside E3-R2's scope could not be "
                         f"dropped: {error}", scope=first.as_dict())
            return
        if dropped:
            owner.run.note(scope_dropped=[breach.as_dict() for breach in dropped])
        # Still out of E3-R2's scope, or nothing in scope left: refused,
        # with what the agent did (`scope`) and, once files were dropped,
        # why what was left was refused too (`scope_after_drop`).
        if not verdict.ok or (dropped and verdict.nothing_changed):
            after_drop = {"scope_after_drop": verdict.as_dict()} if dropped else {}
            owner.refuse(Reason.SCOPE, first.render(), scope=first.as_dict(), **after_drop)
            return
        # Step 7: only a verdict that changed something is trimmed and
        # tested.
        if verdict.nothing_changed:
            if fix_run is None:
                # No fix step ran, and every remedy was skipped: say so, and
                # why, rather than blame a fix step (it recurs every run
                # until the reasons are dealt with).
                self.fail(NOTHING_REMEDIED, remedies_skipped=[
                    {"path": s.path, "rule_id": s.rule_id, "remedy": s.remedy,
                     "reason": s.reason} for s in remedies.skipped])
            else:
                self.fail(NOTHING_CHANGED)
            return
        # Trim, then the gate on the PR's tree.
        trimmed = trim_pr_paths(verdict.paths, sandbox.work_list, cap)
        trimmed_nfc = {nfc(path) for path in trimmed}
        left_out = [path for path in verdict.paths if nfc(path) not in trimmed_nfc]
        try:
            gate = run_test_gate(sandbox, verdict, paths=trimmed)
        except GateError as error:
            owner.refuse(Reason.TEST_GATE, f"the test gate could not answer: {error}")
            return
        if gate.strategy == ABANDONED:
            self.fail(gate.render(), **gate.ledger_fields())
            return
        # Step 8: the fixes, counted before the patch.
        try:
            records = record_fixes(sandbox, gate, remedies, **tools.lint_options)
            if fix_run is not None:
                records += record_tier1_fixes(sandbox, gate, fix_run)
        except (GitGuardError, LintUntrusted) as error:
            # GitGuardError covers record_fixes' GateError and ScopeCheckError
            # (both are GitGuardErrors), and a git call that failed.
            owner.refuse(Reason.FIX_RECORDS, f"{FIXES_UNCOUNTED}: {error}")
            return
        try:
            fix_fields = fix_ledger_fields(records, self.delta)
        except ValueError as error:
            # Records of another base than the delta's: only ledger_fields'
            # ValueError is a refusal; one from building the records is a
            # bug, and crashes as one.
            owner.refuse(Reason.FIX_RECORDS, f"{FIXES_UNCOUNTED}: {error}")
            return
        # Step 9: the patch of exactly the gate's paths, from the bytes it
        # tested.
        try:
            patch = gate.patch(sandbox)
        except GateError as error:
            owner.refuse(Reason.TEST_GATE, error)
            return
        self.open_pr(records, gate, patch, remedies, left_out, fix_fields, dropped)

    def open_pr(self, records, gate, patch, remedies, left_out, fix_fields, dropped=()):
        """Step 9's PR: its text, then ONE publish.publish, and the line for
        each of publish's outcomes (publish's "Errors" table)."""
        owner, sandbox, sweep_base = self.owner, self.sandbox, self.owner.sweep_base
        gate_fields = gate.ledger_fields()
        try:
            # One day (owner.day) for the branch, the title and the message.
            body = final_body(records, sandbox.work_list, gate, sandbox.base, remedies.skipped,
                              left_out, scope_dropped=dropped)
            branch = sweep_branch(owner.branch_prefix, owner.day, sandbox.base)
            title = pr_title(records, gate, owner.day)
            message = commit_message(records, gate, owner.day, sandbox.base)
        except ValueError as error:
            # sweep_pr's SweepBodyError is a ValueError too.
            self.fail(f"the PR's text could not be written, so nothing was pushed: {error}",
                      **gate_fields)
            return
        # Until publish returns or raises one of its own outcomes, the
        # branch and the PR may exist: a run that ends in between (SIGTERM's
        # Terminated, a crash) leaves this on its line.
        self.run.note(**{PUBLISH_PENDING: {"branch": branch, "gh_repo": sweep_base.gh_repo,
                                           "note": f"{PR_MAY_EXIST}: the run ended while "
                                                   "it was being published"}})
        try:
            published = publish.publish(
                owner.repo, base=sandbox.base, patch=patch, paths=gate.paths_for_pr,
                branch=branch, branch_prefix=owner.branch_prefix, gh_repo=sweep_base.gh_repo,
                message=message, title=title, body=body)
        except publish.PublishRefused as error:
            self.run.unnote(PUBLISH_PENDING)
            owner.refuse(Reason.PUBLISH, f"the sweep's PR was refused before anything was "
                                         f"pushed: {error}")
            return
        except publish.PrOutcomeUnknown as error:
            self.fail(f"{PR_MAY_EXIST}: {error}", **gate_fields)
            return
        except publish.PublishError as error:
            # PushFailed and PrCreateFailed: no PR, and their messages say
            # where the branch is.
            self.run.unnote(PUBLISH_PENDING)
            self.fail(str(error), **gate_fields)
            return
        except GitGuardError as error:
            # Outside publish's family, so where it stopped is not known.
            self.fail(f"publishing failed unexpectedly, so whether {branch} reached origin "
                      f"is unknown (check origin, and its open PRs): {error}", **gate_fields)
            return
        # The PR exists: on the line at once, whatever comes next.
        self.run.unnote(PUBLISH_PENDING)
        self.run.note(pr=pr_field(published.number, published.branch, published.url,
                                  sandbox.base, gate.paths_for_pr))
        if published.base_branch != sweep_base.default_branch:
            self.fail(f"{published.url} was opened into {published.base_branch!r}, but the "
                      f"sweep was built on {sweep_base.default_branch!r} (GitHub's default "
                      "changed while it ran); check the PR before merging it", **gate_fields)
            return
        self.run.succeeded(f"opened {published.url}: {title}", **self.decision.ledger_fields(),
                           **gate_fields, **fix_fields, **owner.tier1_state.ledger_fields())
        self.write_snapshot()


def _unfinished(fix_run):
    """Why the fix step did not finish, or "" when it did (module docstring,
    "What each outcome writes"): it could not start, timed out, exited
    non-zero, or printed no payload, or one whose `is_error` is anything
    but False (absent is False) or whose `subtype` starts with "error" in
    any case (`error_max_turns`). What it said on stderr is quoted redacted
    and clipped (github.quote_output), as gh's is: it lands on a ledger
    line."""
    if not fix_run.started:
        # run_fix_step's own text: fix_step.NOT_STARTED and the OSError.
        return fix_run.stderr.strip() or "the fix step could not start"
    if fix_run.timed_out:
        return "the fix step timed out, so nothing it left was reviewed or opened as a PR"
    payload = fix_run.payload()
    # Finished only with `is_error` exactly False (or absent) and no
    # `subtype` starting "error", in any case (`error_max_turns`,
    # `Error_during_execution`); a payload with no subtype is finished.
    is_error = None if payload is None else payload.get("is_error", False)
    subtype = None if payload is None else payload.get("subtype")
    error_subtype = subtype is not None and str(subtype).casefold().startswith("error")
    if fix_run.exit_code != 0 or payload is None or is_error is not False or error_subtype:
        why = (f"exited {fix_run.exit_code}" if fix_run.exit_code != 0
               else "reported no result" if payload is None
               else f"reported an error ({subtype})" if error_subtype
               else f"reported an error (is_error: {is_error!r})")
        said = github.quote_output(fix_run.stderr)
        return (f"the fix step {why}, so nothing it left was reviewed or opened as a PR"
                + (f" (it said: {said})" if said else ""))
    return ""


# --- the line's cost -------------------------------------------------------------------

#: Every top-level field line_cost_fields may write.
COST_FIELDS = frozenset({"input_tokens", "output_tokens", CACHE_WRITE_FIELD,
                         CACHE_READ_FIELD, CACHE_WRITE_5M_FIELD, CACHE_WRITE_1H_FIELD,
                         "model", "cost_usd"})


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
      with `model`. So are the prompt-cache counts,
      `cache_creation_input_tokens` and `cache_read_input_tokens` (cm-2ui),
      each written only when its sum is above 0 (a call that used no cache
      leaves a line as it always was) and read as 0 when a usage lacks it.
    - Any call that ran did not (its usage is missing, as after a timeout
      or crash, or a count, a cache count included, is not
      fix_step.usable_number): both counts are budget.UNRECORDED, with
      `model` and no cache counts, whatever the other call reported;
      budget.month_to_date counts the line as `unrecorded`.
    - `cost_usd` is the sum of the calls' reported dollar figures (the fix
      step's `total_cost_usd` plus the scan's `cost_usd`), and is left out
      when a call that ran reported none (or one that is not
      usable_number), so a known part is never written as the whole. It is
      kept for the record; the ceiling prices tokens.

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
        for position, field in ((2, CACHE_WRITE_FIELD), (3, CACHE_READ_FIELD)):
            total = sum(count[position] for count in counts)
            if total > 0:
                fields[field] = total
        # The 5-minute / 1-hour split of the writes (cm-1et), only when every
        # call that wrote the cache gave it: a split for part of the writes
        # would price the rest as 1-hour anyway, but would read as complete.
        splits = [_write_split(usage) for usage, _cost in calls]
        writers = [splits[index] for index, count in enumerate(counts) if count[2] > 0]
        if writers and all(split is not None for split in writers):
            for position, field in ((0, CACHE_WRITE_5M_FIELD), (1, CACHE_WRITE_1H_FIELD)):
                total = sum(split[position] for split in writers)
                if total > 0:
                    fields[field] = total
    fields["model"] = model
    costs = [cost for _usage, cost in calls]
    if all(usable_number(cost) for cost in costs):
        fields["cost_usd"] = sum(costs)
    return fields


def _write_split(usage):
    """(5-minute, 1-hour) cache-write tokens from a call's
    usage.cache_creation, or None when it is absent or not a pair of usable
    counts: writes then price as 1-hour, the dearer."""
    split = usage.get("cache_creation") if isinstance(usage, dict) else None
    if not isinstance(split, dict):
        return None
    pair = tuple(0 if split.get(key) is None else split.get(key)
                 for key in ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens"))
    return pair if all(usable_number(value) for value in pair) else None


def _counts(usage):
    """(input, output, cache write, cache read) tokens from a call's
    `usage`, or None when it has no usable input and output pair or a cache
    count it gives is not usable. An absent cache count is 0."""
    if not isinstance(usage, dict):
        return None
    # A cache count that is absent or null is 0: the API omits or nulls it
    # for a call that used no cache. Any other non-number is not usable.
    cache = tuple(0 if usage.get(field) is None else usage.get(field)
                  for field in (CACHE_WRITE_FIELD, CACHE_READ_FIELD))
    counts = (usage.get("input_tokens"), usage.get("output_tokens"), *cache)
    return counts if all(usable_number(value) for value in counts) else None
