"""E3-R6: the sweep respects the open-PR cap (E0-R6) and the budget (E0-R4).

The guards themselves are proven in tests/guards/. These prove the WIRING at
the sweep's entry point, `job_guard.run_guarded`: built from the repo's own
config, a refusal is the job's `job` line in the repo's own ledger with the
reason, and the body, which is where cm-fkd.4 clones, runs the fix step and
spends, is never called. A spy stands in for that body. The ledger is read
back from the canonical path (the harness's), where the budget reads it.

The open-PR listing is fed the shape `gh pr list --state open --json
headRefName,number,url,body,files,isCrossRepository` really returns (files
as {path, additions, deletions}), mapped through idempotence.sweep_pr_listing
as cm-fkd.4's callable will. The ledger is seeded with job lines in the shape
JobRun writes, plus E11-R1's token attribution.
"""

import json
import os
import socket
import subprocess
import sys
from datetime import datetime, timezone

import pytest

from curamoir.config import DEFAULTS, ConfigError
from curamoir.guards import Reason, Refusal
from curamoir.idempotence import sweep_marker, sweep_pr_listing
from curamoir.job_guard import UNUSABLE_ROOT, GuardedRun, run_guarded
from curamoir.repo_config import strict_config

BASE = "3f9c2a1b7e5d4c6a8b0e1f2d3c4b5a6978877665"
CHEAP = DEFAULTS["models"]["cheap"]
CEILING = DEFAULTS["budget"]["monthly_ceiling_usd"]
OUTPUT_PRICE = DEFAULTS["budget"]["prices"][CHEAP]["output"]


def october():
    """A fixed clock, so month attribution is not a function of the test's date."""
    return datetime(2026, 10, 5, tzinfo=timezone.utc)


def gh_item(number, branch, cross_repository=False):
    """One item of `gh pr list --json headRefName,number,url,body,files,
    isCrossRepository`, as gh prints it."""
    return {
        "headRefName": branch,
        "number": number,
        "url": f"https://github.com/example/repo/pull/{number}",
        "body": f"Sweep fixes.\n\n{sweep_marker(BASE)}\n",
        "files": [{"path": "src/sample/web/handlers.py", "additions": 4, "deletions": 2}],
        "isCrossRepository": cross_repository,
    }


def listing(*items):
    """The open-PR callable cm-fkd.4 will supply, over canned gh output."""
    return lambda: [sweep_pr_listing(item) for item in items]


def seed_spend(ledger, usd):
    """A previous sweep's job line costing `usd` on the cheap model, all output."""
    ledger.record(
        kind="job", job="sweep", reason=None, message="fixed 3 violations",
        run_id="a1b2c3d4e5f6", outcome="success",
        started="2026-10-02T03:00:00+00:00", ended="2026-10-02T03:04:10+00:00",
        model=CHEAP, input_tokens=0, output_tokens=round(usd / OUTPUT_PRICE * 1_000_000),
        at="2026-10-02T03:04:10+00:00",
    )


class Spy:
    """Stands in for the sweep body: the clone, the fix step, the spend."""

    def __init__(self):
        self.calls = 0

    def __call__(self, run):
        self.calls += 1
        run.succeeded("opened curamoir/sweep-2026-10-05")
        return "swept"


def sweep(harness, open_prs):
    """Run the sweep's entry point over the harness repo with a spy body."""
    body = Spy()
    result = run_guarded(harness.repo, "sweep", open_prs, body, now=october)
    return result, body, harness.ledger()


def this_run(ledger):
    """The job line this run wrote: the last one (seeded lines come first)."""
    return [entry for entry in ledger.entries() if entry["kind"] == "job"][-1]


