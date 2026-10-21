"""E0-R9: every job appends one ledger line on exit — success, failure or refusal.

Two kinds of record coexist and must not be confused:

- `kind="job"` — exactly one per job exit. This is what E0-R9 counts.
- `kind="guard"` — one per guardrail that fired. E13-R3's dogfood log reads
  these ("record every case where a guardrail fired"), and there can be several
  in a run, or none.

A job that crashes still writes its line. An unattended agent that dies
silently is indistinguishable from one that had nothing to do.
"""

import pytest

from curamoir.guards import JobContext, Reason, Refusal
from curamoir.job_run import JobRun
from curamoir.ledger import Ledger


def job_lines(ledger):
    return [e for e in ledger.entries() if e["kind"] == "job"]


def test_successful_job_writes_exactly_one_job_line(tmp_path):
    ledger = Ledger(tmp_path / "ledger.jsonl")

    with JobRun(ledger, job="sweep") as run:
        run.succeeded(summary="fixed 5 violations")

    lines = job_lines(ledger)
    assert len(lines) == 1
    assert lines[0]["outcome"] == "success"
    assert lines[0]["job"] == "sweep"


def test_refused_job_writes_one_job_line_carrying_the_reason(tmp_path):
    ledger = Ledger(tmp_path / "ledger.jsonl")

    with JobRun(ledger, job="sweep") as run:
        run.refused(Refusal(Reason.PR_CAP, "3 open curamoir/ PRs already"))

    lines = job_lines(ledger)
    assert len(lines) == 1
    assert lines[0]["outcome"] == "refused"
    assert lines[0]["reason"] == "pr_cap"


def test_a_crashing_job_still_writes_its_line(tmp_path):
    ledger = Ledger(tmp_path / "ledger.jsonl")

    with pytest.raises(RuntimeError), JobRun(ledger, job="review"):
        raise RuntimeError("model call exploded")

    lines = job_lines(ledger)
    assert len(lines) == 1
    assert lines[0]["outcome"] == "failure"
    assert "model call exploded" in lines[0]["message"]


def test_a_job_that_reports_nothing_still_writes_a_line(tmp_path):
    """Forgetting to call succeeded() must not lose the run. The line says the
    outcome is unknown, which is a bug signal rather than a silent success."""
    ledger = Ledger(tmp_path / "ledger.jsonl")

    with JobRun(ledger, job="brief"):
        pass

    lines = job_lines(ledger)
    assert len(lines) == 1
    assert lines[0]["outcome"] == "unknown"


def test_every_job_line_carries_a_run_id_and_a_timespan(tmp_path):
    ledger = Ledger(tmp_path / "ledger.jsonl")

    with JobRun(ledger, job="sweep") as run:
        run.succeeded()

    line = job_lines(ledger)[0]
    assert line["run_id"]
    assert line["started"] <= line["ended"]


def test_guard_events_and_the_job_line_are_separable(tmp_path):
    """A refused job writes one job line plus one guard event. E0-R9 counts the
    former; E13-R3's "every case where a guardrail fired" reads the latter."""
    from curamoir.guards import Gateway

    ledger = Ledger(tmp_path / "ledger.jsonl")
    gateway = Gateway(guards=[lambda _c: Refusal(Reason.PAUSED, "paused")], ledger=ledger)

    with JobRun(ledger, job="sweep") as run:
        outcome = gateway.perform(JobContext(job="sweep", repo=tmp_path), lambda: None)
        run.refused(outcome)

    entries = ledger.entries()
    assert [e["kind"] for e in entries] == ["guard", "job"]
    assert len(job_lines(ledger)) == 1


def test_the_ledger_is_readable_from_the_run_and_cannot_be_replaced(tmp_path):
    """A job reads its history through the run (fix_scope step 2b), from the
    ledger its own line goes to; it cannot point the run somewhere else."""
    ledger = Ledger(tmp_path / "ledger.jsonl")

    with JobRun(ledger, job="sweep") as run:
        assert run.ledger is ledger
        with pytest.raises(AttributeError):
            run.ledger = Ledger(tmp_path / "elsewhere.jsonl")
        run.succeeded()

    assert len(job_lines(ledger)) == 1


@pytest.mark.parametrize("finish", [
    lambda run: run.succeeded("swept after all"),
    lambda run: run.failed("clone failed"),
])
def test_a_later_outcome_clears_the_reason_an_earlier_refusal_recorded(tmp_path, finish):
    """The line carries the outcome last recorded, and a reason only when that
    outcome is a refusal: `success` with reason `budget` would contradict
    itself in the brief."""
    ledger = Ledger(tmp_path / "ledger.jsonl")

    with JobRun(ledger, job="sweep") as run:
        run.refused(Refusal(Reason.BUDGET, "over the ceiling"))
        finish(run)

    assert run.reason is None
    assert job_lines(ledger)[0]["reason"] is None


def test_a_crash_after_a_refusal_clears_the_reason_it_recorded(tmp_path):
    """The crash path's own clear: a body that refused and then raised wrote a
    failure, and a `failure` line carrying reason `budget` would tell the
    brief the job was refused for its budget when it in fact fell over."""
    ledger = Ledger(tmp_path / "ledger.jsonl")

    with (pytest.raises(RuntimeError, match="after the refusal"),
          JobRun(ledger, job="sweep") as run):
        run.refused(Refusal(Reason.BUDGET, "over the ceiling"))
        raise RuntimeError("after the refusal")

    [line] = job_lines(ledger)
    assert line["outcome"] == "failure"
    assert line["reason"] is None
    assert run.reason is None
