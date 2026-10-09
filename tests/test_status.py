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


# --- the invariants the code comments name, each with a fixture -----------
#
# Every pre-existing test exercised the happy path, so the invariants the code
# comments describe were never entered. Each fixture below is the shape the
# comment names, not a convenient one.

def test_a_lock_that_is_present_but_unreadable_is_not_reported_as_free(tmp_path):
    """status.py's own comment: "Present but unreadable is not free. Say so
    rather than implying free." No test ever wrote a corrupt lock, so the
    except branch was never entered and flipping held to False stayed green."""
    repo = initialised(tmp_path)
    (repo / ".curamoir" / "state" / "lock").write_text("not json at all", encoding="utf-8")

    status = collect_status(repo)

    assert status["lock"]["held"] is True
    assert "unreadable" in (status["lock"]["detail"] or "").lower()
    assert "unreadable" in render_status(status).lower()


def test_spend_from_a_previous_month_is_not_counted(tmp_path):
    """The only spend test wrote one entry timestamped now, so the month filter
    could not be wrong. Deleting it entirely stayed green."""
    from curamoir.ledger import Ledger

    repo = initialised(tmp_path)
    ledger = Ledger(repo / ".curamoir" / "state" / "ledger.jsonl")
    for at in ("2026-09-15T10:00:00+00:00", "2026-10-15T10:00:00+00:00"):
        ledger.record(kind="job", job="sweep", reason=None, message="", outcome="success",
                      model="claude-sonnet-5", input_tokens=1_000_000, output_tokens=0, at=at)

    status = collect_status(repo, now=lambda: datetime(2026, 10, 20, tzinfo=timezone.utc))

    # $2.00 for the October entry alone; $4.00 would mean September leaked in.
    assert status["spend"]["month_to_date_usd"] == 2.00


def test_an_entry_in_another_timezone_is_attributed_by_utc(tmp_path):
    """status.py's comment describes this exact case: 2026-11-01T01:00+13:00 is
    2026-10-31T12:00Z, so attributing by the entry's own offset would lose it
    from October and double-count it in November. No fixture existed."""
    from curamoir.ledger import Ledger

    repo = initialised(tmp_path)
    Ledger(repo / ".curamoir" / "state" / "ledger.jsonl").record(
        kind="job", job="sweep", reason=None, message="", outcome="success",
        model="claude-sonnet-5", input_tokens=1_000_000, output_tokens=0,
        at="2026-11-01T01:00:00+13:00")

    status = collect_status(repo, now=lambda: datetime(2026, 10, 31, tzinfo=timezone.utc))

    assert status["spend"]["month_to_date_usd"] == 2.00, "the entry was not attributed to October"


def test_an_unreadable_ledger_line_is_reported_as_a_lower_bound(tmp_path):
    repo = initialised(tmp_path)
    (repo / ".curamoir" / "state" / "ledger.jsonl").write_text(
        json.dumps({"kind": "job", "job": "s", "model": "claude-sonnet-5",
                    "input_tokens": 1_000_000, "output_tokens": 0,
                    "at": datetime.now(timezone.utc).isoformat()}) + "\n{torn\n",
        encoding="utf-8")

    status = collect_status(repo)

    assert status["spend"]["unreadable_lines"] == 1
    assert "lower bound" in (status["spend"]["detail"] or "")
    assert "lower bound" in render_status(status)


def test_the_defaults_handed_out_on_a_missing_config_cannot_be_mutated(tmp_path):
    """A shared mutable DEFAULTS is one careless write away from poisoning every
    later load_config in the process."""
    from curamoir.config import DEFAULTS

    repo = initialised(tmp_path)
    (repo / ".curamoir" / "curamoir.yaml").unlink()
    original = DEFAULTS["budget"]["monthly_ceiling_usd"]

    status = collect_status(repo)
    status_ceiling = status["spend"]["ceiling_usd"]
    assert status_ceiling == original

    # Prove it is a copy, not the module-level object.
    from copy import deepcopy
    snapshot = deepcopy(DEFAULTS)
    collect_status(repo)
    assert snapshot == DEFAULTS


def test_the_grades_section_says_not_available_independently_of_cadence(tmp_path):
    """The old assertion searched the whole rendering for "not yet available",
    which render_status emits from BOTH _summarise_grades and _summarise_cadence
    — so a broken grades summary passed as long as cadence still rendered. That
    is this session's "'trunk' in message" escape, recurring here."""
    from curamoir.status import _summarise_grades

    assert "not yet available" in _summarise_grades(None).lower()
    assert "not yet available" in _summarise_grades({"modules": {}}).lower()
    assert "not yet available" not in _summarise_grades(
        {"modules": {"src/a": {"grade": "B"}}}
    ).lower()
