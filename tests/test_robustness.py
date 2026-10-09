"""Correctness-review findings 3-10: the failure paths that take the system down.

Common shape: a malformed input that is certain to occur — a crash mid-append, a
typo in an editable table — turns into "every job refused" plus "the one command
meant to tell you why crashes with a traceback".
"""

import json
import subprocess
from datetime import timezone

import pytest

from curamoir.budget import Budget, PriceTable, UnknownModel
from curamoir.guards import Degraded, Gateway, JobContext
from curamoir.ledger import Ledger
from curamoir.status import collect_status

PRICES = PriceTable({"claude-sonnet-5": {"input": 2.0, "output": 10.0}})


def initialised(tmp_path):
    from curamoir.scaffold import init_repo

    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    package = tmp_path / "src" / "app"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    init_repo(tmp_path)
    return tmp_path


# --- 3. a half-written ledger line ----------------------------------------

def test_a_truncated_ledger_line_does_not_refuse_every_job(tmp_path):
    """An append-only file written by hooks AND the runner will eventually be
    cut mid-write by a crash. That must not refuse all future jobs."""
    repo = initialised(tmp_path)
    ledger_path = repo / ".curamoir" / "state" / "ledger.jsonl"
    ledger_path.write_text(
        json.dumps({"kind": "job", "job": "sweep", "model": "claude-sonnet-5",
                    "input_tokens": 1000, "output_tokens": 100,
                    "at": "2026-10-07T10:00:00+00:00"}) + "\n"
        + '{"kind": "job", "job": "swe',  # crash mid-append
        encoding="utf-8")

    gateway = Gateway(guards=[Budget(prices=PRICES, monthly_ceiling_usd=15.0)])
    outcome = gateway.perform(JobContext(job="sweep", repo=repo), lambda: "ran")

    assert outcome == "ran", f"job refused because of a malformed ledger line: {outcome}"


def test_a_truncated_ledger_line_does_not_crash_status(tmp_path):
    repo = initialised(tmp_path)
    (repo / ".curamoir" / "state" / "ledger.jsonl").write_text(
        '{"kind": "job", "job": "swe', encoding="utf-8")

    status = collect_status(repo)

    assert status["initialised"] is True


def test_unreadable_ledger_lines_are_counted_not_silently_dropped(tmp_path):
    """Silently skipping would undercount spend, which is the one thing the
    budget guard exists to prevent."""
    path = tmp_path / "ledger.jsonl"
    path.write_text('{"kind":"job"}\nnot json\n{"kind":"job"}\n', encoding="utf-8")

    ledger = Ledger(path)

    assert len(ledger.entries()) == 2
    assert ledger.unreadable_lines() == 1


# --- 4. a malformed price table -------------------------------------------

def test_a_price_entry_missing_output_degrades_rather_than_exploding(tmp_path):
    with pytest.raises(UnknownModel):
        PriceTable({"m": {"input": 1.0}}).cost("m", 1000, 1000)


def test_a_malformed_price_table_is_rejected_by_config_validation():
    from curamoir.config import ConfigError, load_config

    with pytest.raises(ConfigError) as caught:
        load_config({"budget": {"prices": {"m": {"input": 1.0}}}})

    assert "budget.prices.m" in str(caught.value)


def test_a_mistyped_price_key_does_not_refuse_every_job(tmp_path):
    repo = initialised(tmp_path)
    (repo / ".curamoir" / "state" / "ledger.jsonl").write_text(
        json.dumps({"kind": "job", "job": "sweep", "model": "m",
                    "input_tokens": 1, "output_tokens": 1,
                    "at": "2026-10-07T10:00:00+00:00"}) + "\n", encoding="utf-8")

    guard = Budget(prices=PriceTable({"m": {"input": 1.0}}), monthly_ceiling_usd=15.0)
    ledger = Ledger(repo / ".curamoir" / "state" / "ledger.jsonl")
    gateway = Gateway(guards=[guard], ledger=ledger)

    outcome = gateway.perform(JobContext(job="sweep", repo=repo), lambda: "ran")

    assert outcome == "ran", f"job refused over a mistyped price key: {outcome}"
    assert any(e.get("severity") == "degraded" for e in ledger.entries()), (
        "the unenforced ceiling was not recorded"
    )


# --- 5. porcelain parsing -------------------------------------------------

