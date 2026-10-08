"""A guard whose subject is the filesystem cannot check "which filesystem?".

`Degraded` exists for a check that could not complete — a `gh` outage must not
halt a month of scheduled work. But four of the five guards read the repo, and
when they did not know which repo to read they returned None, which the gateway
reads as a clean pass. So a context with no repo ran the side effect with the
kill switch, the lock, the default-branch check and the spend ceiling all
silently disengaged, and recorded nothing: precisely the outcome `Degraded`'s
docstring calls "how a guardrail turns out never to have run".

Two layers close it, because the first one is only a signature:

1. `repo` is required. A caller who cannot resolve the root must fail closed
   itself rather than build a context that disables the guard layer.
2. Each repo-reading guard still answers `Degraded` for an explicit None, so
   the bypass is unreachable even from a caller that defeats layer 1.
"""

import pytest

from curamoir.budget import Budget, PriceTable
from curamoir.guards import Degraded, JobContext
from curamoir.guards.default_branch import DefaultBranchGuard
from curamoir.guards.kill_switch import KillSwitch
from curamoir.guards.lock import SupervisorLock

PRICES = PriceTable({"claude-sonnet-5": {"input": 2.0, "output": 10.0}})

REPO_READING_GUARDS = [
    pytest.param(KillSwitch(), id="kill_switch"),
    pytest.param(SupervisorLock(), id="lock"),
    pytest.param(DefaultBranchGuard(), id="default_branch"),
    pytest.param(Budget(prices=PRICES, monthly_ceiling_usd=15.0), id="budget"),
]


def test_a_job_context_cannot_be_built_without_naming_its_repo():
    """The default was `None`, so forgetting the keyword disabled four guards."""
    with pytest.raises(TypeError):
        JobContext(job="sweep")


@pytest.mark.parametrize("guard", REPO_READING_GUARDS)
def test_a_guard_that_does_not_know_the_repo_degrades_rather_than_allows(guard):
    """Allowing is fine; allowing SILENTLY is the bug. Each must say so."""
    verdict = guard(JobContext(job="sweep", repo=None))

    assert isinstance(verdict, Degraded), (
        f"{type(guard).__name__} returned {verdict!r} for an unknown repo, "
        "which the gateway cannot distinguish from a clean pass"
    )
    assert "repo" in verdict.message.lower()