def test_three_open_curamoir_prs_refuse_the_sweep_with_reason_pr_cap(harness):
    seed_spend(harness.ledger(), 1.00)
    open_prs = listing(
        gh_item(41, "curamoir/sweep-2026-09-14"),
        gh_item(44, "curamoir/sweep-2026-09-21"),
        gh_item(47, "curamoir/sweep-2026-09-28"),
    )

    result, body, ledger = sweep(harness, open_prs)

    assert body.calls == 0
    assert (result.outcome, result.reason, result.value) == ("refused", Reason.PR_CAP, None)
    line = this_run(ledger)
    assert (line["outcome"], line["reason"]) == ("refused", "pr_cap")
    assert "curamoir/sweep-2026-09-28" in line["message"]


def test_a_ledger_at_95_percent_of_the_ceiling_refuses_the_sweep_with_reason_budget(harness):
    seed_spend(harness.ledger(), 0.95 * CEILING)

    result, body, ledger = sweep(harness, listing())

    assert body.calls == 0
    assert result.reason is Reason.BUDGET
    line = this_run(ledger)
    assert (line["outcome"], line["reason"]) == ("refused", "budget")
    assert f"{0.95 * CEILING:.2f}" in line["message"]


def test_under_the_cap_and_the_ceiling_the_sweep_runs_from_the_default_branch(harness):
    """Without this, a seam that always refused would pass the two above. It
    runs from `main`, which DefaultBranchGuard would refuse: that guard is
    applied at push time, not here."""
    seed_spend(harness.ledger(), 0.50 * CEILING)
    open_prs = listing(
        gh_item(41, "curamoir/sweep-2026-09-14"),
        gh_item(44, "curamoir/sweep-2026-09-21"),
    )

    result, body, ledger = sweep(harness, open_prs)

    assert harness.current_branch() == "main"
    assert body.calls == 1
    assert (result.outcome, result.value) == ("success", "swept")
    assert this_run(ledger)["outcome"] == "success"
    assert [e for e in ledger.entries() if e["kind"] == "guard"] == []


def test_a_developers_pr_and_a_forks_curamoir_pr_do_not_count_towards_the_cap(harness):
    """Two of cúramóir's own plus two that are not: under the cap of three."""
    open_prs = listing(
        gh_item(41, "curamoir/sweep-2026-09-14"),
        gh_item(44, "curamoir/sweep-2026-09-21"),
        gh_item(45, "feature/login"),
        gh_item(46, "curamoir/sweep-2026-09-25", cross_repository=True),
    )

    result, body, ledger = sweep(harness, open_prs)

    assert body.calls == 1
    assert (result.outcome, result.value) == ("success", "swept")
    assert this_run(ledger)["outcome"] == "success"
    assert [e for e in ledger.entries() if e["kind"] == "guard"] == []


def test_the_configured_cap_is_the_one_enforced(harness):
    harness.write(".curamoir/curamoir.yaml", "version: 1\nsweep:\n  max_open_prs: 1\n")

    result, body, ledger = sweep(harness, listing(gh_item(41, "curamoir/sweep-2026-09-14")))

    assert body.calls == 0
    assert result.reason is Reason.PR_CAP
    assert this_run(ledger)["reason"] == "pr_cap"


def test_the_configured_ceiling_is_the_one_enforced(harness):
    """$4 spent plus the $2 estimate is under the default $15 and over a
    configured $5."""
    harness.write(".curamoir/curamoir.yaml", "version: 1\nbudget:\n  monthly_ceiling_usd: 5\n")
    seed_spend(harness.ledger(), 4.00)

    result, body, ledger = sweep(harness, listing())

    assert body.calls == 0
    assert result.reason is Reason.BUDGET
    assert (this_run(ledger)["outcome"], this_run(ledger)["reason"]) == ("refused", "budget")


@pytest.mark.parametrize(("per_run_ceiling", "outcome", "calls"), [
    (0.99, "success", 1),
    (1.01, "refused", 0),
])
def test_the_configured_per_run_ceiling_is_the_projected_cost_of_this_run(
        harness, per_run_ceiling, outcome, calls):
    """cm-7ck: $14 spent leaves $1 under the default $15 ceiling, so the
    configured budget.per_run_ceiling_usd alone decides the run, a cent
    either side of the boundary."""
    harness.write(".curamoir/curamoir.yaml",
                  f"version: 1\nbudget:\n  per_run_ceiling_usd: {per_run_ceiling}\n")
    seed_spend(harness.ledger(), CEILING - 1.00)

    result, body, ledger = sweep(harness, listing())

    assert body.calls == calls
    assert result.outcome == this_run(ledger)["outcome"] == outcome


