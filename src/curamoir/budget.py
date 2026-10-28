"""E0-R4: the spend ceiling, and the price table it is computed from.

Cost is DERIVED at read time from recorded tokens, never stored as a dollar
figure. That is what lets E11-R2 "change the reported cost without re-running
jobs" when a price changes, and it means a stale price is a correctable error
rather than one baked into history.

G4 is "make the cost of doing this visible and capped". This module is the
"capped" half; /curamoir:cost (E11-R2) is the "visible" half.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

from curamoir.guards.context import unusable_repo
from curamoir.guards.reasons import Reason
from curamoir.guards.result import Degraded, Refusal
from curamoir.ledger import parse_ledger_timestamp, repo_ledger

PER_MILLION = 1_000_000

#: The ledger fields a line's prompt-cache tokens are recorded in, named as
#: in the Messages API's `usage` (cm-2ui). Optional: a line without them
#: (every line before cm-2ui, and a call that used no cache) has none.
CACHE_WRITE_FIELD = "cache_creation_input_tokens"
CACHE_READ_FIELD = "cache_read_input_tokens"
#: The split of a line's cache writes, when the usage gave it
#: (usage.cache_creation's ephemeral_5m_input_tokens and
#: ephemeral_1h_input_tokens, cm-1et). Optional; only the 5-minute share
#: changes the price, since every write not shown to be 5-minute is priced
#: as 1-hour, and the 1-hour count is kept for the record.
CACHE_WRITE_5M_FIELD = "cache_creation_5m_input_tokens"
CACHE_WRITE_1H_FIELD = "cache_creation_1h_input_tokens"
TOKEN_FIELDS = ("input_tokens", "output_tokens", CACHE_WRITE_FIELD, CACHE_READ_FIELD,
                CACHE_WRITE_5M_FIELD)

#: Cache tokens priced as multiples of the model's input rate when its price
#: entry does not set `cache_write` (5-minute), `cache_write_1h` or
#: `cache_read`: Anthropic's standard multipliers (user decisions 2026-10-28,
#: cm-2ui and cm-1et). A write not shown to be a 5-minute one is priced as a
#: 1-hour one, the dearer: the first dogfood sweep's CLI cost reconciled
#: exactly at 2x, so Claude Code writes the 1-hour cache.
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_WRITE_1H_MULTIPLIER = 2.0
CACHE_READ_MULTIPLIER = 0.1

#: The token count a writer records for a model call that ran but reported
#: no usage (it timed out, crashed, or printed something that was not JSON).
#: Such a call still spent money, so it must not read as $0: month_to_date
#: counts a line carrying this marker as `unrecorded`, which makes the
#: month's total a stated lower bound. A named marker, not any non-number,
#: so the rule does not rest on an accident of type checking, and kept
#: apart from `unaccounted`: a writer bug that emits "1000" is damage worth
#: inspecting the ledger for, but this line is exactly what was meant.
UNRECORDED = "unrecorded"


@dataclass(frozen=True)
class Spend:
    """What a month's ledger read came to, and how much of it was unusable.

    Three numbers, because two of them are caveats on the first: either
    makes `usd` a LOWER BOUND rather than a total. `unaccounted` counts
    entries that carried token counts but could not be priced or dated,
    which is damage someone should look for in the ledger. `unrecorded`
    counts entries whose counts are UNRECORDED: a model call that ran but
    reported no usage. That line reads perfectly well, so it is kept apart,
    and the caveat for it must not send anyone hunting for corruption.

    Returned rather than recorded on the ledger. It was briefly stored there,
    which gave one field two producers with different lifecycles and no correct
    reset: zeroing it on each read discarded what this pass found, and not
    zeroing it made two reads on one handle report phantom unreadable lines.
    Both are wrong, and neither is wrong if the count never lands on the handle.
    """

    usd: float
    unaccounted: int = 0
    unrecorded: int = 0


def month_to_date(ledger, prices, now):
    """Spend for ledger entries dated in `now`'s calendar month (UTC).

    Shared by the E0-R4 budget guard and E1-R3's /curamoir:status so the two can
    never disagree about what a month's spend is. Raises UnknownModel for an
    entry naming a model the price table does not cover.
    """
    total = 0.0
    unaccounted = 0
    unrecorded = 0
    for entry in ledger.entries():
        if all(entry.get(field) is None for field in TOKEN_FIELDS):
            continue
        when = parse_ledger_timestamp(entry.get("at"))
        if when is None:
            # Tokens with no usable date is unattributable spend, not $0. Left
            # uncounted it would read as "nothing spent" with the unreadable
            # tally at zero, so nothing downstream would hedge the total.
            unaccounted += 1
            continue
        if (when.year, when.month) != (now.year, now.month):
            continue
        if UNRECORDED in (entry.get(field) for field in TOKEN_FIELDS):
            unrecorded += 1
            continue
        tokens = _token_counts(entry)
        if tokens is None:
            unaccounted += 1
            continue
        if not any(tokens):
            # No model call ran, so there is nothing to price, whatever the
            # line names as its model (or none). Pricing it would raise
            # UnknownModel for a line with no model, and the guard would
            # then stop enforcing the ceiling for the rest of the month
            # over a line that spent nothing.
            continue
        input_tokens, output_tokens, cache_write, cache_read, cache_write_5m = tokens
        total += prices.cost(entry.get("model"), input_tokens, output_tokens,
                             cache_write_tokens=cache_write, cache_read_tokens=cache_read,
                             cache_write_5m_tokens=cache_write_5m)
    return Spend(usd=total, unaccounted=unaccounted, unrecorded=unrecorded)


def combined_shortfall(unreadable, unaccounted):
    """How much of the ledger did not reach the total, or None if unknown.

    Unreadable LINES plus unattributable ENTRIES. Two sources, one number,
    because every consumer asks the same question of it: is this total a lower
    bound? Shared by the budget guard and `/curamoir:status` so the two cannot
    word the same caveat from different arithmetic.
    """
    if unreadable is None:
        return None
    return unreadable + unaccounted


def lower_bound_causes(unreadable, unrecorded):
    """Why a month's total is a lower bound, in words, or "" if it is not.

    The two causes are named apart because they ask different things of the
    reader: an unreadable line is damage to inspect the ledger for; a call
    with no usage is a readable record of spend nobody measured. Shared by
    the budget guard and `/curamoir:status`, so both word it alike.
    """
    causes = []
    if unreadable:
        causes.append(f"{unreadable} unreadable ledger line(s)")
    if unrecorded:
        causes.append(f"{unrecorded} ledger line(s) record a model call with no usage")
    return " and ".join(causes)


def _token_counts(entry):
    """`(input, output, cache write, cache read, 5-minute cache write)` as
    numbers, a missing one as 0, or None if any is not a number.

    A writer emitting "1000" for 1000 is a plausible hook bug, and a str
    reaches the price table as a TypeError from multiplication rather than
    as anything the degraded path recognises. (UNRECORDED never gets here:
    month_to_date counts it first, as `unrecorded`.)
    """
    counts = []
    for key in TOKEN_FIELDS:
        value = entry.get(key) or 0
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        counts.append(value)
    return tuple(counts)


def _rate(value):
    """A price as a float; TypeError for a bool or None, which float() would
    take as 1.0 or reject unclearly."""
    if isinstance(value, bool) or value is None:
        raise TypeError(f"not a price: {value!r}")
    return float(value)


class UnknownModel(Exception):
    """A ledger entry names a model the price table does not cover."""


class PriceTable:
    """USD per million tokens, by model.

    Ships with defaults and a "verify prices" note (E11-R1): published prices
    change, and a table that silently rots understates spend.
    """

    def __init__(self, prices):
        self._prices = dict(prices)

    def cost(self, model, input_tokens, output_tokens, cache_write_tokens=0,
             cache_read_tokens=0, cache_write_5m_tokens=0):
        """Cost in USD. Raises UnknownModel for an absent or malformed entry.

        Malformed, not just absent: a config typo (`ouput`) must surface as
        UnknownModel naming the model, rather than as a KeyError the Gateway
        turns into GUARD_ERROR with a dict key for a message. The cache
        rates `cache_write` (5-minute), `cache_write_1h` and `cache_read` are
        optional (the CACHE_*_MULTIPLIERs of the input rate when absent), and
        one that is given but is not a number is malformed too.

        `cache_write_tokens` is every cache write; `cache_write_5m_tokens`,
        capped at it, is the share shown to be 5-minute writes, and the rest
        is priced as 1-hour writes.
        """
        entry = self._prices.get(model)
        if not isinstance(entry, dict):
            raise UnknownModel(model)
        try:
            rate = {side: _rate(entry[side]) for side in ("input", "output")}
            rate["cache_write"] = (_rate(entry["cache_write"]) if "cache_write" in entry
                                   else rate["input"] * CACHE_WRITE_MULTIPLIER)
            rate["cache_write_1h"] = (_rate(entry["cache_write_1h"])
                                      if "cache_write_1h" in entry
                                      else rate["input"] * CACHE_WRITE_1H_MULTIPLIER)
            rate["cache_read"] = (_rate(entry["cache_read"]) if "cache_read" in entry
                                  else rate["input"] * CACHE_READ_MULTIPLIER)
        except (KeyError, TypeError, ValueError):
            raise UnknownModel(model) from None
        five_minute = min(cache_write_5m_tokens, cache_write_tokens)
        return (
            input_tokens * rate["input"]
            + output_tokens * rate["output"]
            + five_minute * rate["cache_write"]
            + (cache_write_tokens - five_minute) * rate["cache_write_1h"]
            + cache_read_tokens * rate["cache_read"]
        ) / PER_MILLION


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

        ledger = repo_ledger(context.repo)
        try:
            spend = month_to_date(ledger, self._prices, self._now())
            spent = spend.usd
            # One number for the caller: both "a line would not parse" and "an
            # entry could not be attributed" mean the same thing here — some of
            # this file did not reach the total, so the total is a lower bound.
            unreadable = combined_shortfall(ledger.unreadable_lines(), spend.unaccounted)
            # Kept apart from `unreadable`: also a lower bound, but a readable
            # line, so its wording must not point at the ledger as damaged.
            unrecorded = spend.unrecorded
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
        causes = lower_bound_causes(unreadable, unrecorded)
        if projected > self._ceiling:
            suffix = f" (and {causes}, so the real total is higher still)" if causes else ""
            message = (
                f"month-to-date spend ${spent:.2f} plus an estimated "
                f"${self._per_run:.2f} for this run would reach ${projected:.2f}, "
                f"breaching the ${self._ceiling:.2f} monthly ceiling{suffix}."
            )
            if self._on_ceiling == "warn":
                return Degraded(reason=Reason.BUDGET,
                                message=message + " Configured to warn, so proceeding.")
            return Refusal(reason=Reason.BUDGET, message=message)

        if causes:
            # Under the ceiling on what we could read, but a skipped line could
            # have recorded any amount, and a call with no usage spent an amount
            # nobody measured, so say the ceiling is UNCONFIRMED rather than
            # claiming it was enforced. Only damage is worth inspecting for.
            inspect = " Inspect .curamoir/state/ledger.jsonl." if unreadable else ""
            return Degraded(
                reason=Reason.BUDGET,
                message=(
                    f"{causes}; month-to-date spend of ${spent:.2f} is a lower "
                    f"bound, so the ${self._ceiling:.2f} ceiling could not be "
                    f"confirmed.{inspect}"
                ),
            )

        return None



