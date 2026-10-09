"""The guard layer's front door: the vocabulary, the chokepoint, and the guards.

Epic 10's runner has to assemble all five guards into a `Gateway`. Exporting
only the types meant reaching into five submodules to do it — the front door
offered what you check against and withheld what you instantiate.

`unusable_repo` is here too, because `context.py` names it as the predicate a
caller needs in order to fail closed on a root it could not resolve (cm-7ck),
and `curamoir.budget` already imports it across the package boundary.
"""

from curamoir.budget import Budget
from curamoir.guards.context import JobContext, unusable_repo
from curamoir.guards.default_branch import DefaultBranchGuard
from curamoir.guards.gateway import Gateway
from curamoir.guards.kill_switch import KillSwitch
from curamoir.guards.lock import SupervisorLock
from curamoir.guards.pr_cap import OpenPrCap
from curamoir.guards.reasons import Reason
from curamoir.guards.result import Degraded, Refusal

__all__ = [
    # the vocabulary
    "Degraded",
    "Reason",
    "Refusal",
    # the chokepoint and what it is told
    "Gateway",
    "JobContext",
    "unusable_repo",
    # the five guards, in the order the spec lists them
    "KillSwitch",
    "SupervisorLock",
    "DefaultBranchGuard",
    "OpenPrCap",
    "Budget",
]
