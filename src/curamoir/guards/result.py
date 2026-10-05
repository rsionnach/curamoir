"""What a guard returns."""

from dataclasses import dataclass

from curamoir.guards.reasons import Reason


@dataclass(frozen=True)
class Refusal:
    """A guard's decision to stop a side effect.

    `reason` is for machines (ledger, brief, tests); `message` is for the human
    or agent who needs to know what to do instead.
    """

    reason: Reason
    message: str


@dataclass(frozen=True)
class Degraded:
    """A guard that could not complete its check.

    Allows the side effect, because refusing on a failed lookup would let a
    GitHub outage halt a month of scheduled work. Recorded, because allowing
    silently is how a guardrail turns out never to have run.

    `reason` names the check that degraded, so the brief can say which
    protection was unavailable rather than merely that something was.
    """

    reason: Reason
    message: str