def test_unstaged_changes_are_described_correctly(tmp_path):
    """Porcelain v1 is two status characters then a space. For an UNSTAGED change
    the first is a space, so splitting on the first space put the status
    character into the path and lost the label — and modified-but-unstaged is the
    overwhelmingly common state of the interrupted session a handoff is for."""
    from curamoir.handoff import _describe_porcelain

    assert _describe_porcelain(" M README.md") == "modified: README.md"
    assert _describe_porcelain(" D gone.py") == "deleted: gone.py"
    assert _describe_porcelain("M  staged.py") == "modified: staged.py"
    assert _describe_porcelain("MM both.py") == "modified: both.py"
    assert _describe_porcelain("UU conflict.py") == "conflicted: conflict.py"
    assert _describe_porcelain("?? new file.txt") == "untracked: new file.txt"
    assert _describe_porcelain("R  old.py -> new.py") == "renamed: old.py -> new.py"


def test_a_real_unstaged_edit_is_named_in_the_handoff(tmp_path):
    from curamoir.handoff import build_handoff

    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "--initial-branch=main"], ["config", "user.email", "t@e.invalid"],
                 ["config", "user.name", "T"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "i"], cwd=repo, check=True, capture_output=True)
    (repo / "app.py").write_text("x = 2\n", encoding="utf-8")  # unstaged
    (repo / ".curamoir" / "handoff").mkdir(parents=True)

    changes = build_handoff(repo)["uncommitted"]

    assert changes == ["modified: app.py"], changes


# --- 6. doctor must not destroy what it diagnoses -------------------------

def test_doctor_does_not_overwrite_the_developers_handoff(tmp_path):
    from curamoir.doctor import diagnose

    repo = initialised(tmp_path)
    handoff = repo / ".curamoir" / "handoff" / "latest.md"
    handoff.parent.mkdir(parents=True, exist_ok=True)
    handoff.write_text("# MY REAL HANDOFF\n\nthe thing I must not lose\n", encoding="utf-8")
    journal = repo / ".curamoir" / "handoff" / "journal.jsonl"
    journal.write_text(json.dumps({"at": "t", "note": "my real decision"}) + "\n", encoding="utf-8")

    diagnose(repo, tools={"python3": True, "yaml": True})

    assert "MY REAL HANDOFF" in handoff.read_text(encoding="utf-8")
    assert journal.read_text(encoding="utf-8").count("\n") == 1, "doctor fabricated journal notes"
    history = list((repo / ".curamoir" / "handoff" / "history").glob("*.md"))
    assert history == [], f"doctor burned history slots: {history}"


# --- 8. doctor's python3 check --------------------------------------------

def test_the_python3_check_fails_when_python3_is_not_on_path(tmp_path, monkeypatch):
    """It resolved the RUNNING interpreter's absolute path, which exists by
    definition, so the check could never fail — while the hook wrappers it stands
    for resolve `command -v python3` and exit 0 silently when that misses.

    Tested behaviourally: asserting on the source text broke as soon as the
    explanation mentioned sys.executable in a comment.
    """
    from curamoir import doctor

    # Capture the FUNCTION, not the module: `import shutil` in the test binds the
    # same module object doctor holds, so patching through the module recursed
    # into the patch itself.
    original_which = doctor.shutil.which

    def which(name):
        # Hide every candidate the WRAPPERS try, taken from doctor's own list
        # rather than spelled out here: naming `python3` and `python` by hand
        # stopped covering the case as soon as the wrappers learned to prefer
        # `python3.12` first, and the test went green on a machine that had one.
        if name in doctor.WRAPPER_INTERPRETERS:
            return None
        return original_which(name)

    monkeypatch.setattr(doctor.shutil, "which", which)

    findings = doctor.diagnose(initialised(tmp_path), tools={"yaml": True})
    python = [f for f in findings if f.check == "python3"]

    assert python and python[0].level == "error", python


# --- 10. job-level CI permissions -----------------------------------------

def test_a_job_level_permissions_block_is_checked():
    """A job-level block OVERRIDES the workflow-level one in GitHub Actions, so
    `id-token: write` — the scope that unlocks OIDC cloud credentials — could be
    added by indenting four lines."""
    from curamoir.ci.permissions import check_workflow_permissions

    workflow = """
name: curamoir
permissions:
  contents: write
  pull-requests: write
  issues: write
jobs:
  sweep:
    permissions:
      id-token: write
      packages: write
    runs-on: ubuntu-latest
"""
    problems = check_workflow_permissions(workflow)

    assert problems
    assert any("id-token" in p for p in problems), problems


