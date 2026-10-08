"""What a command verdict is made of: the outcome types and why-codes.

`Decision`, `Rule` and `Category` are one vocabulary and always travel together,
mirroring `guards/` where the dataclass results sit beside their `Reason` enum.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class Category(str, Enum):
    """Why a command was refused.

    Structured so callers and tests assert the REASON rather than a prose
    substring: `$R -rf /` refused as UNVERIFIABLE and `foo` refused as
    NOT_ALLOWLISTED are different outcomes, and DEC-9 turns on the difference.
    """

    #: Construction whose effect cannot be determined by reading it — command
    #: substitution, a shell `-c`, a variable reference, an interpreter.
    UNVERIFIABLE = "unverifiable"
    #: A recognisable command that cúramóir's jobs have no business running.
    NOT_ALLOWLISTED = "not_allowlisted"
    #: Could not be tokenised at all (unbalanced quoting).
    UNPARSEABLE = "unparseable"
    #: Allowlisted program, destructive use — caught by the second pass.
    DESTRUCTIVE = "destructive"

    def __str__(self):
        return self.value


@dataclass(frozen=True)
class Rule:
    """One matched destructive-command rule: why, and the safe alternative.

    Internal to the destructive pass. `deny` is implicit — a matched rule always
    refuses — which is why this is a separate type from `Decision` rather than a
    `Decision` with a different meaning.
    """

    reason: str
    alternative: str


@dataclass(frozen=True)
class Decision:
    """A verdict on one command, as returned to the hook.

    `category` is the structured half and the half callers assert on: "refused
    because the guard cannot see inside it" and "refused because it is not on
    the list" are different outcomes (DEC-9), and prose is not a safe way to
    tell them apart.
    """

    deny: bool
    reason: str = ""
    alternative: str = ""
    category: Optional[Category] = None