def test_the_configured_prices_are_the_ones_spend_is_computed_from(harness):
    """Tokens worth $7 at the default price are $14 at a configured double
    price: $14 plus the $2 estimate breaches the default $15 ceiling."""
    harness.write(".curamoir/curamoir.yaml", (
        "version: 1\nbudget:\n  prices:\n"
        f"    {CHEAP}: {{input: 2.00, output: {2 * OUTPUT_PRICE:.2f}}}\n"))
    seed_spend(harness.ledger(), 7.00)

    result, body, ledger = sweep(harness, listing())

    assert body.calls == 0
    assert result.reason is Reason.BUDGET
    assert this_run(ledger)["reason"] == "budget"


def test_on_ceiling_warn_records_a_degraded_budget_check_and_the_sweep_runs(harness):
    harness.write(".curamoir/curamoir.yaml", "version: 1\nbudget:\n  on_ceiling: warn\n")
    seed_spend(harness.ledger(), 0.95 * CEILING)

    result, body, ledger = sweep(harness, listing())

    assert body.calls == 1
    assert (result.outcome, result.value) == ("success", "swept")
    guard_lines = [e for e in ledger.entries() if e["kind"] == "guard"]
    assert [(e["reason"], e["severity"]) for e in guard_lines] == [("budget", "degraded")]


def test_the_configured_branch_prefix_is_the_one_counted(harness):
    harness.write(".curamoir/curamoir.yaml", "version: 1\nguardrails:\n  branch_prefix: bot/\n")
    open_prs = listing(gh_item(41, "bot/sweep-1"), gh_item(44, "bot/sweep-2"),
                       gh_item(47, "bot/sweep-3"))

    result, body, ledger = sweep(harness, open_prs)

    assert body.calls == 0
    assert result.reason is Reason.PR_CAP
    assert this_run(ledger)["reason"] == "pr_cap"


def test_the_kill_switch_is_checked_before_the_budget_and_the_cap(harness):
    """E10-R1's order on a valid config: a paused repo over the ceiling and at
    the cap is refused as paused, so the brief says why the right way."""
    harness.write(".curamoir/PAUSE", "")
    seed_spend(harness.ledger(), 0.95 * CEILING)
    open_prs = listing(
        gh_item(41, "curamoir/sweep-2026-09-14"),
        gh_item(44, "curamoir/sweep-2026-09-21"),
        gh_item(47, "curamoir/sweep-2026-09-28"),
    )

    result, body, ledger = sweep(harness, open_prs)

    assert body.calls == 0
    assert result.reason is Reason.PAUSED
    assert this_run(ledger)["reason"] == "paused"


def test_a_pause_file_refuses_the_sweep_with_reason_paused(harness):
    harness.write(".curamoir/PAUSE", "")

    result, body, ledger = sweep(harness, listing())

    assert body.calls == 0
    assert result.reason is Reason.PAUSED
    assert (this_run(ledger)["outcome"], this_run(ledger)["reason"]) == ("refused", "paused")


def test_a_live_lock_refuses_the_sweep_with_reason_locked(harness):
    """This process, on this host, started now: a lock that is live."""
    harness.write(".curamoir/state/lock", json.dumps({
        "pid": os.getpid(), "host": socket.gethostname(), "job": "review",
        "started": datetime.now(timezone.utc).isoformat(),
    }))

    result, body, ledger = sweep(harness, listing())

    assert body.calls == 0
    assert result.reason is Reason.LOCKED
    assert (this_run(ledger)["outcome"], this_run(ledger)["reason"]) == ("refused", "locked")