# --- the budget guard's own failure paths ---------------------------------

def test_an_unreadable_ledger_does_not_pass_as_zero_spend(tmp_path):
    """Round 2's fix turned a loud crash into a silent zero: OSError returned []
    with the unreadable count still 0, so the caller could not tell "no spend"
    from "could not read", and the ceiling was enforced against $0.00."""
    repo = initialised(tmp_path)
    ledger_path = repo / ".curamoir" / "state" / "ledger.jsonl"
    ledger_path.unlink(missing_ok=True)
    ledger_path.mkdir()  # a directory where a file belongs: OSError on read

    from curamoir.guards import Degraded

    outcome = Budget(prices=PRICES, monthly_ceiling_usd=10.0)(
        JobContext(job="sweep", repo=repo)
    )

    assert isinstance(outcome, Degraded), f"silent allow with spend assumed $0: {outcome}"
    assert "could not" in outcome.message.lower() or "unreadable" in outcome.message.lower()


def test_a_lower_bound_already_over_the_ceiling_refuses(tmp_path):
    """A corrupt line made the guard return Degraded BEFORE comparing projected
    spend to the ceiling, so one bad byte turned on_ceiling: pause into
    on_ceiling: ignore — and the message claimed the ceiling "was enforced".

    A lower bound that already exceeds the ceiling is PROOF of a breach, not
    uncertainty about one."""
    repo = initialised(tmp_path)
    (repo / ".curamoir" / "state" / "ledger.jsonl").write_text(
        json.dumps({"kind": "job", "job": "sweep", "model": "claude-sonnet-5",
                    "input_tokens": 20_000_000, "output_tokens": 20_000_000,
                    "at": "2026-10-08T10:00:00+00:00"}) + "\n{broken\n",
        encoding="utf-8")

    from curamoir.guards import Refusal

    outcome = Budget(prices=PRICES, monthly_ceiling_usd=10.0, on_ceiling="pause")(
        JobContext(job="sweep", repo=repo)
    )

    assert isinstance(outcome, Refusal), f"unbounded spend bypass: {outcome}"
    assert outcome.reason.value == "budget"


def test_a_lower_bound_under_the_ceiling_degrades_and_says_it_is_unconfirmed(tmp_path):
    repo = initialised(tmp_path)
    (repo / ".curamoir" / "state" / "ledger.jsonl").write_text(
        json.dumps({"kind": "job", "job": "sweep", "model": "claude-sonnet-5",
                    "input_tokens": 1000, "output_tokens": 100,
                    "at": "2026-10-08T10:00:00+00:00"}) + "\n{broken\n",
        encoding="utf-8")

    from curamoir.guards import Degraded

    outcome = Budget(prices=PRICES, monthly_ceiling_usd=15.0)(
        JobContext(job="sweep", repo=repo)
    )

    assert isinstance(outcome, Degraded)
    assert "could not be confirmed" in outcome.message.lower()
    assert "was enforced" not in outcome.message.lower(), "the message still overclaims"


def test_infinite_and_nan_prices_are_rejected():
    """YAML parses .inf, .nan and 1e400 to exactly these. They passed validation,
    then made every job refuse with a message reading "would reach $inf" — and
    nan compares false against everything, so the comparison silently inverted."""
    from curamoir.config import ConfigError, load_config

    for value in (float("inf"), float("nan"), float("-inf")):
        with pytest.raises(ConfigError) as caught:
            load_config({"budget": {"prices": {"m": {"input": value, "output": 1.0}}}})
        assert "budget.prices.m.input" in str(caught.value), value


# --- a ledger line that is valid JSON but not an entry ---------------------

@pytest.mark.parametrize("line", [
    pytest.param("42", id="bare_scalar"),
    pytest.param("null", id="json_null"),
    pytest.param('["sweep", 1]', id="json_list"),
    pytest.param('"a string"', id="json_string"),
])
def test_a_json_valid_non_entry_line_does_not_refuse_every_job(tmp_path, line):
    """`json.loads` succeeding does not make the result an entry.

    The truncated-line case above is caught because it raises ValueError. These
    parse cleanly, so they reached `entry.get(...)` and raised AttributeError —
    not UnknownModel, so it escaped the Degraded path and the gateway turned it
    into GUARD_ERROR. In a file the spec says is append-only and never pruned,
    that refuses every future job until someone hand-edits an audit trail.
    """
    repo = initialised(tmp_path)
    (repo / ".curamoir" / "state" / "ledger.jsonl").write_text(line + "\n", encoding="utf-8")

    gateway = Gateway(guards=[Budget(prices=PRICES, monthly_ceiling_usd=15.0)])
    outcome = gateway.perform(JobContext(job="sweep", repo=repo), lambda: "ran")

    assert outcome == "ran", f"a {line!r} line refused the job: {outcome}"


