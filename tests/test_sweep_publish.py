"""cm-fkd.14 (cm-fkd.4's ST5): the sweep body's second half, fix_scope's
runner steps 3 to 10, and the sweep's registration (curamoir.sweep_job,
curamoir.runner).

Acceptance criteria, and the tests that hold them:

- "green: PR created; success line has pr, fixes, test_gate, tier1":
  test_green_opens_one_pr_and_the_success_line_has_pr_fixes_gate_and_tier1.
- "reduced: the dropped path is absent from the commit and the body":
  test_reduced_leaves_the_dropped_path_out_of_the_commit_and_the_fixes. The
  body still names the dropped file under "Test gate" and "Files left for
  the next run", which sweep_pr's layout requires (E3-R3's "rest"); it is
  absent from the commit, from the body's "Fixes" and from `fixes`.
- "abandoned: no push, no gh create call, failure line":
  test_abandoned_pushes_nothing_creates_no_pr_and_fails.
- "out-of-scope edit: refusal, no push":
  test_an_out_of_scope_edit_is_refused_and_nothing_is_pushed.
- "PR creation fails: failure line with no fixes (and branch deleted per
  ST3)": test_a_pr_that_fails_to_open_is_a_failure_with_no_fixes_and_no_branch.
- "crash after the PR opened: the line keeps pr":
  test_a_crash_after_the_pr_opened_keeps_pr_on_the_line.
- "fix step times out: UNRECORDED":
  test_a_fix_step_that_times_out_is_unrecorded_spend_and_no_pr.
- "The fix step's argv grants no Bash or any shell tool":
  test_the_fix_steps_argv_grants_no_shell_tool (and the green test).

**The world** is tests/test_sweep_job.py's: real git, the committed
sample checked out with origin naming a GitHub repository that the
checkout's own insteadOf/pushInsteadOf send to a local bare repository
(so the push really lands there, and nowhere else), HOME and
XDG_CONFIG_HOME isolated (autouse `isolated_home`, imported), the fake gh
of tests/conftest.py (`no_real_gh`, which records `pr create`'s body), and
stand-in `claude` scripts (tests/test_tier1.fake_claude) that make the
fix's edits. The real lint, the real gate (a grep-style test command), the
real ledger. No test reaches GitHub, the real gh or a model.
"""

import contextlib
import dataclasses
import json
import os
import pathlib
import shutil
import signal
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone

import pytest

from curamoir import github, publish, runner, sweep_job
from curamoir.budget import UNRECORDED, month_to_date
from curamoir.fix_counts import AGENT, REMEDY, RULE, read_snapshot, snapshot_path
from curamoir.fix_scope import ScopeCheckError
from curamoir.fix_step import FIX_TOOLS, usable_number
from curamoir.git_guard import GitGuardError
from curamoir.green_gate import ABANDONED, GREEN, REDUCED, GateError, GateResult
from curamoir.guards.reasons import Reason
from curamoir.idempotence import WORK, parse_sweep_marker
from curamoir.job_run import FAILURE, REFUSED, SUCCESS, JobRun
from curamoir.ledger import repo_ledger
from curamoir.sweep import LintUntrusted
from curamoir.sweep_job import (
    NO_FILES_ALLOWED,
    NOTHING_CHANGED,
    NOTHING_REMEDIED,
    PR_MAY_EXIST,
    PUBLISH_PENDING,
    SweepTools,
    run_sweep,
    sweep_day,
)
from curamoir.sweep_pr import SweepBodyError
from curamoir.termination import Terminated
from curamoir.tier1 import OFF, SCANNED
from tests import claude_payloads
from tests.conftest import NO_REAL_CLAUDE
from tests.test_lint import NO_BARE_PRINT, NO_VARIANT_MODULE
from tests.test_sweep_job import (  # noqa: F401  (isolated_home is an autouse fixture)
    GH_REPO,
    MODEL,
    PRICES,
    base_of,
    checkout,
    git,
    isolated_home,
    job_lines,
)
from tests.test_tier1 import (
    BILLING,
    ECC,
    EDITS,
    FOUND_BILLING,
    HANDLERS,
    VARIANT,
    fake_claude,
    findings_reply,
)

#: A grep-style test command: green unless a file under src/ holds BREAKS.
GATE = "version: 1\nsweep:\n  tier1_scan: false\n  test_command: \"! grep -rq BREAKS src\"\n"
#: The two planted prints' fixes (tests/test_tier1.EDITS): one per file.
PRINT_FIXES = [EDITS[0], EDITS[2]]
#: A fix of the handlers print that the test command finds red.
RED_HANDLERS_FIX = (HANDLERS, 'print(f"request failed: {exception}")',
                    'logger.error("BREAKS %s", exception)')
#: The clock every sweep here runs on: late on the 27th west of UTC, so the
#: UTC day (the 28th) is not the local one.
NOW = datetime(2026, 10, 27, 23, 30, tzinfo=timezone(timedelta(hours=-5)))
DAY = "2026-10-28"


def sweep(repo, run_class=JobRun, crashes=None, **tools):
    """run_sweep on a JobRun (or `run_class`) writing to `repo`'s ledger,
    with the fixed clock; its line. Nothing is swallowed: a test that means
    the run to crash names the exception as `crashes`, and the sweep must
    raise exactly that (JobRun has written its failure line by then)."""
    tools.setdefault("now", lambda: NOW)
    expected = pytest.raises(crashes) if crashes else contextlib.nullcontext()
    with expected, run_class(repo_ledger(repo), "sweep") as run:
        run_sweep(repo, run, SweepTools(**tools))
    return job_lines(repo)[-1]


def fixer(tmp_path, edits=PRINT_FIXES, reply="done", **options):
    """A stand-in fix agent making `edits`; (path, log)."""
    return fake_claude(tmp_path, "fix", reply, edits=edits, **options)


def creates(gh):
    return [record for record in gh.call_records() if record["argv"][:2] == ["pr", "create"]]


def origin_branches(tmp_path):
    return git(tmp_path / "origin.git", "for-each-ref", "--format=%(refname)",
               "refs/heads").split()


def nothing_pushed(tmp_path, gh):
    return origin_branches(tmp_path) == ["refs/heads/main"] and creates(gh) == []


