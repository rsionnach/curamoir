"""E0-R1: cúramóir never writes to the default branch.

All changes go to a branch with the configured prefix and are opened as a PR.
Enforced in code, not prompt — which is exactly what this test file proves: no
model is involved in any of it.

The default branch is DETECTED, never assumed to be `main`. A repo whose default
is `master`, `trunk` or `develop` must be protected just as well.
"""

from curamoir.guards import Reason
from curamoir.guards.default_branch import DefaultBranchGuard


def test_writing_from_the_default_branch_is_refused(harness):
    assert harness.current_branch() == "main"

    result = harness.run(DefaultBranchGuard(), job="sweep")

    result.assert_refused(Reason.DEFAULT_BRANCH)
    assert result.performed is False


def test_writing_from_a_curamoir_branch_is_allowed(harness):
    harness.checkout("curamoir/sweep-2026-10-05")

    result = harness.run(DefaultBranchGuard(), job="sweep")

    result.assert_allowed()
    assert result.performed is True


def test_a_branch_without_the_configured_prefix_is_refused(harness):
    """Not merely "not the default branch" — cúramóir's own work belongs on its
    own prefix, so a stray branch is refused too. That keeps the open-PR cap
    (E0-R6) and the sweep's branch naming (E3-R3) honest."""
    harness.checkout("some-other-feature")

    result = harness.run(DefaultBranchGuard(), job="sweep")

    result.assert_refused(Reason.DEFAULT_BRANCH)


def test_the_default_branch_is_detected_not_assumed_to_be_main(harness):
    """A repo whose default branch is `trunk` must be protected on `trunk`."""
    harness.rename_branch("trunk")

    result = harness.run(DefaultBranchGuard(), job="sweep")

    result.assert_refused(Reason.DEFAULT_BRANCH)
    assert "default branch 'trunk'" in result.refusal.message


def test_a_configured_prefix_is_honoured(harness):
    harness.checkout("janitor/sweep-1")

    result = harness.run(DefaultBranchGuard(branch_prefix="janitor/"), job="sweep")

    result.assert_allowed()


def test_refusal_names_the_safe_alternative(harness):
    result = harness.run(DefaultBranchGuard(), job="sweep")

    assert "curamoir/" in result.refusal.message


def test_an_empty_prefix_does_not_disable_default_branch_protection(harness):
    """The case only the default-branch check can catch.

    `guardrails.branch_prefix: ""` in config makes every branch satisfy the
    prefix test, so without a separate default-branch check a misconfiguration
    would quietly open `main` to a sweep. This is why the two checks are not
    redundant.
    """
    result = harness.run(DefaultBranchGuard(branch_prefix=""), job="sweep")

    result.assert_refused(Reason.DEFAULT_BRANCH)
    assert "default branch" in result.refusal.message


# --- a target branch, checked instead of HEAD (cm-fkd.12) -------------------------
#
# The sweep never checks out its branch: it pushes a commit to
# `<prefix>sweep-...` from the user's checkout, which may be on `main`. So at
# push time the guard judges the branch being written, not the one checked
# out (fix_scope's runner step 0 (i): DefaultBranchGuard at push/PR time).


def test_a_target_branch_is_judged_instead_of_the_checked_out_one(harness):
    assert harness.current_branch() == "main"

    result = harness.run(DefaultBranchGuard(target_branch="curamoir/sweep-2026-10-05"),
                         job="sweep")

    result.assert_allowed()


def test_a_target_of_the_default_branch_is_refused_from_any_checkout(harness):
    harness.checkout("curamoir/sweep-2026-10-05")

    result = harness.run(DefaultBranchGuard(target_branch="main"), job="sweep")

    result.assert_refused(Reason.DEFAULT_BRANCH)
    assert "default branch 'main'" in result.refusal.message


def test_a_target_outside_the_prefix_is_refused(harness):
    harness.checkout("curamoir/sweep-2026-10-05")

    result = harness.run(DefaultBranchGuard(target_branch="feature/x"), job="sweep")

    result.assert_refused(Reason.DEFAULT_BRANCH)
    assert "'feature/x' is outside the 'curamoir/' prefix" in result.refusal.message


def test_an_empty_target_is_refused_not_degraded(harness):
    """A HEAD git cannot name degrades (the job still runs, on record); a
    target the caller named as "" is the caller's bug, and the write it
    guards must not happen."""
    harness.checkout("curamoir/sweep-2026-10-05")

    result = harness.run(DefaultBranchGuard(target_branch=""), job="sweep")

    result.assert_refused(Reason.DEFAULT_BRANCH)
    assert result.performed is False