def test_a_non_entry_line_is_counted_as_unreadable_not_discarded(tmp_path):
    """Skipping it silently would undercount spend, which is the one failure
    the budget guard exists to prevent."""
    ledger = Ledger(tmp_path / "ledger.jsonl")
    ledger._path.write_text("42\nnull\n", encoding="utf-8")

    assert ledger.entries() == []
    assert ledger.unreadable_lines() == 2


def test_token_counts_that_are_not_numbers_degrade_rather_than_crash(tmp_path):
    """A writer emitting "1000" instead of 1000 is a plausible hook bug, and
    multiplying a str by a float raises TypeError deep inside the price table."""
    repo = initialised(tmp_path)
    (repo / ".curamoir" / "state" / "ledger.jsonl").write_text(
        json.dumps({"kind": "job", "job": "sweep", "model": "claude-sonnet-5",
                    "input_tokens": "1000", "output_tokens": "100",
                    "at": "2026-10-08T10:00:00+00:00"}) + "\n",
        encoding="utf-8")

    outcome = Budget(prices=PRICES, monthly_ceiling_usd=15.0)(
        JobContext(job="sweep", repo=repo)
    )

    assert isinstance(outcome, Degraded), outcome
    assert "could not be confirmed" in outcome.message.lower()


# --- an append that lands on a line with no terminator ---------------------

def test_recording_after_a_truncated_line_does_not_eat_the_new_entry(tmp_path):
    """A crash mid-write leaves a line with no trailing newline. Appending
    concatenated onto it, so the crash cost TWO records rather than one — and
    `unreadable_lines()` still said 1, under-reporting its own loss."""
    ledger = Ledger(tmp_path / "ledger.jsonl")
    ledger.record(job="first", reason=None, message="ok")
    with ledger._path.open("a", encoding="utf-8") as handle:
        handle.write('{"kind": "job", "job": "swe')  # crash mid-append

    ledger.record(job="second", reason=None, message="ok")

    jobs = [entry.get("job") for entry in ledger.entries()]
    assert jobs == ["first", "second"], f"the new record was swallowed: {jobs}"
    assert ledger.unreadable_lines() == 1


# --- spend that cannot be attributed to a month ----------------------------

def test_an_entry_with_an_unusable_timestamp_is_not_counted_as_zero(tmp_path):
    """Undercounting is the failure this guard exists to prevent. An entry
    carrying tokens but no parseable `at` is unattributable spend, not $0, and
    with the unreadable tally at 0 nothing downstream hedges the number."""
    ledger = Ledger(tmp_path / "ledger.jsonl")
    ledger._path.write_text(
        json.dumps({"kind": "job", "job": "sweep", "model": "claude-sonnet-5",
                    "input_tokens": 1_000_000, "output_tokens": 1_000_000,
                    "at": "08/10/2026"}) + "\n",
        encoding="utf-8")

    from datetime import datetime

    from curamoir.budget import month_to_date

    spend = month_to_date(ledger, PRICES, datetime(2026, 10, 8, tzinfo=timezone.utc))

    assert spend.usd == 0.0, "an unparseable date cannot be attributed to this month"
    assert spend.unaccounted == 1, (
        "$12 of real spend contributed $0 with no signal that anything was missed"
    )


# --- a ceiling that is not a usable number ---------------------------------

def test_non_finite_and_negative_ceilings_are_rejected():
    """`projected > nan` is always False, so `.nan` turns on_ceiling: pause into
    on_ceiling: ignore with no error. The identical hazard is already rejected
    for budget.prices.*, where the comment explains why."""
    from curamoir.config import ConfigError, load_config

    for key in ("monthly_ceiling_usd", "per_run_ceiling_usd"):
        for value in (float("nan"), float("inf"), float("-inf"), -5.0):
            with pytest.raises(ConfigError) as caught:
                load_config({"budget": {key: value}})
            assert f"budget.{key}" in str(caught.value), (key, value)
