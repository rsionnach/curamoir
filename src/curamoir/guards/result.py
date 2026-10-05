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