def test_open_prs_that_cannot_be_listed_degrade_the_cap_and_the_sweep_runs(harness):
    """The cap's documented outage policy, seen from the sweep: allowed, and
    a degraded pr_cap guard line says the cap was not enforced."""
    def gh_outage():
        raise OSError("gh: could not resolve host api.github.com")

    result, body, ledger = sweep(harness, gh_outage)

    assert body.calls == 1
    assert (result.outcome, result.value) == ("success", "swept")
    guard_lines = [e for e in ledger.entries() if e["kind"] == "guard"]
    assert [(e["reason"], e["severity"]) for e in guard_lines] == [("pr_cap", "degraded")]
    assert this_run(ledger)["outcome"] == "success"


def test_a_label_source_that_raises_degrades_the_kill_switch_and_the_sweep_runs(harness):
    """fix_scope step 0 (ii): the label callable must time out and raise.
    A raise is this: the PAUSE file still checked, a degraded `paused`
    guard line, and the job runs and writes its line. A hang the scheduler
    then kills would write nothing at all."""
    def gh_timeout():
        raise TimeoutError("gh label list timed out after 30s")

    body = Spy()
    result = run_guarded(harness.repo, "sweep", listing(), body, now=october,
                         repo_labels=gh_timeout)
    ledger = harness.ledger()

    assert body.calls == 1
    assert (result.outcome, result.value) == ("success", "swept")
    guard_lines = [e for e in ledger.entries() if e["kind"] == "guard"]
    assert [(e["reason"], e["severity"]) for e in guard_lines] == [("paused", "degraded")]
    assert "timed out" in guard_lines[0]["message"]
    assert this_run(ledger)["outcome"] == "success"


def test_an_invalid_config_refuses_rather_than_falling_back_to_the_default_ceiling(harness):
    """$10 spent is under the default $15 ceiling but over the configured $5.
    The typo makes the file invalid; falling back to the defaults would run
    the sweep under a ceiling three times the one the developer set."""
    harness.write(".curamoir/curamoir.yaml",
                  "version: 1\nbudget:\n  monthly_ceiling_usd: 5\nsweeep:\n  max_open_prs: 1\n")
    seed_spend(harness.ledger(), 10.00)

    result, body, ledger = sweep(harness, listing())

    assert body.calls == 0
    assert result.reason is Reason.GUARD_ERROR
    line = this_run(ledger)
    assert (line["outcome"], line["reason"]) == ("refused", "guard_error")
    assert "sweeep" in line["message"]


def test_a_config_that_is_not_valid_yaml_refuses_with_guard_error(harness):
    harness.write(".curamoir/curamoir.yaml", "version: 1\nbudget: [unclosed\n")

    result, body, ledger = sweep(harness, listing())

    assert body.calls == 0
    assert result.reason is Reason.GUARD_ERROR
    line = this_run(ledger)
    assert (line["outcome"], line["reason"]) == ("refused", "guard_error")
    assert "not valid YAML" in line["message"]
    assert "\n" not in line["message"]


def test_a_paused_repo_with_a_broken_config_is_refused_as_paused(harness):
    """The config is read before any guard runs; its failure must still wait
    behind the kill switch, which E10-R1 puts first."""
    harness.write(".curamoir/curamoir.yaml", "version: 1\nbudget: [unclosed\n")
    harness.write(".curamoir/PAUSE", "")

    result, body, ledger = sweep(harness, listing())

    assert body.calls == 0
    assert result.reason is Reason.PAUSED
    assert this_run(ledger)["reason"] == "paused"


def test_a_config_path_that_is_a_directory_refuses_with_guard_error(harness):
    (harness.repo / ".curamoir" / "curamoir.yaml").mkdir(parents=True)

    result, body, ledger = sweep(harness, listing())

    assert body.calls == 0
    assert result.reason is Reason.GUARD_ERROR
    assert (this_run(ledger)["outcome"], this_run(ledger)["reason"]) == ("refused", "guard_error")
    assert "cannot be read" in this_run(ledger)["message"]


