"""E0-R4: the spend ceiling, and the price table it is computed from.

Cost is DERIVED at read time from recorded tokens, never stored as a dollar
figure. That is what lets E11-R2 "change the reported cost without re-running
jobs" when a price changes, and it means a stale price is a correctable error
rather than one baked into history.

G4 is "make the cost of doing this visible and capped". This module is the
"capped" half; /curamoir:cost (E11-R2) is the "visible" half.
"""

from datetime import datetime, timezone

from curamoir.guards.reasons import Reason
from curamoir.guards.result import Degraded, Refusal
from curamoir.ledger import Ledger

PER_MILLION = 1_000_000


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
        """Cost in USD, or UnknownModel for an absent or malformed entry.

        A mistyped key ("ouput") used to raise KeyError from inside the guard,
        which the Gateway turned into GUARD_ERROR — every job refused, with a
        message naming a dict key rather than the config file.
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
        if context.repo is None:
            return None

        ledger = Ledger(context.repo / ".curamoir" / "state" / "ledger.jsonl")
        try:
            spent = self._month_to_date(ledger)
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

        projected = spent + self._per_run
        if projected <= self._ceiling:
            return None

        message = (
            f"month-to-date spend ${spent:.2f} plus an estimated ${self._per_run:.2f} "
            f"for this run would reach ${projected:.2f}, breaching the "
            f"${self._ceiling:.2f} monthly ceiling."
        )
        if self._on_ceiling == "warn":
            return Degraded(reason=Reason.BUDGET, message=message + " Configured to warn, so proceeding.")
        return Refusal(reason=Reason.BUDGET, message=message)

    def _month_to_date(self, ledger):
        now = self._now()
        total = 0.0
        for entry in ledger.entries():
            if entry.get("input_tokens") is None and entry.get("output_tokens") is None:
                continue
            when = _parse(entry.get("at"))
            if when is None or (when.year, when.month) != (now.year, now.month):
                continue
            total += self._prices.cost(
                entry.get("model"),
                entry.get("input_tokens") or 0,
                entry.get("output_tokens") or 0,
            )
        return total


def _parse(timestamp):
    try:
        when = datetime.fromisoformat(timestamp)
    except (TypeError, ValueError):
        return None
    # Normalise to UTC. Attributing by the entry's own offset put a
    # 2026-11-01T01:00+13:00 record (2026-10-31T12:00Z) into November, so a
    # ledger merged from CI or another machine would lose and then double-count
    # it across the month boundary.
    when = when.replace(tzinfo=timezone.utc) if when.tzinfo is None else when
    return when.astimezone(timezone.utc)
