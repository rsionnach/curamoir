"""E0-R4: the spend ceiling.

Before every job the runner reads the ledger, computes month-to-date spend, and
refuses if the projected cost would breach `monthly_ceiling_usd`.

Cost is DERIVED at read time from recorded tokens and an editable price table,
never stored as a dollar figure. That is what makes E11-R2's "editing the price
table changes the reported cost without re-running jobs" possible, and it means
a stale price is correctable rather than baked into history.
"""

from datetime import datetime, timezone

from curamoir.budget import Budget, PriceTable
from curamoir.guards import Reason

# Verified 2026-10-05. Ships with a "verify prices" note (E11-R1).
PRICES = PriceTable({
    "claude-sonnet-5": {"input": 2.00, "output": 10.00},
    "claude-opus-5": {"input": 5.00, "output": 25.00},
})

OCTOBER = lambda: datetime(2026, 10, 5, tzinfo=timezone.utc)


def spend(ledger, model, input_tokens, output_tokens, at="2026-10-02T12:00:00+00:00"):
    ledger.record(
        kind="job", job="sweep", reason=None, message="", outcome="success",
        model=model, input_tokens=input_tokens, output_tokens=output_tokens, at=at,
    )


def test_an_empty_ledger_leaves_the_full_ceiling_available(harness):
    guard = Budget(prices=PRICES, monthly_ceiling_usd=15.0, now=OCTOBER)

    result = harness.run(guard, job="sweep")

    result.assert_allowed()


def test_spend_well_under_the_ceiling_allows_the_job(harness):
    ledger = harness.ledger()
    spend(ledger, "claude-sonnet-5", input_tokens=1_000_000, output_tokens=100_000)  # $3.00

    result = harness.run(Budget(prices=PRICES, monthly_ceiling_usd=15.0, now=OCTOBER), job="sweep")

    result.assert_allowed()


def test_a_ledger_at_95_percent_of_the_ceiling_refuses_with_reason_budget(harness):
    ledger = harness.ledger()
    # 2M in + 1M out on sonnet = $4.00 + $10.00 = $14.00 of a $15 ceiling.
    spend(ledger, "claude-sonnet-5", input_tokens=2_000_000, output_tokens=1_000_000)

    result = harness.run(
        Budget(prices=PRICES, monthly_ceiling_usd=15.0, per_run_estimate_usd=2.0, now=OCTOBER),
        job="sweep",
    )

    result.assert_refused(Reason.BUDGET)
    assert "14.00" in result.refusal.message


def test_spend_from_a_previous_month_does_not_count_against_this_month(harness):
    ledger = harness.ledger()
    spend(ledger, "claude-opus-5", input_tokens=10_000_000, output_tokens=10_000_000,
          at="2026-09-20T12:00:00+00:00")  # $300 in September

    result = harness.run(Budget(prices=PRICES, monthly_ceiling_usd=15.0, now=OCTOBER), job="sweep")

    result.assert_allowed()


def test_on_ceiling_warn_records_a_degraded_check_instead_of_refusing(harness):
    """`budget.on_ceiling: warn` is a documented config value (§6.3), so it must
    do something other than refuse — and must not pass silently."""
    ledger = harness.ledger()
    spend(ledger, "claude-sonnet-5", input_tokens=2_000_000, output_tokens=1_000_000)

    result = harness.run(
        Budget(prices=PRICES, monthly_ceiling_usd=15.0, per_run_estimate_usd=2.0,
               on_ceiling="warn", now=OCTOBER),
        job="sweep",
    )

    result.assert_allowed()
    assert result.degraded is not None


def test_an_unknown_model_in_the_ledger_degrades_rather_than_undercounting(harness):
    """An unpriced model must not silently contribute $0 — that would let spend
    drift past the ceiling unnoticed, which is the one thing this guard exists
    to prevent."""
    ledger = harness.ledger()
    spend(ledger, "some-model-added-later", input_tokens=5_000_000, output_tokens=5_000_000)

    result = harness.run(Budget(prices=PRICES, monthly_ceiling_usd=15.0, now=OCTOBER), job="sweep")

    assert result.degraded is not None
    assert "some-model-added-later" in result.degraded.message
