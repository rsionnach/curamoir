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
    """

    job: str
    repo: Optional[Path] = None
