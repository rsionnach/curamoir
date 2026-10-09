"""An audit line must never be able to fail the job it is auditing.

`JobRun`'s contract is "cannot leave without writing its ledger line", and
round 4 made an OSError there non-fatal. But `record()` has a second way to
fail that has nothing to do with the filesystem: `json.dumps` raises TypeError
for a value it cannot serialise, and `succeeded(**fields)` is the documented
extension point — the one E11-R1 will use for cost attribution, where a `Path`
or a `datetime` is the obvious slip.

That failure was worse than the OSError it sat beside, because it also fired on
the CLEAN path: a job that did its work raised, and wrote no line at all.
"""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from curamoir.ledger import Ledger


def test_a_successful_job_is_not_failed_by_an_unserialisable_field(tmp_path):
    from curamoir.job_run import JobRun

    ledger = Ledger(tmp_path / "ledger.jsonl")

    with JobRun(job="sweep", ledger=ledger) as run:
        run.succeeded("done", artefact=Path("/tmp/x"), at=datetime.now(timezone.utc))

    lines = ledger.entries()
    assert len(lines) == 1, f"E0-R9 promises one line per exit, got {lines}"
    assert lines[0]["outcome"] == "success"


def test_an_unserialisable_field_does_not_replace_the_real_exception(tmp_path):
    from curamoir.job_run import JobRun

    ledger = Ledger(tmp_path / "ledger.jsonl")

    with pytest.raises(RuntimeError, match="the real failure"):
        with JobRun(job="sweep", ledger=ledger, ) as run:
            run.failed("broke", artefact=Path("/tmp/x"))
            raise RuntimeError("the real failure the operator needs to see")


def test_an_unserialisable_field_is_recorded_as_its_repr_not_dropped(tmp_path):
    """Losing the line entirely is worse than logging a field as text."""
    ledger = Ledger(tmp_path / "ledger.jsonl")

    ledger.record(job="sweep", reason=None, message="ok", artefact=Path("/tmp/x"))

    assert "/tmp/x" in ledger.entries()[0]["artefact"]


def test_the_unreadable_count_is_known_before_any_read(tmp_path):
    """`unreadable_lines()` guarded against being called first; the newer
    `note_unaccounted` did not, and raised AttributeError on the same object."""
    ledger = Ledger(tmp_path / "ledger.jsonl")

    ledger.note_unaccounted()

    assert ledger.unreadable_lines() in (None, 0, 1)


def test_unattributable_spend_survives_a_later_read(tmp_path):
    """The two contributors to the count have different lifecycles: `entries()`
    recomputes parse failures, which is right, and used to discard notes
    contributed from outside by `month_to_date`'s separate pass, which is a
    silent understatement from the one counter built to prevent exactly that."""
    import json

    from curamoir.budget import PriceTable, month_to_date

    prices = PriceTable({"claude-sonnet-5": {"input": 2.0, "output": 10.0}})
    ledger = Ledger(tmp_path / "ledger.jsonl")
    ledger._path.write_text(
        json.dumps({"kind": "job", "model": "claude-sonnet-5",
                    "input_tokens": 1_000_000, "output_tokens": 0,
                    "at": "not a date"}) + "\n",
        encoding="utf-8")

    month_to_date(ledger, prices, datetime(2026, 10, 8, tzinfo=timezone.utc))
    before = ledger.unreadable_lines()
    ledger.entries()

    assert before == 1
    assert ledger.unreadable_lines() == 1, "a later read discarded the note"
