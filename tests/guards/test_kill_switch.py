"""E0-R3: the kill switch.

If `.curamoir/PAUSE` exists, or the repository carries a `curamoir:paused`
label, every scheduled job exits immediately with a logged reason and no
side effects.

This is the guardrail a returning developer reaches for first, and the one whose
mechanism every issue and PR footer documents (E6-R2). It must be the cheapest
and least surprising check in the system.
"""

from copy import deepcopy

import pytest
from tests import gh_label_payloads

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


def assert_degraded_not_unpaused(result):
    """A label source that answered in the wrong shape has not said "no
    labels": the kill switch was not checked, and the ledger must say so."""
    result.assert_allowed()
    assert result.degraded is not None
    assert result.degraded.reason == Reason.PAUSED
    assert [e["severity"] for e in result.ledger] == ["degraded"]


def test_gh_label_objects_degrade_the_check_rather_than_never_matching(harness):
    """`gh label list --json name` gives `{name: ...}` objects. Passed through
    unconverted, they would never equal the label name, and a paused repo
    would run as if unpaused, with nothing recorded. The list is gh's real
    output (tests/gh_label_payloads.py), with the pause label among it."""
    labels = lambda: deepcopy(gh_label_payloads.LABEL_LIST)  # noqa: E731

    result = harness.run(KillSwitch(repo_labels=labels), job="sweep")

    assert_degraded_not_unpaused(result)
    assert "not a label-name string" in result.degraded.message


def test_a_bare_string_of_labels_degrades_the_check(harness):
    """A string is iterable, so `list()` would split it into characters."""
    result = harness.run(KillSwitch(repo_labels=lambda: "curamoir:paused"), job="sweep")

    assert_degraded_not_unpaused(result)
    assert "not an iterable of label names" in result.degraded.message


def test_bytes_from_the_label_source_degrade_the_check(harness):
    """Bytes iterate as ints, so `list()` would give numbers, never the label
    name: a source that hands back gh's raw stdout undecoded would leave a
    paused repository running. Refused alongside str, and the line says so."""
    result = harness.run(KillSwitch(repo_labels=lambda: b"curamoir:paused"), job="sweep")

    assert_degraded_not_unpaused(result)
    assert "returned bytes" in result.degraded.message
    assert "not an iterable of label names" in result.degraded.message


def test_a_label_that_is_not_a_string_degrades_the_check(harness):
    result = harness.run(
        KillSwitch(repo_labels=lambda: ["bug", 7]), job="sweep"
    )

    assert_degraded_not_unpaused(result)
    # The line names the contract that was broken, not an AttributeError
    # from deep inside the comparison, so it says what to fix.
    assert "not a label-name string" in result.degraded.message


def test_the_paused_label_matches_whatever_its_case(harness):
    """GitHub treats label names case-insensitively, so a maintainer who
    types `Curamoir:Paused` has paused the repository."""
    result = harness.run(
        KillSwitch(repo_labels=lambda: ["Curamoir:Paused"]), job="sweep"
    )

    result.assert_refused(Reason.PAUSED)


@pytest.mark.parametrize("returned", [
    gh_label_payloads.REPO_VIEW,
    gh_label_payloads.LABEL_LIST[1],
], ids=["gh-repo-view", "one-gh-object"])
def test_a_mapping_of_labels_degrades_the_check(harness, returned):
    """`gh repo view --json labels` gives `{"labels": [...]}`, and one gh label
    is `{"name": ...}`. A dict iterates as its str keys, which would pass as
    label names that never match: a paused repository run, nothing said.
    Both shapes are gh's recorded output (tests/gh_label_payloads.py); the
    one object is the pause label's own entry from `gh label list`."""
    result = harness.run(KillSwitch(repo_labels=lambda: deepcopy(returned)), job="sweep")

    assert_degraded_not_unpaused(result)
    assert "not an iterable of label names" in result.degraded.message
    assert "dict" in result.degraded.message


def test_none_from_the_label_source_degrades_the_check_and_says_so(harness):
    """A callable that forgot to return has not said there are no labels."""
    result = harness.run(KillSwitch(repo_labels=lambda: None), job="sweep")

    assert_degraded_not_unpaused(result)
    assert "returned None, not an iterable of label names" in result.degraded.message


def test_a_label_source_that_fails_partway_through_degrades_the_check(harness):
    """A lazy source can fail after yielding some labels; the ones before the
    failure are not the repository's labels, and must not be read as such."""
    def labels():
        yield "bug"
        raise OSError("gh: connection reset")

    result = harness.run(KillSwitch(repo_labels=labels), job="sweep")

    assert_degraded_not_unpaused(result)
    assert "connection reset" in result.degraded.message


def test_the_paused_label_matches_with_surrounding_whitespace(harness):
    result = harness.run(
        KillSwitch(repo_labels=lambda: ["  curamoir:paused\n"]), job="sweep"
    )

    result.assert_refused(Reason.PAUSED)
