"""E1-R3: /curamoir:status — everything you need on return, in under two seconds.

Two constraints are testable and both are asserted: no model calls, and under
two seconds. They are the point of the command. A status that needs a model is
a status you cannot run on a whim, and one that takes ten seconds is one you
stop running.

Sections whose producing epic has not landed must read "not yet available"
rather than erroring or lying — this command ships in M0, before grades (E7),
the runner (E10) and the sweep (Epic 3) exist.
"""

import json
import time
from datetime import datetime, timedelta, timezone

from curamoir.status import collect_status, render_status


def initialised(tmp_path):
    from curamoir.scaffold import init_repo

    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    package = tmp_path / "src" / "app"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    init_repo(tmp_path)
    return tmp_path


def test_status_on_an_uninitialised_repo_says_so_instead_of_failing(tmp_path):
    status = collect_status(tmp_path)

    assert status["initialised"] is False
    assert "init" in render_status(status).lower()


def test_pause_state_is_reported(tmp_path):
    repo = initialised(tmp_path)
    (repo / ".curamoir" / "PAUSE").write_text("", encoding="utf-8")

    status = collect_status(repo)

    assert status["paused"] is True
    assert "paused" in render_status(status).lower()


def test_an_absent_pause_file_reads_as_running(tmp_path):
    status = collect_status(initialised(tmp_path))

    assert status["paused"] is False


def test_lock_state_is_reported_with_its_holder(tmp_path):
    repo = initialised(tmp_path)
    (repo / ".curamoir" / "state" / "lock").write_text(
        json.dumps({"pid": 1, "host": "somewhere", "started": "2026-10-05T20:00:00+00:00",
                    "job": "sweep"}), encoding="utf-8")

    status = collect_status(repo)

    assert status["lock"]["held"] is True
    assert status["lock"]["job"] == "sweep"


def test_month_to_date_spend_is_shown_against_the_ceiling(tmp_path):
    repo = initialised(tmp_path)
    from curamoir.ledger import Ledger

    now = datetime.now(timezone.utc).isoformat()
    Ledger(repo / ".curamoir" / "state" / "ledger.jsonl").record(
        kind="job", job="sweep", reason=None, message="", outcome="success",
        model="claude-sonnet-5", input_tokens=1_000_000, output_tokens=100_000, at=now,
    )

    status = collect_status(repo)

    assert status["spend"]["month_to_date_usd"] == 3.00
    assert status["spend"]["ceiling_usd"] == 15


def test_handoff_age_is_reported_in_days(tmp_path):
    repo = initialised(tmp_path)
    handoff = repo / ".curamoir" / "handoff" / "latest.md"
    handoff.parent.mkdir(parents=True, exist_ok=True)
    handoff.write_text("# handoff\n", encoding="utf-8")
    old = (datetime.now(timezone.utc) - timedelta(days=9)).timestamp()
    import os
    os.utime(handoff, (old, old))

    status = collect_status(repo)

    assert status["handoff"]["age_days"] == 9


def test_a_missing_handoff_is_reported_as_absent_not_as_zero_days(tmp_path):
    """Zero days would read as "written just now", which is the opposite of the
    truth and exactly the wrong thing to tell someone returning."""
    status = collect_status(initialised(tmp_path))

    assert status["handoff"]["age_days"] is None
    assert "never" in render_status(status).lower()


def test_sections_whose_epics_have_not_landed_say_not_yet_available(tmp_path):
    status = collect_status(initialised(tmp_path))
    rendered = render_status(status)

    assert status["grades"] is None
    assert "not yet available" in rendered.lower()


def test_grades_are_shown_when_present(tmp_path):
    repo = initialised(tmp_path)
    (repo / ".curamoir" / "state" / "grades.json").write_text(
        json.dumps({"modules": {"src/app": {"grade": "B", "score": 81}}}), encoding="utf-8")

    status = collect_status(repo)

    assert status["grades"]["modules"]["src/app"]["grade"] == "B"


def test_open_pull_requests_are_reported_when_a_lister_is_supplied(tmp_path):
    repo = initialised(tmp_path)

    status = collect_status(repo, open_pr_branches=lambda: ["curamoir/sweep-1", "feature/x"])

    assert status["open_prs"]["ours"] == ["curamoir/sweep-1"]


def test_an_unreachable_pr_lister_degrades_rather_than_failing(tmp_path):
    """gh is a network call. Status must never hang or crash over it — that would
    break the two-second promise."""
    def exploding():
        raise RuntimeError("gh unreachable")

    status = collect_status(initialised(tmp_path), open_pr_branches=exploding)

    assert status["open_prs"]["unavailable"] is True
    assert "unavailable" in render_status(status).lower()


def test_collecting_status_makes_no_network_or_model_call_by_default(tmp_path):
    """No lister supplied means no `gh` call at all, so the default path is
    purely local file reads."""
    status = collect_status(initialised(tmp_path))

    assert status["open_prs"]["checked"] is False


def test_status_completes_well_inside_two_seconds(tmp_path):
    repo = initialised(tmp_path)

    started = time.monotonic()
    render_status(collect_status(repo))
    elapsed = time.monotonic() - started

    assert elapsed < 2.0, f"took {elapsed:.3f}s, over the two-second requirement"