def changed_on(tmp_path, branch, base):
    """The paths `branch` on origin changes from `base`."""
    return git(tmp_path / "origin.git", "diff", "--name-only", base, branch).split()


def section(body, heading):
    """The lines of `body` under `heading`, up to the next heading."""
    lines = body.split("\n")
    start = lines.index(heading) + 1
    end = next((i for i in range(start, len(lines)) if lines[i].startswith("#")), len(lines))
    return "\n".join(lines[start:end])


def dispatching_claude(tmp_path, scan, fix):
    """One `claude` for both calls, as SweepTools has: the scan's stand-in
    when the toolset has no Edit (tier1.SCAN_TOOLS), else the fix's."""
    script = tmp_path / "bin" / "both"
    script.write_text(
        f"#!{sys.executable}\nimport os, sys\n"
        "tools = sys.argv[sys.argv.index('--tools') + 1].split(',')\n"
        f"target = {fix!r} if 'Edit' in tools else {scan!r}\n"
        "os.execv(target, [target, *sys.argv[1:]])\n", encoding="utf-8")
    script.chmod(0o755)
    return str(script)


@pytest.fixture(autouse=True)
def quick_lookups(monkeypatch):
    monkeypatch.setattr(publish, "LOOKUP_DELAY", 0)


# --- the acceptance criteria -----------------------------------------------------------

def test_green_opens_one_pr_and_the_success_line_has_pr_fixes_gate_and_tier1(tmp_path,
                                                                            no_real_gh):
    repo = checkout(tmp_path, NO_BARE_PRINT, config=GATE)
    base = base_of(repo)
    head_before = git(repo, "rev-parse", "HEAD")
    status_before = git(repo, "status", "--porcelain", "--", ":!.curamoir/state")
    claude, log = fixer(tmp_path)

    line = sweep(repo, claude=claude)

    assert line["outcome"] == SUCCESS, line["message"]
    branch = f"curamoir/sweep-{DAY}-{base[:8]}"
    [create] = creates(no_real_gh)
    argv = create["argv"]
    assert argv[argv.index("--repo") + 1] == GH_REPO
    assert argv[argv.index("--head") + 1] == branch
    assert argv[argv.index("--base") + 1] == "main"
    assert argv[argv.index("--title") + 1] == f"curamoir sweep {DAY}: 2 fixes of 1 principle"
    body = create["body_file_content"]
    assert parse_sweep_marker(body) == base
    assert BILLING in section(body, "### Fixes") and HANDLERS in section(body, "### Fixes")
    # The line: pr, fixes, test_gate, tier1, and the decision's fields.
    assert line["pr"] == {"number": 1, "branch": branch,
                          "url": "https://github.com/acme/widgets/pull/1", "base": base,
                          "paths": sorted([BILLING, HANDLERS])}
    assert sorted(fix["path"] for fix in line["fixes"]) == sorted([BILLING, HANDLERS])
    assert {(fix["source"], fix["fixed_by"]) for fix in line["fixes"]} == {(RULE, AGENT)}
    assert line["fix_summary"] == {"structured-logging": 2}
    assert line["test_gate"]["strategy"] == GREEN
    assert line["tier1"]["status"] == OFF
    assert line["work"] == WORK and line["found_delta"]["base"] == base
    assert line["message"] == (f"opened https://github.com/acme/widgets/pull/1: "
                               f"curamoir sweep {DAY}: 2 fixes of 1 principle")
    # The fix step's spend, at the base's models.cheap.
    usage = claude_payloads.USAGE
    assert (line["input_tokens"], line["output_tokens"], line["model"], line["cost_usd"]) == (
        usage["input_tokens"], usage["output_tokens"], MODEL, 0.02)
    # Exactly the two files landed, on the sweep branch, and the checkout
    # itself is untouched: the agent ran in the sandbox, never here.
    assert origin_branches(tmp_path) == [f"refs/heads/{branch}", "refs/heads/main"]
    assert sorted(changed_on(tmp_path, branch, base)) == sorted([BILLING, HANDLERS])
    assert json.loads(log.read_text())["cwd"] != str(repo)
    assert git(repo, "rev-parse", "HEAD") == head_before
    # (Only the runner's own state, the ledger and the snapshot, is new.)
    assert git(repo, "status", "--porcelain", "--", ":!.curamoir/state") == status_before
    assert read_snapshot(snapshot_path(repo))["base"] == base
    # The decision's listing named origin's repository (one create, above).
    lists = [r["argv"] for r in no_real_gh.call_records() if r["argv"][:2] == ["pr", "list"]]
    assert lists and all(a[a.index("--repo") + 1] == GH_REPO for a in lists)


def test_reduced_leaves_the_dropped_path_out_of_the_commit_and_the_fixes(tmp_path,
                                                                        no_real_gh):
    repo = checkout(tmp_path, NO_BARE_PRINT, config=GATE)
    base = base_of(repo)
    claude, _ = fixer(tmp_path, edits=[EDITS[0], RED_HANDLERS_FIX])

    line = sweep(repo, claude=claude)

    assert line["outcome"] == SUCCESS, line["message"]
    assert line["test_gate"]["strategy"] == REDUCED
    assert [d["path"] for d in line["test_gate"]["dropped"]] == [HANDLERS]
    assert line["pr"]["paths"] == [BILLING]
    assert [fix["path"] for fix in line["fixes"]] == [BILLING]
    assert changed_on(tmp_path, line["pr"]["branch"], base) == [BILLING]
    body = creates(no_real_gh)[0]["body_file_content"]
    assert HANDLERS not in section(body, "### Fixes")
    assert BILLING in section(body, "### Fixes")
    # Named as left for the next run, which is where the layout puts it.
    assert HANDLERS in section(body, "### Files left for the next run")


def test_abandoned_pushes_nothing_creates_no_pr_and_fails(tmp_path, no_real_gh):
    """The test command is red at the base too: the gate abandons (base_red)."""
    repo = checkout(tmp_path, NO_BARE_PRINT,
                    config=GATE.replace("! grep -rq BREAKS src", "false"))
    claude, _ = fixer(tmp_path)

    line = sweep(repo, claude=claude)

    assert line["outcome"] == FAILURE and line["message"].startswith("no PR: ")
    assert line["test_gate"]["strategy"] == ABANDONED
    assert "pr" not in line and "fixes" not in line
    assert line["found_delta"]["base"] == base_of(repo) and line["work"] == WORK
    assert line["tier1"]["status"] == OFF
    assert nothing_pushed(tmp_path, no_real_gh)
    assert read_snapshot(snapshot_path(repo)) is not None


