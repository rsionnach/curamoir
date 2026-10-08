"""`repo is None` was one value of a question with several wrong answers.

Making `repo` required and degrading on `None` closed the default, which was the
reachable case. It did not close the CLASS: three of the four repo-reading
guards tested `is None` and nothing else, so a path that is not a repository
still produced a clean, unrecorded pass — the exact outcome the Degraded
vocabulary exists to prevent.

A repo moved, renamed or deleted between being scheduled and being run gives
both cases below, and an unattended job is exactly where that happens.

`Path("")` — the shape of `Path(os.environ.get("REPO_ROOT", ""))` — belongs to
the same family and is deliberately NOT tested here, because it cannot be
caught here: pathlib resolves it to `.` before a guard sees it, so it is
indistinguishable from a deliberate `--repo .`. That one belongs to whoever
resolves the root, which is the runner; it is recorded on bead cm-7ck.

My own docstring in `test_unknown_repo.py` claimed "the bypass is unreachable
even from a caller that defeats layer 1". It was unreachable for one value.
"""

from pathlib import Path

import pytest

from curamoir.budget import Budget, PriceTable
from curamoir.guards import Degraded, JobContext
from curamoir.guards.default_branch import DefaultBranchGuard
from curamoir.guards.kill_switch import KillSwitch
from curamoir.guards.lock import SupervisorLock

PRICES = PriceTable({"claude-sonnet-5": {"input": 2.0, "output": 10.0}})

GUARDS = [
    pytest.param(KillSwitch(), id="kill_switch"),
    pytest.param(SupervisorLock(), id="lock"),
    pytest.param(DefaultBranchGuard(), id="default_branch"),
    pytest.param(Budget(prices=PRICES, monthly_ceiling_usd=15.0), id="budget"),
]


@pytest.fixture
def not_a_repo(tmp_path, request):
    if request.param == "missing":
        return tmp_path / "gone"
    if request.param == "a_file":
        target = tmp_path / "a-file"
        target.write_text("not a directory\n", encoding="utf-8")
        return target
    raise AssertionError(request.param)


@pytest.mark.parametrize("guard", GUARDS)
@pytest.mark.parametrize(
    "not_a_repo", ["missing", "a_file"], indirect=True
)
def test_a_repo_that_is_not_a_directory_degrades_rather_than_allowing(guard, not_a_repo):
    verdict = guard(JobContext(job="sweep", repo=not_a_repo))

    assert verdict is not None, (
        f"{type(guard).__name__} returned a clean, unrecorded pass for "
        f"{not_a_repo!r}"
    )
    assert isinstance(verdict, Degraded), verdict


@pytest.mark.parametrize("guard", GUARDS)
def test_a_real_repository_is_still_judged_normally(guard, tmp_path):
    """The predicate must not refuse every job it is meant to let through."""
    (tmp_path / ".curamoir" / "state").mkdir(parents=True)

    verdict = guard(JobContext(job="sweep", repo=tmp_path))

    assert not isinstance(verdict, Degraded) or "repo" not in verdict.message.lower()
