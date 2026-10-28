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

def OCTOBER():
    """A fixed clock, so month attribution is not a function of the test's date."""
    return datetime(2026, 10, 5, tzinfo=timezone.utc)


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


# --- cache tokens (cm-2ui, user decision 2026-10-28) -------------------------------
#
# Own rates, defaulted: a cache write at 1.25x the model's input rate and a
# cache read at 0.1x, Anthropic's standard multipliers, unless the model's
# entry sets `cache_write` / `cache_read` itself.

import pytest  # noqa: E402

from curamoir.budget import UNRECORDED, UnknownModel, month_to_date  # noqa: E402
from curamoir.ledger import Ledger  # noqa: E402


def test_cache_tokens_default_to_the_standard_multiples_of_the_input_rate():
    # sonnet input is $2.00/M: a write is $2.50/M, a read $0.20/M.
    assert PRICES.cost("claude-sonnet-5", 0, 0, cache_write_tokens=1_000_000) == pytest.approx(2.5)
    assert PRICES.cost("claude-sonnet-5", 0, 0, cache_read_tokens=1_000_000) == pytest.approx(0.2)


def test_an_explicit_cache_rate_overrides_the_default():
    prices = PriceTable({"m": {"input": 2.0, "output": 10.0,
                               "cache_write": 4.0, "cache_read": 1.0}})

    assert prices.cost("m", 0, 0, cache_write_tokens=1_000_000,
                       cache_read_tokens=1_000_000) == pytest.approx(5.0)


@pytest.mark.parametrize("rate", ["cheap", None, True])
def test_a_malformed_cache_rate_is_an_unknown_model_not_a_guess(rate):
    prices = PriceTable({"m": {"input": 2.0, "output": 10.0, "cache_read": rate}})

    with pytest.raises(UnknownModel):
        prices.cost("m", 1, 1, cache_read_tokens=1)


def cache_line(ledger, **fields):
    ledger.record(kind="job", job="sweep", reason=None, message="", outcome="success",
                  model="claude-sonnet-5", at="2026-10-02T12:00:00+00:00",
                  **{"input_tokens": 0, "output_tokens": 0, **fields})


def test_month_to_date_prices_a_lines_cache_tokens(harness):
    ledger = Ledger(harness.repo / ".curamoir" / "state" / "ledger.jsonl")
    cache_line(ledger, input_tokens=1_000_000, cache_creation_input_tokens=1_000_000,
               cache_read_input_tokens=10_000_000)

    spend = month_to_date(ledger, PRICES, OCTOBER())

    # $2.00 input + $2.50 write + $2.00 read
    assert spend.usd == pytest.approx(6.5) and spend.unaccounted == 0


def test_a_line_of_only_cache_reads_is_still_priced(harness):
    ledger = Ledger(harness.repo / ".curamoir" / "state" / "ledger.jsonl")
    cache_line(ledger, cache_read_input_tokens=1_000_000)

    assert month_to_date(ledger, PRICES, OCTOBER()).usd == pytest.approx(0.2)


def test_a_line_from_before_cache_tokens_were_recorded_reads_as_before(harness):
    ledger = Ledger(harness.repo / ".curamoir" / "state" / "ledger.jsonl")
    spend(ledger, "claude-sonnet-5", 1_000_000, 100_000)

    assert month_to_date(ledger, PRICES, OCTOBER()).usd == pytest.approx(3.0)


def test_a_cache_count_that_is_not_a_number_is_unaccounted_not_zero(harness):
    ledger = Ledger(harness.repo / ".curamoir" / "state" / "ledger.jsonl")
    cache_line(ledger, input_tokens=10, cache_read_input_tokens="lots")

    spend_read = month_to_date(ledger, PRICES, OCTOBER())

    assert spend_read.unaccounted == 1 and spend_read.usd == 0


def test_an_unrecorded_cache_count_makes_the_line_unrecorded(harness):
    ledger = Ledger(harness.repo / ".curamoir" / "state" / "ledger.jsonl")
    cache_line(ledger, input_tokens=10, cache_creation_input_tokens=UNRECORDED)

    assert month_to_date(ledger, PRICES, OCTOBER()).unrecorded == 1