def test_an_out_of_scope_file_is_dropped_and_the_rest_opens_a_pr(tmp_path, no_real_gh):
    """cm-9ab (user decision 2026-10-28): drop and re-gate. The first
    dogfood sweep spent $1 and kept nothing because one file broke E3-R2.
    Now the breaching file is restored to the base, the review runs again on
    what is left, and the PR carries the in-scope fixes; the line records
    what was dropped and why."""
    repo = checkout(tmp_path, NO_BARE_PRINT, config=GATE)
    claude, _ = fixer(tmp_path, edits=[*PRINT_FIXES, ("README.md", "#", "# Rewritten\n#")])

    line = sweep(repo, claude=claude)

    assert line["outcome"] == SUCCESS, line["message"]
    assert "README.md" not in line["pr"]["paths"]
    assert sorted(fix["path"] for fix in line["fixes"]) == sorted([BILLING, HANDLERS])
    dropped = line["scope_dropped"]
    assert [breach["path"] for breach in dropped] == ["README.md"]
    assert dropped[0]["kind"] == "file_not_in_work_list"


def test_when_every_changed_file_breaches_the_run_is_refused(tmp_path, no_real_gh):
    repo = checkout(tmp_path, NO_BARE_PRINT, config=GATE)
    claude, _ = fixer(tmp_path, edits=[("README.md", "#", "# Rewritten\n#")])

    line = sweep(repo, claude=claude)

    assert line["outcome"] == REFUSED and line["reason"] == Reason.SCOPE.value
    assert "out of scope" in line["message"]
    assert line["scope"]["ok"] is False
    assert {b["path"] for b in line["scope"]["breaches"]} == {"README.md"}
    assert line["tier1"]["status"] == OFF
    assert "found_delta" not in line and "fixes" not in line and "pr" not in line
    assert nothing_pushed(tmp_path, no_real_gh)
    # The call ran: its spend is on the refusal too.
    assert line["input_tokens"] == claude_payloads.USAGE["input_tokens"]


def test_a_pr_that_fails_to_open_is_a_failure_with_no_fixes_and_no_branch(tmp_path,
                                                                         no_real_gh):
    repo = checkout(tmp_path, NO_BARE_PRINT, config=GATE)
    claude, _ = fixer(tmp_path)
    no_real_gh.feed(fail_on={"pr create": "HTTP 422: Validation Failed"})

    line = sweep(repo, claude=claude)

    assert line["outcome"] == FAILURE
    assert "HTTP 422" in line["message"]
    assert "deleted from origin again" in line["message"]
    assert "pr" not in line and "fixes" not in line
    assert line["test_gate"]["strategy"] == GREEN and line["work"] == WORK
    # Pushed, then deleted again (publish's PrCreateFailed).
    assert origin_branches(tmp_path) == ["refs/heads/main"]
    assert len(creates(no_real_gh)) == 1


def test_a_crash_after_the_pr_opened_keeps_pr_on_the_line(tmp_path, no_real_gh):
    """The run ends as the success is being recorded: JobRun writes the
    crash's failure line, which still carries the `pr` noted the moment the
    PR existed, and no `fixes`."""
    repo = checkout(tmp_path, NO_BARE_PRINT, config=GATE)
    claude, _ = fixer(tmp_path)

    class CrashingRun(JobRun):
        def succeeded(self, summary="", **fields):
            raise RuntimeError("the machine went to sleep")

    line = sweep(repo, run_class=CrashingRun, crashes=RuntimeError, claude=claude)

    assert line["outcome"] == FAILURE and "went to sleep" in line["message"]
    assert line["pr"]["number"] == 1 and line["pr"]["base"] == base_of(repo)
    assert "fixes" not in line
    assert len(creates(no_real_gh)) == 1


def test_a_fix_step_that_times_out_is_unrecorded_spend_and_no_pr(tmp_path, no_real_gh):
    repo = checkout(tmp_path, NO_BARE_PRINT, config=GATE)
    claude, _ = fixer(tmp_path)
    pid_file = tmp_path / "fix.pid"
    pathlib.Path(claude).write_text(
        f"#!{sys.executable}\n"
        "import os, time\n"
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
        "time.sleep(60)\n",
        encoding="utf-8")
    try:
        line = sweep(repo, claude=claude, fix_timeout=1)
    finally:
        if pid_file.exists():
            with contextlib.suppress(ProcessLookupError, ValueError):
                os.kill(int(pid_file.read_text()), signal.SIGKILL)

    assert line["outcome"] == FAILURE and "timed out" in line["message"]
    assert line["input_tokens"] == line["output_tokens"] == UNRECORDED
    assert line["model"] == MODEL and "cost_usd" not in line
    assert line["work"] == WORK and "found_delta" in line
    assert nothing_pushed(tmp_path, no_real_gh)
    spend = month_to_date(repo_ledger(repo), PRICES, datetime.now(timezone.utc))
    assert spend.unrecorded == 1 and spend.usd == 0.0


def test_the_fix_steps_argv_grants_no_shell_tool(tmp_path, no_real_gh):
    repo = checkout(tmp_path, NO_BARE_PRINT, config=GATE)
    claude, log = fixer(tmp_path)

    sweep(repo, claude=claude)

    argv = json.loads(log.read_text())["argv"]
    tools = argv[argv.index("--tools") + 1].split(",")
    assert tools == list(FIX_TOOLS) == ["Read", "Grep", "Glob", "Edit"]
    allowed = argv[argv.index("--allowedTools") + 1]
    # No shell tool, and nothing that could run or write one: no Task
    # (a subagent with its own tools) and no Write (new files).
    for forbidden in ("Bash", "Shell", "PowerShell", "Task", "Write"):
        assert forbidden not in tools and not allowed.startswith(f"{forbidden}(")
        assert f",{forbidden}(" not in allowed
    assert "--restricted" in argv and argv[argv.index("--permission-mode") + 1] == "dontAsk"