def test_a_config_that_is_a_dangling_symlink_refuses_with_guard_error(harness):
    (harness.repo / ".curamoir").mkdir(exist_ok=True)
    (harness.repo / ".curamoir" / "curamoir.yaml").symlink_to(harness.repo / "gone.yaml")

    result, body, ledger = sweep(harness, listing())

    assert body.calls == 0
    assert result.reason is Reason.GUARD_ERROR
    assert (this_run(ledger)["outcome"], this_run(ledger)["reason"]) == ("refused", "guard_error")


def test_a_repo_root_that_cannot_be_inspected_runs_nothing_and_creates_nothing(tmp_path, capsys):
    missing = tmp_path / "moved-away"
    body = Spy()

    result = run_guarded(missing, "sweep", listing(), body)

    assert result == GuardedRun(outcome=UNUSABLE_ROOT, reason=None, value=None)
    assert body.calls == 0
    assert not missing.exists()
    assert "moved-away is not a directory" in capsys.readouterr().err


def test_a_repo_root_of_none_runs_nothing(capsys):
    body = Spy()

    result = run_guarded(None, "sweep", listing(), body)

    assert result == GuardedRun(outcome=UNUSABLE_ROOT, reason=None, value=None)
    assert body.calls == 0
    assert "the repo root is unknown" in capsys.readouterr().err


def test_a_paused_label_refuses_the_sweep_with_reason_paused(harness):
    """The label half of the kill switch: cm-fkd.4 supplies the `gh` call."""
    body = Spy()

    result = run_guarded(harness.repo, "sweep", listing(), body, now=october,
                         repo_labels=lambda: ["curamoir:paused"])

    assert body.calls == 0
    assert (result.outcome, result.reason) == ("refused", Reason.PAUSED)
    line = this_run(harness.ledger())
    assert (line["outcome"], line["reason"]) == ("refused", "paused")
    assert "curamoir:paused" in line["message"]


def test_the_body_reads_history_from_the_ledger_its_line_is_written_to(harness):
    """Step 2b's `ledger.entries()` is `run.ledger.entries()`: the repo's own
    ledger, the one the budget reads and the job line goes to."""
    seed_spend(harness.ledger(), 1.00)
    seen = []

    def body(run):
        seen.extend(run.ledger.entries())
        run.succeeded("read the history")

    run_guarded(harness.repo, "sweep", listing(), body, now=october)

    assert [entry["run_id"] for entry in seen] == ["a1b2c3d4e5f6"]
    assert this_run(harness.ledger())["outcome"] == "success"


def test_a_config_that_is_a_fifo_refuses_rather_than_hanging(harness):
    """Opening a FIFO blocks until something writes to it, which unattended
    is never: the sweep would hang until the scheduler killed it, unrecorded."""
    (harness.repo / ".curamoir").mkdir(exist_ok=True)
    os.mkfifo(harness.repo / ".curamoir" / "curamoir.yaml")

    result, body, ledger = sweep(harness, listing())

    assert body.calls == 0
    assert (result.outcome, result.reason) == ("refused", Reason.GUARD_ERROR)
    assert "not a regular file" in this_run(ledger)["message"]


def test_a_config_whose_bytes_are_not_utf8_refuses_with_guard_error(harness):
    """A decode failure is a ValueError, not an OSError; it must reach the
    sweep as a refusal with a reason, not escape run_guarded as a crash."""
    (harness.repo / ".curamoir").mkdir(exist_ok=True)
    (harness.repo / ".curamoir" / "curamoir.yaml").write_bytes(b"\xff\xfe")

    result, body, ledger = sweep(harness, listing())

    assert body.calls == 0
    assert (result.outcome, result.reason) == ("refused", Reason.GUARD_ERROR)
    assert (this_run(ledger)["outcome"], this_run(ledger)["reason"]) == ("refused", "guard_error")
    assert "cannot be read" in this_run(ledger)["message"]


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="root reads a directory whatever its mode")
def test_a_config_symlinked_into_an_unreadable_directory_refuses_with_guard_error(harness):
    """The link itself is there, so the config is not absent; asking whether
    its target is a regular file raises EACCES (pathlib re-raises it on
    3.12), which must still reach the caller as a refusal with a reason."""
    locked = harness.repo.parent / "locked"
    locked.mkdir()
    (locked / "curamoir.yaml").write_text("version: 1\n", encoding="utf-8")
    (harness.repo / ".curamoir").mkdir(exist_ok=True)
    (harness.repo / ".curamoir" / "curamoir.yaml").symlink_to(locked / "curamoir.yaml")
    locked.chmod(0)
    try:
        with pytest.raises(ConfigError, match="not a regular file"):
            strict_config(harness.repo)
        result, body, ledger = sweep(harness, listing())
    finally:
        locked.chmod(0o755)

    assert body.calls == 0
    assert (result.outcome, result.reason) == ("refused", Reason.GUARD_ERROR)
    assert (this_run(ledger)["outcome"], this_run(ledger)["reason"]) == ("refused", "guard_error")


