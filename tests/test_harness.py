"""Tests for the guardrail harness itself.

Every Epic 0 acceptance criterion is phrased as "a test harness demonstrates
that...", so the harness is a deliverable, not scaffolding, and it gets its own
tests.
"""

import pytest

from curamoir.guards import Reason, Refusal


def test_harness_provides_a_real_throwaway_git_repo(harness):
    assert (harness.repo / ".git").is_dir()
    assert harness.current_branch() != ""


def test_harness_runs_a_guard_in_isolation_and_reports_refusal_and_ledger(harness):
    def pause_guard(context):
        if (context.repo / ".curamoir" / "PAUSE").exists():
            return Refusal(Reason.PAUSED, "remove .curamoir/PAUSE to resume")
        return None

    harness.write(".curamoir/PAUSE", "")

    result = harness.run(pause_guard, job="sweep")

    assert result.refusal.reason is Reason.PAUSED
    assert [entry["reason"] for entry in result.ledger] == ["paused"]
    assert result.performed is False


def test_harness_lets_the_side_effect_through_when_the_guard_is_silent(harness):
    def pause_guard(context):
        if (context.repo / ".curamoir" / "PAUSE").exists():
            return Refusal(Reason.PAUSED, "paused")
        return None

    result = harness.run(pause_guard, job="sweep")

    assert result.refusal is None
    assert result.ledger == []
    assert result.performed is True


def test_prompt_only_guardrail_fails_the_harness(harness):
    """E0-R0's second acceptance criterion.

    A guardrail that lives only in prompt text enforces nothing in code. Its
    code-level equivalent inspects nothing and allows everything, and the
    harness's own assertion helper must reject it even though the repo is in a
    state a real guardrail would refuse.
    """
    def prompt_only_guard(_context):
        return None

    harness.write(".curamoir/PAUSE", "")

    result = harness.run(prompt_only_guard, job="sweep")

    with pytest.raises(AssertionError, match="did not refuse"):
        result.assert_refused(Reason.PAUSED)