# --- the line's cost across both calls -------------------------------------------------

def test_the_scan_and_the_fix_step_are_summed_on_the_line(tmp_path, no_real_gh):
    repo = checkout(tmp_path, NO_BARE_PRINT, config=GATE.replace(
        "  tier1_scan: false\n", ""))
    scan, scan_log = fake_claude(tmp_path, "scan", findings_reply())
    fix, fix_log = fixer(tmp_path)

    line = sweep(repo, claude=dispatching_claude(tmp_path, scan, fix))

    assert scan_log.exists() and fix_log.exists()
    assert line["outcome"] == SUCCESS, line["message"]
    assert line["tier1"]["status"] == SCANNED
    usage = claude_payloads.USAGE
    assert (line["input_tokens"], line["output_tokens"], line["model"]) == (
        2 * usage["input_tokens"], 2 * usage["output_tokens"], MODEL)
    assert line["cost_usd"] == pytest.approx(0.04)


@pytest.mark.parametrize("stop", [Terminated(signal.SIGTERM), RuntimeError("crash")],
                         ids=["Terminated", "RuntimeError"])
def test_a_run_that_ends_during_the_fix_call_drops_the_scans_cost_as_the_whole(
        tmp_path, no_real_gh, monkeypatch, stop):
    """The scan reported its cost; the run ends while the fix step's call is
    under way. The line says both counts are UNRECORDED and carries no
    `cost_usd`: the scan's figure alone would read as the line's total."""
    repo = checkout(tmp_path, NO_BARE_PRINT, config=GATE.replace(
        "  tier1_scan: false\n", ""))
    scan, _ = fake_claude(tmp_path, "scan", findings_reply())

    def ends_during_the_call(sandbox, prompt, before_call=None, **options):
        before_call()
        raise stop

    monkeypatch.setattr(sweep_job, "run_fix_step", ends_during_the_call)

    with contextlib.suppress(type(stop)), JobRun(repo_ledger(repo), "sweep") as run:
        run_sweep(repo, run, SweepTools(claude=scan, now=lambda: NOW))
    line = job_lines(repo)[-1]

    assert line["outcome"] == FAILURE
    assert line["input_tokens"] == line["output_tokens"] == UNRECORDED
    assert line["model"] == MODEL and "cost_usd" not in line
    assert line["tier1"]["status"] == SCANNED and line["tier1"]["cost_usd"] == 0.02


@pytest.mark.parametrize("value, usable", [
    (0, True), (12, True), (0.5, True), (-1, False), (-0.01, False), (True, False),
    (float("nan"), False), (float("inf"), False), (10**400, False), ("3", False),
    (None, False)])
def test_one_rule_decides_whether_a_reported_number_is_usable(value, usable):
    assert usable_number(value) is usable


def test_a_negative_reported_cost_is_never_written(tmp_path, no_real_gh):
    repo = checkout(tmp_path, NO_BARE_PRINT, config=GATE)
    claude, _ = fake_claude(tmp_path, "fix", None, edits=PRINT_FIXES,
                            payload=claude_payloads.success("done", cost=-0.5))

    line = sweep(repo, claude=claude)

    assert line["outcome"] == SUCCESS and "cost_usd" not in line
    assert line["input_tokens"] == claude_payloads.USAGE["input_tokens"]


# --- the other outcomes ----------------------------------------------------------------

def test_a_fix_step_that_changed_nothing_is_a_failure(tmp_path, no_real_gh):
    repo = checkout(tmp_path, NO_BARE_PRINT, config=GATE)
    claude, _ = fixer(tmp_path, edits=())

    line = sweep(repo, claude=claude)

    assert line["outcome"] == FAILURE and line["message"] == NOTHING_CHANGED
    assert line["work"] == WORK and "found_delta" in line and "test_gate" not in line
    assert nothing_pushed(tmp_path, no_real_gh)


@pytest.mark.parametrize("exit_code, payload, why", [
    (3, None, "exited 3"),
    (0, claude_payloads.error_max_turns(), "reported an error (error_max_turns)"),
    # A max-turns stop whose `is_error` is not true: its subtype still says so.
    (0, {**claude_payloads.error_max_turns(), "is_error": False},
     "reported an error (error_max_turns)"),
])
def test_a_fix_step_that_did_not_finish_is_a_failure_and_is_not_reviewed(
        tmp_path, no_real_gh, exit_code, payload, why):
    """Its edits are not reviewed or published: the agent stopped part-way."""
    repo = checkout(tmp_path, NO_BARE_PRINT, config=GATE)
    claude, _ = fake_claude(tmp_path, "fix", "done", edits=PRINT_FIXES, exit_code=exit_code,
                            payload=payload)

    line = sweep(repo, claude=claude)

    assert line["outcome"] == FAILURE and why in line["message"]
    assert "test_gate" not in line and nothing_pushed(tmp_path, no_real_gh)


def test_the_default_claude_in_tests_refuses_and_is_never_a_model(tmp_path, no_real_gh,
                                                                no_real_claude):
    """tests/conftest.py's no_real_claude: SweepTools() with no `claude`
    runs sweep_job.CLAUDE, the refusing stand-in; it starts (so the call is
    recorded as unrecorded spend), prints no payload, and the sweep fails."""
    repo = checkout(tmp_path, NO_BARE_PRINT, config=GATE)
    assert SweepTools().claude == str(no_real_claude)

    with JobRun(repo_ledger(repo), "sweep") as run:
        run_sweep(repo, run, SweepTools(now=lambda: NOW))
    line = job_lines(repo)[-1]

    assert line["outcome"] == FAILURE and "exited 97" in line["message"]
    assert NO_REAL_CLAUDE in line["message"]
    assert nothing_pushed(tmp_path, no_real_gh)


def test_files_past_the_cap_are_left_for_the_next_run(tmp_path, no_real_gh):
    repo = checkout(tmp_path, NO_BARE_PRINT, config=GATE + "  max_files_per_pr: 1\n")
    claude, _ = fixer(tmp_path)

    line = sweep(repo, claude=claude)

    assert line["outcome"] == SUCCESS, line["message"]
    assert line["pr"]["paths"] == [BILLING]
    assert [fix["path"] for fix in line["fixes"]] == [BILLING]
    body = creates(no_real_gh)[0]["body_file_content"]
    assert HANDLERS in section(body, "### Files left for the next run")


