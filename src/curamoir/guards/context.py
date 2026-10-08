"""What a guard is told about the side effect being attempted."""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class JobContext:
    """One attempted side effect, described well enough to judge it.

    Guards need more than a job name: E0-R1 must find the default branch, E0-R3
    must look for `.curamoir/PAUSE`, E0-R4 must read the ledger. They all need
    the repo. `job` is what the ledger records; `repo` is what guards inspect.

    `repo` is required, and has no default on purpose. Four of the five guards
    read the repo and can say nothing without it, so a context that omitted it
    ran the side effect with the kill switch, the lock, the default-branch check
    and the spend ceiling all disengaged — and recorded nothing. A caller that
    cannot resolve the root must fail closed itself rather than build a context
    that cannot be guarded. `None` remains expressible, so that a caller who
    means it gets four `Degraded` records instead of a silent pass.
    """

    job: str
    repo: Optional[Path]
