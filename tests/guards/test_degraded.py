"""A guard that cannot complete its check is neither a refusal nor a clean pass.

Guards with a network dependency — the kill switch's repo-label lookup (E0-R3),
the PR cap's open-PR count (E0-R6) — must not read a `gh` outage as a refusal,
or a month of scheduled work halts for the wrong reason. But allowing silently
is how you discover, four weeks later, that a guardrail never ran.

So: allow, and record. `Degraded` is the third outcome.
"""

from curamoir.guards import Degraded, Gateway, JobContext, Reason
from curamoir.guards.kill_switch import KillSwitch
from curamoir.ledger import Ledger


def test_a_degraded_guard_does_not_block_the_side_effect(tmp_path):
    performed = []
    gateway = Gateway(
        guards=[lambda _c: Degraded(Reason.PAUSED, "could not reach gh")],
        ledger=Ledger(tmp_path / "ledger.jsonl"),
    )

    gateway.perform(JobContext(job="sweep"), lambda: performed.append("wrote"))

    assert performed == ["wrote"]


def test_a_degraded_check_is_recorded_so_it_is_not_silent(tmp_path):
    ledger = Ledger(tmp_path / "ledger.jsonl")
    gateway = Gateway(
        guards=[lambda _c: Degraded(Reason.PAUSED, "could not reach gh")],
        ledger=ledger,
    )

    gateway.perform(JobContext(job="sweep"), lambda: None)

    entry = ledger.entries()[0]
    assert entry["kind"] == "guard"
    assert entry["severity"] == "degraded"
    assert "could not reach gh" in entry["message"]


def test_later_guards_still_run_after_a_degraded_one(tmp_path):
    """Degraded is not a short circuit. A failed label lookup must not stop the
    budget check from running."""
    consulted = []
    gateway = Gateway(
        guards=[
            lambda _c: Degraded(Reason.PAUSED, "label lookup failed"),
            lambda _c: consulted.append("budget"),
        ],
        ledger=Ledger(tmp_path / "ledger.jsonl"),
    )

    gateway.perform(JobContext(job="sweep"), lambda: None)

    assert consulted == ["budget"]


def test_kill_switch_reports_degraded_when_the_label_source_fails(harness):
    def exploding_labels():
        raise RuntimeError("gh unreachable")

    result = harness.run(KillSwitch(repo_labels=exploding_labels), job="sweep")

    assert result.performed is True
    assert result.degraded is not None
    assert "gh unreachable" in result.degraded.message
    assert [e["severity"] for e in result.ledger] == ["degraded"]