def test_when_the_remedies_leave_the_agent_nothing_no_call_is_made(tmp_path, no_real_gh):
    """Only the variant-module rule, whose `remedy: delete_file` the runner
    applies itself: the agent has nothing left, so no model call, and the PR
    carries the deletion, recorded as the remedy's."""
    repo = checkout(tmp_path, NO_VARIANT_MODULE, config=GATE)
    claude, log = fixer(tmp_path)

    line = sweep(repo, claude=claude)

    assert not log.exists()
    assert line["outcome"] == SUCCESS, line["message"]
    assert line["pr"]["paths"] == [VARIANT]
    assert [(fix["path"], fix["fixed_by"]) for fix in line["fixes"]] == [(VARIANT, REMEDY)]
    assert not {"input_tokens", "output_tokens", "model", "cost_usd"} & set(line)
    assert line["tier1"]["status"] == OFF


def test_a_body_that_cannot_be_written_fails_before_any_push(tmp_path, no_real_gh,
                                                           monkeypatch):
    repo = checkout(tmp_path, NO_BARE_PRINT, config=GATE)
    claude, _ = fixer(tmp_path)

    def too_big(*_args, **_kwargs):
        raise SweepBodyError("the marker and the gate line alone do not fit")

    monkeypatch.setattr(sweep_job, "final_body", too_big)

    line = sweep(repo, claude=claude)

    assert line["outcome"] == FAILURE and "do not fit" in line["message"]
    assert "nothing was pushed" in line["message"]
    assert "pr" not in line and "fixes" not in line
    assert line["test_gate"]["strategy"] == GREEN
    assert nothing_pushed(tmp_path, no_real_gh)


def test_a_naive_clock_is_refused_before_anything_is_asked_or_spent(tmp_path, no_real_gh):
    repo = checkout(tmp_path, NO_BARE_PRINT, config=GATE)
    claude, log = fixer(tmp_path)

    line = sweep(repo, claude=claude, now=lambda: datetime(2026, 10, 27, 23, 30))

    assert line["outcome"] == REFUSED and line["reason"] == Reason.GUARD_ERROR.value
    assert "aware datetime" in line["message"]
    assert not log.exists(), "no fix call"
    assert no_real_gh.calls() == [] and nothing_pushed(tmp_path, no_real_gh)
    assert not {"input_tokens", "output_tokens", "model", "cost_usd"} & set(line)


def test_the_sweeps_day_is_the_utc_day():
    assert sweep_day(NOW) == date(2026, 10, 28)
    assert type(sweep_day(NOW)) is date
    for naive in (datetime(2026, 10, 27), date(2026, 10, 27), "2026-10-27"):
        with pytest.raises(ValueError):
            sweep_day(naive)


def test_a_branch_already_on_origin_is_a_publish_refusal(tmp_path, no_real_gh):
    repo = checkout(tmp_path, NO_BARE_PRINT, config=GATE)
    branch = f"curamoir/sweep-{DAY}-{base_of(repo)[:8]}"
    git(repo, "push", "--quiet", "origin", f"main:refs/heads/{branch}")
    claude, _ = fixer(tmp_path)

    line = sweep(repo, claude=claude)

    assert line["outcome"] == REFUSED and line["reason"] == Reason.PUBLISH.value
    assert "already exists on origin" in line["message"]
    assert "found_delta" not in line and "pr" not in line and "fixes" not in line
    assert line["tier1"]["status"] == OFF
    assert creates(no_real_gh) == []
    assert git(tmp_path / "origin.git", "rev-parse", branch) == git(repo, "rev-parse", "main")
    # publish said where the branch is (never pushed): no pending field.
    assert PUBLISH_PENDING not in line


