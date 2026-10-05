"""E0-R6: the open-PR cap.

"A returning developer must never face a wall of PRs." Same instinct as the
filing rate limit (E6-R5): the plugin exists to reduce re-entry cost, so it
must not greet you with a backlog of its own making.

Implemented in the guard layer so every PR-opening job inherits it, not just
the sweep.
"""

from curamoir.guards import Reason
from curamoir.guards.pr_cap import OpenPrCap


def branches(*names):
    return lambda: list(names)


def test_no_open_prs_allows_a_new_one(harness):
    result = harness.run(OpenPrCap(open_pr_branches=branches(), max_open_prs=3), job="sweep")

    result.assert_allowed()


def test_under_the_cap_allows_a_new_one(harness):
    result = harness.run(
        OpenPrCap(open_pr_branches=branches("curamoir/sweep-1", "curamoir/sweep-2"), max_open_prs=3),
        job="sweep",
    )

    result.assert_allowed()


def test_at_the_cap_refuses_with_reason_pr_cap(harness):
    result = harness.run(
        OpenPrCap(
            open_pr_branches=branches("curamoir/sweep-1", "curamoir/sweep-2", "curamoir/sweep-3"),
            max_open_prs=3,
        ),
        job="sweep",
    )

    result.assert_refused(Reason.PR_CAP)
    assert result.performed is False


def test_only_curamoir_branches_count_towards_the_cap(harness):
    """The developer's own open PRs are not cúramóir's to ration."""
    result = harness.run(
        OpenPrCap(
            open_pr_branches=branches("feature/login", "fix/typo", "dependabot/npm/react"),
            max_open_prs=3,
        ),
        job="sweep",
    )

    result.assert_allowed()


def test_the_configured_prefix_is_honoured(harness):
    result = harness.run(
        OpenPrCap(
            open_pr_branches=branches("janitor/a", "janitor/b", "janitor/c"),
            max_open_prs=3,
            branch_prefix="janitor/",
        ),
        job="sweep",
    )

    result.assert_refused(Reason.PR_CAP)


def test_refusal_message_names_the_count_and_the_cap(harness):
    result = harness.run(
        OpenPrCap(open_pr_branches=branches("curamoir/a", "curamoir/b"), max_open_prs=2),
        job="sweep",
    )

    assert "2" in result.refusal.message


def test_an_unreachable_pr_list_degrades_rather_than_opening_an_unbounded_pr(harness):
    """gh is a network dependency. We cannot show the cap is satisfied, so we
    do not claim it is — but neither do we refuse a month of work over an
    outage. Allow, and record that the cap was not enforced."""
    def exploding():
        raise RuntimeError("gh unreachable")

    result = harness.run(OpenPrCap(open_pr_branches=exploding, max_open_prs=3), job="sweep")

    assert result.degraded is not None
    assert "not enforced" in result.degraded.message.lower()