def test_a_refusal_the_body_returns_is_its_value_not_a_guard_refusal(harness):
    """A Refusal can come from a guard or from the body; the result says which."""
    def body(run):
        refusal = Refusal(reason=Reason.IDEMPOTENCE, message="open sweep PRs unknown")
        run.refused(refusal)
        return refusal

    result = run_guarded(harness.repo, "sweep", listing(), body, now=october)

    assert (result.outcome, result.reason) == ("refused", Reason.IDEMPOTENCE)
    assert result.value == Refusal(reason=Reason.IDEMPOTENCE, message="open sweep PRs unknown")


def test_a_body_that_returns_none_is_told_apart_from_an_unusable_root(harness):
    def body(run):
        run.succeeded("nothing to return")

    result = run_guarded(harness.repo, "sweep", listing(), body, now=october)

    assert result == GuardedRun(outcome="success", reason=None, value=None)


def test_a_body_that_reports_nothing_returns_the_unknown_outcome(harness):
    result = run_guarded(harness.repo, "sweep", listing(), lambda run: None, now=october)

    assert (result.outcome, result.reason) == ("unknown", None)
    assert this_run(harness.ledger())["outcome"] == "unknown"


def test_the_injected_clock_dates_the_job_line_as_well_as_the_budget(harness):
    """One clock for the run: the line's start and end are the budget's now."""
    result, _body, ledger = sweep(harness, listing())

    line = this_run(ledger)
    assert result.outcome == "success"
    assert (line["started"], line["ended"]) == (october().isoformat(),) * 2


def test_job_guard_imports_first_in_a_fresh_interpreter():
    """The tests above import curamoir.guards first, which hides an import
    cycle (cm-g3c) that a runner importing job_guard first would hit."""
    result = subprocess.run([sys.executable, "-c", "import curamoir.job_guard"],
                            capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr


def test_a_long_list_of_config_problems_is_capped_in_the_message():
    """The guard_error message lands on a ledger line and in a terminal, so
    it is bounded: the first 20 problems, then how many more there were.
    A short list is given in full."""
    from types import SimpleNamespace

    from curamoir.job_guard import _refuse_invalid_config

    many = SimpleNamespace(problems=[f"problem {n}" for n in range(25)])
    message = _refuse_invalid_config(many)(None).message
    assert "problem 19" in message and "problem 20" not in message
    assert message.count("problem ") == 20
    assert "(+5 more)" in message

    few = SimpleNamespace(problems=["sweep.max_open_prs: not a number",
                                    "budget.on_ceiling: not one of pause, ignore"])
    message = _refuse_invalid_config(few)(None).message
    assert message.startswith("; ".join(few.problems) + ".")
    assert "more)" not in message


def test_a_config_error_naming_no_problem_still_says_the_config_is_invalid():
    """A ConfigError with an empty list of problems still gives a message
    that says what is wrong, rather than one that opens on a bare full
    stop."""
    from curamoir.job_guard import _refuse_invalid_config

    message = _refuse_invalid_config(ConfigError([]))(None).message
    assert message.startswith("the config is invalid. ")