def test_a_failing_pre_push_hook_is_a_failure_with_no_pr(tmp_path, no_real_gh):
    repo = checkout(tmp_path, NO_BARE_PRINT, config=GATE)
    hook = repo / ".git" / "hooks" / "pre-push"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text("#!/bin/sh\necho 'the hook says no' >&2\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    claude, _ = fixer(tmp_path)

    line = sweep(repo, claude=claude)

    assert line["outcome"] == FAILURE and "the hook says no" in line["message"]
    assert "pr" not in line and "fixes" not in line
    assert nothing_pushed(tmp_path, no_real_gh)


def test_a_pr_whose_outcome_is_unknown_is_a_failure_saying_it_may_exist(tmp_path,
                                                                        no_real_gh,
                                                                        monkeypatch):
    repo = checkout(tmp_path, NO_BARE_PRINT, config=GATE)
    claude, _ = fixer(tmp_path)
    no_real_gh.feed(create_fails_with="HTTP 502: Bad Gateway")

    def lookup_fails(*_args, **_kwargs):
        raise github.GhError("gh pr list exited 1: HTTP 502")

    monkeypatch.setattr(publish, "find_open_pr", lookup_fails)

    line = sweep(repo, claude=claude)

    assert line["outcome"] == FAILURE and line["message"].startswith(PR_MAY_EXIST)
    assert "pr" not in line and "fixes" not in line
    # The outcome is unknown, so the pending field stays.
    assert line[PUBLISH_PENDING]["note"].startswith(PR_MAY_EXIST)
    # The branch is left on origin on purpose (publish's Errors table).
    assert len(origin_branches(tmp_path)) == 2


def test_a_pr_opened_into_another_branch_keeps_pr_and_records_no_fixes(tmp_path, no_real_gh,
                                                                      monkeypatch):
    repo = checkout(tmp_path, NO_BARE_PRINT, config=GATE)
    claude, _ = fixer(tmp_path)
    real = publish.publish

    def into_develop(*args, **kwargs):
        return dataclasses.replace(real(*args, **kwargs), base_branch="develop")

    monkeypatch.setattr(publish, "publish", into_develop)

    line = sweep(repo, claude=claude)

    assert line["outcome"] == FAILURE and "'develop'" in line["message"]
    assert line["pr"]["number"] == 1 and "fixes" not in line
    assert read_snapshot(snapshot_path(repo)) is not None


def test_fixes_that_cannot_be_counted_are_refused_before_the_pr(tmp_path, no_real_gh,
                                                               monkeypatch):
    repo = checkout(tmp_path, NO_BARE_PRINT, config=GATE)
    claude, _ = fixer(tmp_path)

    def untrusted(*_args, **_kwargs):
        raise LintUntrusted("the re-lint exited 2")

    monkeypatch.setattr(sweep_job, "record_fixes", untrusted)

    line = sweep(repo, claude=claude)

    assert line["outcome"] == REFUSED and line["reason"] == Reason.FIX_RECORDS.value
    assert "re-lint exited 2" in line["message"]
    assert nothing_pushed(tmp_path, no_real_gh)


def test_a_lint_failure_after_the_line_is_recorded_does_not_overwrite_it(tmp_path,
                                                                         no_real_gh,
                                                                         monkeypatch):
    repo = checkout(tmp_path, NO_BARE_PRINT, config=GATE)

    def records_then_raises(self, _sandbox):
        self.run.succeeded("recorded")
        raise LintUntrusted("late")

    monkeypatch.setattr(sweep_job._Sweep, "sweep_in", records_then_raises)

    with pytest.warns(RuntimeWarning, match="late"):
        line = sweep(repo)

    assert line["outcome"] == SUCCESS and line["message"] == "recorded"


# --- through the runner ----------------------------------------------------------------

def test_run_job_sweeps_through_the_registered_body_and_the_claude_seam(tmp_path,
                                                                      no_real_gh,
                                                                      monkeypatch):
    """runner.run_job(repo, "sweep"): the guards (whose gh calls name
    origin's repository), then run_sweep with the `claude` sweep_job.CLAUDE
    names, then one PR. ST6 drives its acceptance this way."""
    repo = checkout(tmp_path, NO_BARE_PRINT, config=GATE)
    claude, log = fixer(tmp_path)
    monkeypatch.setattr(sweep_job, "CLAUDE", claude)

    ran = runner.run_job(repo, "sweep")

    assert ran.exit_code == runner.EXIT_SUCCESS, ran.sentence
    assert log.exists() and len(creates(no_real_gh)) == 1
    [line] = job_lines(repo)
    assert line["outcome"] == SUCCESS and line["pr"]["number"] == 1
    calls = no_real_gh.calls()
    labels = [argv for argv in calls if argv[:2] == ["label", "list"]]
    lists = [argv for argv in calls if argv[:2] == ["pr", "list"]]
    assert len(labels) == 1 and len(lists) == 2
    assert all(argv[argv.index("--repo") + 1] == GH_REPO for argv in labels + lists)


# --- the file cap, unfinished payloads, skipped remedies, gate and review errors --------

def test_a_cap_of_zero_is_refused_before_any_remedy_or_fix_call(tmp_path, no_real_gh):
    """`sweep.max_files_per_pr: 0`: no PR may carry a file, so no fix call is
    paid for (as tier 1 makes no call at 0) and the line is a deliberate
    refusal, never a crash."""
    repo = checkout(tmp_path, NO_BARE_PRINT, NO_VARIANT_MODULE,
                    config=GATE + "  max_files_per_pr: 0\n")
    claude, log = fixer(tmp_path)

    line = sweep(repo, claude=claude)

    assert not log.exists(), "no fix call"
    assert line["outcome"] == REFUSED and line["reason"] == Reason.FILE_CAP.value
    assert line["message"] == NO_FILES_ALLOWED
    assert line["tier1"]["status"] == OFF
    assert not {"input_tokens", "output_tokens", "model", "cost_usd"} & set(line)
    assert "found_delta" not in line and nothing_pushed(tmp_path, no_real_gh)


def test_a_payload_without_a_subtype_is_finished(tmp_path, no_real_gh):
    payload = claude_payloads.success("done")
    del payload["subtype"]
    repo = checkout(tmp_path, NO_BARE_PRINT, config=GATE)
    claude, _ = fake_claude(tmp_path, "fix", None, edits=PRINT_FIXES, payload=payload)

    line = sweep(repo, claude=claude)

    assert line["outcome"] == SUCCESS, line["message"]


def test_skipped_remedies_with_no_fix_step_say_so_and_why(tmp_path, no_real_gh):
    """Only the variant-module rule, and the variant is still imported at the
    base, so its deletion is skipped: no fix step ran, and the line says
    that and the remedy's reason, not that a fix step changed nothing."""
    repo = checkout(tmp_path, NO_VARIANT_MODULE, config=GATE)
    (repo / "src/sample/web/router.py").write_text(
        "from sample.web import handlers_v2\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "--quiet", "-m", "router")
    git(repo, "push", "--quiet", "origin", "main")
    claude, log = fixer(tmp_path)

    line = sweep(repo, claude=claude)

    assert not log.exists()
    assert line["outcome"] == FAILURE and line["message"] == NOTHING_REMEDIED
    assert line["message"] != NOTHING_CHANGED
    [skipped] = line["remedies_skipped"]
    assert skipped["path"] == VARIANT and skipped["remedy"] == "delete_file"
    assert skipped["reason"]
    assert nothing_pushed(tmp_path, no_real_gh)


def test_a_gate_error_from_the_gate_is_a_test_gate_refusal(tmp_path, no_real_gh,
                                                          monkeypatch):
    repo = checkout(tmp_path, NO_BARE_PRINT, config=GATE)
    claude, _ = fixer(tmp_path)

    def cannot_hold(*_args, **_kwargs):
        raise GateError("a fixed file cannot be read, so it cannot be held")

    monkeypatch.setattr(sweep_job, "run_test_gate", cannot_hold)

    line = sweep(repo, claude=claude)

    assert line["outcome"] == REFUSED and line["reason"] == Reason.TEST_GATE.value
    assert "cannot be held" in line["message"]
    assert nothing_pushed(tmp_path, no_real_gh)


def test_a_gate_error_from_the_patch_is_a_test_gate_refusal_keeping_the_spend(
        tmp_path, no_real_gh, monkeypatch):
    repo = checkout(tmp_path, NO_BARE_PRINT, config=GATE)
    claude, _ = fixer(tmp_path)

    def rewritten(_gate, _sandbox):
        raise GateError(f"{BILLING} changed after the green run")

    monkeypatch.setattr(GateResult, "patch", rewritten)

    line = sweep(repo, claude=claude)

    assert line["outcome"] == REFUSED and line["reason"] == Reason.TEST_GATE.value
    assert "changed after the green run" in line["message"]
    assert line["input_tokens"] == claude_payloads.USAGE["input_tokens"]
    assert line["model"] == MODEL
    assert nothing_pushed(tmp_path, no_real_gh)


def test_a_review_that_cannot_answer_is_a_sandbox_refusal_keeping_spend_and_tier1(
        tmp_path, no_real_gh, monkeypatch):
    repo = checkout(tmp_path, NO_BARE_PRINT, config=GATE)
    claude, log = fixer(tmp_path)

    def unverifiable(*_args, **_kwargs):
        raise ScopeCheckError("the principles at the base commit cannot be read")

    monkeypatch.setattr(sweep_job, "verify_fix", unverifiable)

    line = sweep(repo, claude=claude)

    assert log.exists(), "the call ran"
    assert line["outcome"] == REFUSED and line["reason"] == Reason.SANDBOX.value
    assert line["input_tokens"] == claude_payloads.USAGE["input_tokens"]
    assert line["tier1"]["status"] == OFF
    assert nothing_pushed(tmp_path, no_real_gh)


def _payload(**changes):
    payload = claude_payloads.success("done")
    payload.update(changes)
    return {key: value for key, value in payload.items() if value is not _ABSENT}


_ABSENT = object()


@pytest.mark.parametrize("payload, why", [
    # is_error must be exactly False (or absent): anything else is unfinished.
    (_payload(is_error="true"), "reported an error (is_error: 'true')"),
    (_payload(is_error=1), "reported an error (is_error: 1)"),
    (_payload(is_error=None), "reported an error (is_error: None)"),
    # True with a success subtype names is_error, not the subtype.
    (_payload(is_error=True), "reported an error (is_error: True)"),
    # The subtype is compared case-insensitively.
    (_payload(subtype="Error_during_execution"), "reported an error (Error_during_execution)"),
], ids=["is_error-str", "is_error-1", "is_error-null", "is_error-true-success",
        "subtype-mixed-case"])
def test_only_a_payload_that_is_strictly_finished_is_reviewed(tmp_path, no_real_gh,
                                                             payload, why):
    repo = checkout(tmp_path, NO_BARE_PRINT, config=GATE)
    claude, _ = fake_claude(tmp_path, "fix", None, edits=PRINT_FIXES, payload=payload)

    line = sweep(repo, claude=claude)

    assert line["outcome"] == FAILURE and why in line["message"]
    assert "(success)" not in line["message"]
    assert "test_gate" not in line and nothing_pushed(tmp_path, no_real_gh)


@pytest.mark.parametrize("payload", [_payload(is_error=_ABSENT), _payload(subtype=_ABSENT),
                                     _payload(is_error=_ABSENT, subtype=_ABSENT)],
                         ids=["no-is_error", "no-subtype", "neither"])
def test_absent_is_error_and_subtype_count_as_finished(tmp_path, no_real_gh, payload):
    repo = checkout(tmp_path, NO_BARE_PRINT, config=GATE)
    claude, _ = fake_claude(tmp_path, "fix", None, edits=PRINT_FIXES, payload=payload)

    line = sweep(repo, claude=claude)

    assert line["outcome"] == SUCCESS, line["message"]
    assert PUBLISH_PENDING not in line


def test_a_run_that_ends_inside_publish_says_the_branch_or_pr_may_exist(
        tmp_path, no_real_gh, monkeypatch):
    """SIGTERM after `gh pr create` opened the PR, before publish returned:
    the Terminated is not swallowed, and the crash line carries
    PUBLISH_PENDING naming the branch and that it or its PR may exist."""
    repo = checkout(tmp_path, NO_BARE_PRINT, config=GATE)
    claude, _ = fixer(tmp_path)
    real = publish.create_pr

    def created_then_terminated(*args, **kwargs):
        real(*args, **kwargs)
        raise Terminated(signal.SIGTERM)

    monkeypatch.setattr(publish, "create_pr", created_then_terminated)

    with pytest.raises(Terminated), JobRun(repo_ledger(repo), "sweep") as run:
        run_sweep(repo, run, SweepTools(claude=claude, now=lambda: NOW))
    line = job_lines(repo)[-1]

    assert len(creates(no_real_gh)) == 1
    assert line["outcome"] == FAILURE and "Terminated" in line["message"]
    pending = line[PUBLISH_PENDING]
    assert pending["branch"] == f"curamoir/sweep-{DAY}-{base_of(repo)[:8]}"
    assert pending["gh_repo"] == GH_REPO and pending["note"].startswith(PR_MAY_EXIST)
    assert "pr" not in line and "fixes" not in line


def test_publish_pending_is_dropped_once_publish_says_where_the_branch_is(tmp_path,
                                                                        no_real_gh):
    repo = checkout(tmp_path, NO_BARE_PRINT, config=GATE)
    claude, _ = fixer(tmp_path)
    no_real_gh.feed(fail_on={"pr create": "HTTP 422: Validation Failed"})

    line = sweep(repo, claude=claude)

    assert line["outcome"] == FAILURE and PUBLISH_PENDING not in line


# --- provenance: what only these tests pin -----------------------------------------------

def test_an_agent_that_writes_into_the_sandboxs_git_is_refused(tmp_path, no_real_gh):
    """The review holds the sandbox to the snapshot run_fix_step took BEFORE
    the agent ran (fix_run.git_state): a `core.hooksPath` the agent planted
    in `.git/config` is out of scope, and nothing is pushed. A snapshot
    taken after the agent would see nothing changed and publish."""
    repo = checkout(tmp_path, NO_BARE_PRINT, config=GATE)
    claude, _ = fixer(tmp_path, edits=[*PRINT_FIXES, (".git/config", "[core]",
                                                      "[core]\n\thooksPath = planted")])

    line = sweep(repo, claude=claude)

    assert line["outcome"] == REFUSED, line["message"]
    assert line["reason"] in (Reason.SCOPE.value, Reason.SANDBOX.value)
    assert "git_internals_changed" in line["message"]
    assert nothing_pushed(tmp_path, no_real_gh)


#: A secret-shaped token the scan puts in a tier-1 note (the note is model
#: text the repository steers).
SECRET = "token=Zq8vK2mX9pL4wR7t"


def tier1_sweep(tmp_path, note=FOUND_BILLING["note"]):
    """A sweep with tier 1 on: the scan reports the billing plant of
    errors-carry-context with `note`, and the fix agent fixes it and the
    two prints, replying `t1-01 fixed`. Its (repo, line)."""
    repo = checkout(tmp_path, NO_BARE_PRINT, config=GATE.replace("  tier1_scan: false\n", ""))
    scan, _ = fake_claude(tmp_path, "scan", findings_reply({**FOUND_BILLING, "note": note}))
    fix, _ = fixer(tmp_path, edits=[EDITS[0], EDITS[1], EDITS[2]], reply="t1-01 fixed")
    return repo, sweep(repo, claude=dispatching_claude(tmp_path, scan, fix))


def test_a_tier1_fix_is_recorded_and_listed_through_run_sweep(tmp_path, no_real_gh):
    _repo, line = tier1_sweep(tmp_path)

    assert line["outcome"] == SUCCESS, line["message"]
    assert line["tier1"]["status"] == SCANNED and line["tier1"]["findings"] == 1
    [tier1_fix] = [fix for fix in line["fixes"] if fix["source"] == "tier1"]
    assert (tier1_fix["principle_id"], tier1_fix["path"], tier1_fix["span"]) == (
        ECC, BILLING, [37, 37])
    assert line["fix_summary"][ECC] == 1
    fixes = section(creates(no_real_gh)[0]["body_file_content"], "### Fixes")
    assert f"`{ECC}` (tier 1: no rule yet)" in fixes


def test_the_body_sent_is_the_redacted_final_body(tmp_path, no_real_gh):
    """A secret in the tier-1 note reaches gh redacted: the body is
    final_body's, never render_pr_body's or raw text."""
    _repo, line = tier1_sweep(tmp_path, note=f"leaks {SECRET} into the log")

    assert line["outcome"] == SUCCESS, line["message"]
    body = creates(no_real_gh)[0]["body_file_content"]
    assert "Zq8vK2mX9pL4wR7t" not in body
    assert "REDACTED" in body


def test_the_stand_in_claude_is_first_on_path_outside_tests_plugin(no_real_claude):
    assert shutil.which("claude") == str(no_real_claude)
    child = subprocess.run(["claude", "-p", "hello"], capture_output=True, text=True,
                           check=False)
    assert child.returncode == 97
    assert NO_REAL_CLAUDE in child.stderr and child.stdout == ""


def test_records_of_another_base_are_a_fix_records_refusal(tmp_path, no_real_gh,
                                                           monkeypatch):
    repo = checkout(tmp_path, NO_BARE_PRINT, config=GATE)
    claude, _ = fixer(tmp_path)

    def another_base(*_args, **_kwargs):
        raise ValueError("2 fix record(s) are of another base")

    monkeypatch.setattr(sweep_job, "fix_ledger_fields", another_base)

    line = sweep(repo, claude=claude)

    assert line["outcome"] == REFUSED and line["reason"] == Reason.FIX_RECORDS.value
    assert "another base" in line["message"]
    assert nothing_pushed(tmp_path, no_real_gh)


@pytest.mark.parametrize("bug", [RuntimeError, ValueError, TypeError])
def test_a_bug_while_building_the_records_is_a_crash_not_a_refusal(tmp_path, no_real_gh,
                                                                  monkeypatch, bug):
    """Only GitGuardError and LintUntrusted from building the records are
    refusals; anything else, a ValueError (FixRecord's own check) included,
    is a bug and crashes as one."""
    repo = checkout(tmp_path, NO_BARE_PRINT, config=GATE)
    claude, _ = fixer(tmp_path)

    def broken(*_args, **_kwargs):
        raise bug("a FixRecord field is None")

    monkeypatch.setattr(sweep_job, "record_fixes", broken)

    line = sweep(repo, crashes=bug, claude=claude)

    assert line["outcome"] == FAILURE and line["reason"] is None
    assert line["message"] == f"{bug.__name__}: a FixRecord field is None"
    assert nothing_pushed(tmp_path, no_real_gh)


def test_a_git_error_escaping_publish_is_a_failure_saying_the_branch_is_unknown(
        tmp_path, no_real_gh, monkeypatch):
    repo = checkout(tmp_path, NO_BARE_PRINT, config=GATE)
    claude, _ = fixer(tmp_path)

    def git_failed(*_args, **_kwargs):
        raise GitGuardError("git cat-file did not finish within 60 seconds")

    monkeypatch.setattr(publish, "publish", git_failed)

    line = sweep(repo, claude=claude)

    assert line["outcome"] == FAILURE
    assert "reached origin is unknown" in line["message"]
    assert line["test_gate"]["strategy"] == GREEN and "pr" not in line
    assert line[PUBLISH_PENDING]["branch"].startswith("curamoir/sweep-")


def test_the_new_refusal_reasons_are_the_ledgers_strings():
    """The ledger, the brief and every reader compare these strings."""
    assert {reason.name: reason.value for reason in (
        Reason.SCOPE, Reason.TEST_GATE, Reason.FIX_RECORDS, Reason.FILE_CAP,
        Reason.PUBLISH)} == {"SCOPE": "scope", "TEST_GATE": "test_gate",
                             "FIX_RECORDS": "fix_records", "FILE_CAP": "file_cap",
                             "PUBLISH": "publish"}


def test_a_file_the_agent_added_out_of_scope_is_removed_and_the_rest_opens_a_pr(
        tmp_path, no_real_gh):
    """cm-9ab: a dropped path the base does not have is removed, not
    restored."""
    repo = checkout(tmp_path, NO_BARE_PRINT, config=GATE)
    claude, _ = fixer(tmp_path, writes={"scratch_notes.py": "x = 1\n"})

    line = sweep(repo, claude=claude)

    assert line["outcome"] == SUCCESS, line["message"]
    assert "scratch_notes.py" not in line["pr"]["paths"]
    assert [(b["path"], b["kind"]) for b in line["scope_dropped"]] == [
        ("scratch_notes.py", "file_added")]
