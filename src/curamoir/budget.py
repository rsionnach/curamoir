"""E0-R4: the spend ceiling, and the price table it is computed from.

Cost is DERIVED at read time from recorded tokens, never stored as a dollar
figure. That is what lets E11-R2 "change the reported cost without re-running
jobs" when a price changes, and it means a stale price is a correctable error
rather than one baked into history.

G4 is "make the cost of doing this visible and capped". This module is the
"capped" half; /curamoir:cost (E11-R2) is the "visible" half.
"""

from datetime import datetime, timezone

from curamoir.guards.context import unusable_repo
from curamoir.guards.reasons import Reason
from curamoir.guards.result import Degraded, Refusal
from curamoir.ledger import Ledger, parse_ledger_timestamp

PER_MILLION = 1_000_000


def month_to_date(ledger, prices, now):
    """Total USD for ledger entries dated in `now`'s calendar month (UTC).

    Shared by the E0-R4 budget guard and E1-R3's /curamoir:status so the two can
    never disagree about what a month's spend is. Raises UnknownModel for an
    entry naming a model the price table does not cover.
    """
    total = 0.0
    for entry in ledger.entries():
        if entry.get("input_tokens") is None and entry.get("output_tokens") is None:
            continue
        when = parse_ledger_timestamp(entry.get("at"))
        if when is None:
            # Tokens with no usable date is unattributable spend, not $0. Left
            # uncounted it would read as "nothing spent" with the unreadable
            # tally at zero, so nothing downstream would hedge the total.
            ledger.note_unaccounted()
            continue
        if (when.year, when.month) != (now.year, now.month):
            continue
        tokens = _token_counts(entry)
        if tokens is None:
            ledger.note_unaccounted()
            continue
        total += prices.cost(entry.get("model"), *tokens)
    return total


def _token_counts(entry):
    """`(input, output)` as numbers, or None if either is not one.

    A writer emitting "1000" for 1000 is a plausible hook bug, and a str reaches
    the price table as a TypeError from multiplication rather than as anything
    the degraded path recognises.
    """
    counts = []
    for key in ("input_tokens", "output_tokens"):
        value = entry.get(key) or 0
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        counts.append(value)
    return tuple(counts)


class UnknownModel(Exception):
    """A ledger entry names a model the price table does not cover."""


class PriceTable:
    """USD per million tokens, by model.

    Ships with defaults and a "verify prices" note (E11-R1): published prices
    change, and a table that silently rots understates spend.
    """

    def __init__(self, prices):
        self._prices = dict(prices)

    def cost(self, model, input_tokens, output_tokens):
        """Cost in USD. Raises UnknownModel for an absent or malformed entry.

        Malformed, not just absent: a config typo (`ouput`) must surface as
        UnknownModel naming the model, rather than as a KeyError the Gateway
        turns into GUARD_ERROR with a dict key for a message.
        """
        rate = self._prices.get(model)
        if not isinstance(rate, dict):
            raise UnknownModel(model)
        try:
            rate = {"input": float(rate["input"]), "output": float(rate["output"])}
        except (KeyError, TypeError, ValueError):
            raise UnknownModel(model) from None
        return (
            input_tokens * rate["input"] / PER_MILLION
            + output_tokens * rate["output"] / PER_MILLION
        )


class Budget:
    """Refuses a job whose projected cost would breach the monthly ceiling.

    `on_ceiling` mirrors the config value of the same name (§6.3): "pause"
    refuses, "warn" allows but records a degraded check, so the documented
    value does something other than pass silently.
    """

    def __init__(
        self,
        prices,
        monthly_ceiling_usd,
        per_run_estimate_usd=0.0,
        on_ceiling="pause",
        now=None,
    ):
        self._prices = prices
        self._ceiling = monthly_ceiling_usd
        self._per_run = per_run_estimate_usd
        self._on_ceiling = on_ceiling
        self._now = now or (lambda: datetime.now(timezone.utc))

    def __call__(self, context):
        unusable = unusable_repo(context)
        if unusable is not None:
            return Degraded(
                reason=Reason.BUDGET,
                message=(
                    f"{unusable}, so the spend ceiling did NOT run. "
                    "Resolve the repository root before attempting the job."
                ),
            )

        ledger = Ledger(context.repo / ".curamoir" / "state" / "ledger.jsonl")
        try:
            spent = month_to_date(ledger, self._prices, self._now())
            unreadable = ledger.unreadable_lines()
        except UnknownModel as unknown:
            # Do NOT treat an unpriced model as $0. Undercounting is the one
            # failure this guard exists to prevent, so say the cap is unenforced
            # rather than quietly enforcing a wrong one.
            return Degraded(
                reason=Reason.BUDGET,
                message=(
                    f"ledger names model {unknown.args[0]!r}, which the price table "
                    "does not cover; month-to-date spend could not be computed and "
                    "the ceiling was NOT enforced. Add the model to the price table."
                ),
            )

        if unreadable is None:
            # The ledger could not be read at all, so there is no lower bound —
            # not even zero. Enforcing a ceiling against a total we never
            # obtained is the undercount this guard exists to prevent.
            return Degraded(
                reason=Reason.BUDGET,
                message=(
                    "the ledger could not be read, so month-to-date spend is "
                    "unknown and the monthly ceiling could not be enforced at all. "
                    "Inspect .curamoir/state/ledger.jsonl."
                ),
            )

        # Compare FIRST, before the unreadable-line degradation below. A lower
        # bound that already exceeds the ceiling is PROOF of a breach, not
        # uncertainty about one, so on_ceiling must still be honoured.
        projected = spent + self._per_run
        if projected > self._ceiling:
            suffix = (
                f" (and {unreadable} unreadable ledger line(s), so the real total "
                "is higher still)" if unreadable else ""
            )
            message = (
                f"month-to-date spend ${spent:.2f} plus an estimated "
                f"${self._per_run:.2f} for this run would reach ${projected:.2f}, "
                f"breaching the ${self._ceiling:.2f} monthly ceiling{suffix}."
            )
            if self._on_ceiling == "warn":
                return Degraded(reason=Reason.BUDGET,
                                message=message + " Configured to warn, so proceeding.")
            return Refusal(reason=Reason.BUDGET, message=message)

        if unreadable:
            # Under the ceiling on what we could read, but a skipped line could
            # have recorded any amount, so say the ceiling is UNCONFIRMED rather
            # than claiming it was enforced.
            return Degraded(
                reason=Reason.BUDGET,
                message=(
                    f"{unreadable} unreadable ledger line(s); month-to-date spend "
                    f"of ${spent:.2f} is a lower bound, so the ${self._ceiling:.2f} "
                    "ceiling could not be confirmed. Inspect "
                    ".curamoir/state/ledger.jsonl."
                ),
            )

        return None



