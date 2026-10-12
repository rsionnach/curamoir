"""The ledger is the only durable record that a guardrail fired.

Guards fail open on hook timeout (DEC-5), so an unrecorded guard failure is a
silent loss of enforcement. E13-R3's dogfood log reads these entries.
"""

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from curamoir.guards import Gateway, JobContext, Reason, Refusal
from curamoir.ledger import Ledger, parse_ledger_timestamp


def test_refusal_is_recorded_with_its_job_and_reason(tmp_path):
    ledger = Ledger(tmp_path / "ledger.jsonl")
    gateway = Gateway(
        guards=[lambda _c: Refusal(Reason.BUDGET, "at 95% of the monthly ceiling")],
        ledger=ledger,
    )

    gateway.perform(JobContext(job="sweep", repo=tmp_path), lambda: None)

    entries = ledger.entries()
    assert len(entries) == 1
    assert entries[0]["job"] == "sweep"
    assert entries[0]["reason"] == "budget"


def test_ledger_is_append_only_across_separate_handles(tmp_path):
    path = tmp_path / "ledger.jsonl"
    Ledger(path).record(job="sweep", reason=Reason.PAUSED, message="paused")
    Ledger(path).record(job="review", reason=Reason.LOCKED, message="locked")

    assert [e["reason"] for e in Ledger(path).entries()] == ["paused", "locked"]


def test_guard_failure_is_recorded_so_lost_enforcement_is_never_silent(tmp_path):
    def broken_guard(_context):
        raise RuntimeError("ledger unreadable")

    ledger = Ledger(tmp_path / "ledger.jsonl")
    gateway = Gateway(guards=[broken_guard], ledger=ledger)

    gateway.perform(JobContext(job="review", repo=tmp_path), lambda: None)

    entry = ledger.entries()[0]
    assert entry["reason"] == "guard_error"
    assert entry["job"] == "review"
    assert "broken_guard" in entry["message"]


@pytest.mark.parametrize("at", ["2026-10-11T12:00:00Z", "2026-10-11T12:00:00.5Z",
                                "2026-10-11T12:00:00.12+00:00", "2026-10-11T12:00:00.1234567Z"])
def test_a_z_suffix_and_short_fractions_are_read(at):
    when = parse_ledger_timestamp(at)
    assert when is not None
    assert when.replace(microsecond=0) == datetime(2026, 10, 11, 12, tzinfo=timezone.utc)


@pytest.mark.skipif(not shutil.which("python3.9") and not Path("/usr/bin/python3").exists(),
                    reason="no system python3 to check 3.9 semantics with")
def test_the_timestamp_parser_agrees_on_the_fallback_python():
    """scripts/curamoir may run under the system python3 (3.9 on macOS),
    whose fromisoformat rejects `Z` and 1-, 2-, 4- and 5-digit fractions."""
    python = shutil.which("python3.9") or "/usr/bin/python3"
    stamps = ["2026-10-11T12:00:00Z", "2026-10-11T12:00:00.5Z", "2026-10-11T12:00:00.12+00:00",
              "2026-10-11T12:00:00+00:00", "2026-10-11T12:00:00", "nonsense"]
    code = ("import json, sys; from curamoir.ledger import parse_ledger_timestamp as p; "
            "print(json.dumps([None if p(s) is None else p(s).isoformat() "
            "for s in json.loads(sys.argv[1])]))")
    src = Path(__file__).resolve().parents[1] / "src"
    out = subprocess.run([python, "-c", code, json.dumps(stamps)], capture_output=True,
                         text=True, check=True, env={"PYTHONPATH": str(src), "PATH": "/usr/bin"},
                         stdin=subprocess.DEVNULL)
    here = [None if parse_ledger_timestamp(s) is None else parse_ledger_timestamp(s).isoformat()
            for s in stamps]
    assert json.loads(out.stdout) == here
    assert here[0] == "2026-10-11T12:00:00+00:00" and here[-1] is None
