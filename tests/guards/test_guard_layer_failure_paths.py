"""When the guard layer itself fails, the audit trail must survive the failure.

Every case here ends with the side effect correctly prevented or correctly
allowed — the direction was never wrong. What was wrong is what the operator is
left holding afterwards: the real exception replaced by a bookkeeping one, a
refusal that raised instead of being returned, or an unenforced guardrail that
left no record at all. The Degraded vocabulary exists precisely so that an
unenforced check is visible, and these are the paths where it was not.
"""

import os

import pytest

from curamoir.budget import Budget, PriceTable
from curamoir.guards import Degraded, Gateway, JobContext, Reason, Refusal
from curamoir.guards.default_branch import DefaultBranchGuard
from curamoir.guards.lock import SupervisorLock
from curamoir.job_run import JobRun
from curamoir.ledger import Ledger

PRICES = PriceTable({"claude-sonnet-5": {"input": 2.0, "output": 10.0}})


def test_a_ledger_failure_does_not_replace_the_exception_the_job_died_of(tmp_path):
    """`JobRun`'s docstring is "cannot leave without writing its ledger line".
    When the ledger itself cannot be written, the operator needs the REAL
    failure, not a PermissionError about bookkeeping."""
    state = tmp_path / "state"
    state.mkdir()
    ledger = Ledger(state / "ledger.jsonl")
    os.chmod(state, 0o500)
    try:
        with pytest.raises(RuntimeError, match="the real failure"):
            with JobRun(job="sweep", ledger=ledger):
                raise RuntimeError("the real failure the operator needs to see")
    finally:
        os.chmod(state, 0o700)


def test_a_refusal_is_returned_even_when_it_cannot_be_recorded(tmp_path):
    """`perform` is the chokepoint: a caller handles a returned Refusal, and an
    exception out of it is recorded by JobRun as outcome=failure with a Python
    error message instead of outcome=refused with the real reason."""
    state = tmp_path / "state"
    state.mkdir()
    ledger = Ledger(state / "ledger.jsonl")
    gateway = Gateway(
        guards=[lambda _c: Refusal(Reason.PAUSED, "paused by the kill switch")],
        ledger=ledger,
    )
    os.chmod(state, 0o500)
    try:
        outcome = gateway.perform(JobContext(job="sweep", repo=tmp_path), lambda: "RAN")
    finally:
        os.chmod(state, 0o700)

    assert isinstance(outcome, Refusal)
    assert outcome.reason is Reason.PAUSED


def test_a_guard_returning_something_unexpected_refuses_rather_than_raising(tmp_path):
    """A guard author who returns True meaning "fine" got an AttributeError out
    of the chokepoint — neither a refusal nor a pass."""
    gateway = Gateway(guards=[lambda _c: True], ledger=Ledger(tmp_path / "l.jsonl"))

    outcome = gateway.perform(JobContext(job="sweep", repo=tmp_path), lambda: "RAN")

    assert isinstance(outcome, Refusal)
    assert outcome.reason is Reason.GUARD_ERROR


def test_an_unenforced_guardrail_is_never_silent_even_with_no_ledger(tmp_path, capsys):
    """`Gateway(guards=[...])` defaults to ledger=None, and Degraded then
    allowed the action with no record anywhere — which is the one outcome the
    Degraded type exists to make impossible."""
    gateway = Gateway(guards=[lambda _c: Degraded(Reason.PR_CAP, "gh unreachable")])

    outcome = gateway.perform(JobContext(job="sweep", repo=tmp_path), lambda: "RAN")

    assert outcome == "RAN"
    assert "gh unreachable" in capsys.readouterr().err


def test_a_lock_timestamped_in_the_future_is_treated_as_stale(tmp_path):
    """A negative age is never greater than the timeout, so a clock-skewed CI
    machine blocked the repo forever with a message that gave no hint that
    clearing the lock was safe."""
    import json
    from datetime import datetime, timedelta, timezone

    lock = tmp_path / ".curamoir" / "state" / "lock"
    lock.parent.mkdir(parents=True)
    future = datetime.now(timezone.utc) + timedelta(days=7)
    lock.write_text(json.dumps({
        "job": "sweep", "pid": 4242, "host": "some-other-host",
        "started": future.isoformat(),
    }), encoding="utf-8")

    verdict = SupervisorLock()(JobContext(job="sweep", repo=tmp_path))

    assert verdict is not None
    assert "clock" in verdict.message.lower() or "future" in verdict.message.lower()


def test_an_undetectable_current_branch_does_not_pass_the_branch_check(tmp_path):
    """`_git` returns "" whenever git exits non-zero — a repo with no commits, a
    cwd that is not a work tree. With an empty configured prefix, `"".startswith("")`
    was True and the guard allowed: the protection was conditional on detection
    succeeding, which its docstring says it is not."""
    guard = DefaultBranchGuard(branch_prefix="", detect_default=lambda _r: None)

    verdict = guard(JobContext(job="sweep", repo=tmp_path))

    assert verdict is not None, "an unknown current branch is not a safe branch"


def test_the_ceiling_is_compared_against_the_projected_cost_not_only_the_spent_one(tmp_path):
    """E0-R4 says refuse if the PROJECTED cost would breach the ceiling. With
    per_run_estimate_usd defaulting to 0.0 the guard compared only what was
    already spent, so a run was allowed with one cent of headroom and could
    overshoot by any amount."""
    import json

    state = tmp_path / ".curamoir" / "state"
    state.mkdir(parents=True)
    (state / "ledger.jsonl").write_text(json.dumps({
        "kind": "job", "job": "sweep", "model": "claude-sonnet-5",
        "input_tokens": 7_000_000, "output_tokens": 0,
        "at": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).isoformat(),
    }) + "\n", encoding="utf-8")

    guard = Budget(prices=PRICES, monthly_ceiling_usd=15.0, per_run_estimate_usd=5.0)
    verdict = guard(JobContext(job="sweep", repo=tmp_path))

    assert verdict is not None, "spent $14 + a $5 run exceeds a $15 ceiling"
    assert verdict.reason is Reason.BUDGET
