"""The ledger is the only durable record that a guardrail fired.

Guards fail open on hook timeout (DEC-5), so an unrecorded guard failure is a
silent loss of enforcement. E13-R3's dogfood log reads these entries.
"""

from curamoir.guards import Gateway, JobContext, Reason, Refusal
from curamoir.ledger import Ledger


def test_refusal_is_recorded_with_its_job_and_reason(tmp_path):
    ledger = Ledger(tmp_path / "ledger.jsonl")
    gateway = Gateway(
        guards=[lambda _c: Refusal(Reason.BUDGET, "at 95% of the monthly ceiling")],
        ledger=ledger,
    )

    gateway.perform(JobContext(job="sweep"), lambda: None)

    entries = ledger.entries()
    assert len(entries) == 1
    assert entries[0]["job"] == "sweep"
    assert entries[0]["reason"] == "budget"


def test_ledger_is_append_only_across_separate_handles(tmp_path):
    path = tmp_path / "ledger.jsonl"
    Ledger(path).record(job="sweep", reason=Reason.PAUSED, message="paused")
    Ledger(path).record(job="review", reason=Reason.LOCKED, message="locked")

    assert [e["reason"] for e in Ledger(path).entries()] == ["paused", "locked"]


def test_guard_failure_is_recorded_so_lost_enforcement_is_never_silent(tmp_path):
    def broken_guard(_context):
        raise RuntimeError("ledger unreadable")

    ledger = Ledger(tmp_path / "ledger.jsonl")
    gateway = Gateway(guards=[broken_guard], ledger=ledger)

    gateway.perform(JobContext(job="review"), lambda: None)

    entry = ledger.entries()[0]
    assert entry["reason"] == "guard_error"
    assert entry["job"] == "review"
    assert "broken_guard" in entry["message"]
