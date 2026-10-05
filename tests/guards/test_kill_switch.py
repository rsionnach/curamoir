"""E0-R3: the kill switch.

If `.curamoir/PAUSE` exists, or the repository carries a `curamoir:paused`
label/topic, every scheduled job exits immediately with a logged reason and no
side effects.

This is the guardrail a returning developer reaches for first, and the one whose
mechanism every issue and PR footer documents (E6-R2). It must be the cheapest
and least surprising check in the system.
"""

from curamoir.guards import Reason
from curamoir.guards.kill_switch import KillSwitch


def test_pause_file_stops_the_job_with_reason_paused(harness):
    harness.write(".curamoir/PAUSE", "")

    result = harness.run(KillSwitch(), job="sweep")

    result.assert_refused(Reason.PAUSED)
    assert result.performed is False


def test_absent_pause_file_lets_the_job_run(harness):
    result = harness.run(KillSwitch(), job="sweep")

    result.assert_allowed()
    assert result.performed is True


def test_paused_label_on_the_repository_stops_the_job(harness):
    result = harness.run(
        KillSwitch(repo_labels=lambda: ["curamoir", "curamoir:paused"]), job="review"
    )

    result.assert_refused(Reason.PAUSED)


def test_unrelated_labels_do_not_stop_the_job(harness):
    result = harness.run(
        KillSwitch(repo_labels=lambda: ["curamoir", "bug", "enhancement"]), job="review"
    )

    result.assert_allowed()


def test_refusal_message_names_how_to_resume(harness):
    harness.write(".curamoir/PAUSE", "")

    result = harness.run(KillSwitch(), job="sweep")

    assert ".curamoir/PAUSE" in result.refusal.message


def test_an_unreachable_label_source_does_not_stop_a_job_the_pause_file_permits(harness):
    """A paused *label* needs the network. If that lookup fails we must not
    invent a pause — the PAUSE file is the local source of truth, and a GitHub
    outage must not silently halt a month of scheduled work.
    """
    def exploding_labels():
        raise RuntimeError("gh unreachable")

    result = harness.run(KillSwitch(repo_labels=exploding_labels), job="sweep")

    result.assert_allowed()
