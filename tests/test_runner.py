"""E10-R1a (cm-x5a.8): `curamoir run <job>`, one entry point, one fixed order.

These drive the runner the way a scheduler does, through `cli.main(["run",
...])` or `runner.run_job`, over the harness's real git repo initialised by
`scaffold.init_repo`, and read the job line back from the repo's own ledger,
where JobRun wrote it. The open-PR listing is the gh shape test_job_guard
feeds (`listing(gh_item(...))`), and spend is seeded as JobRun's job lines.

The order is proven by OBSERVATION: each step is wrapped, at runtime, in a
spy that appends its name to a list, so a reordering in the code changes the
list whatever the code looks like.
"""

import dataclasses
import io
import os
import subprocess
import sys

import pytest

from curamoir import cli, github, job_guard, runner
from curamoir.budget import Budget
from curamoir.guards import JobContext, KillSwitch, OpenPrCap
from curamoir.ledger import Ledger
from curamoir.runner import (
    EXIT_FAILURE,
    EXIT_NOT_A_JOB,
    EXIT_REFUSED,
    EXIT_SUCCESS,
    EXIT_UNUSABLE_ROOT,
    JOBS,
    NOT_YET_BUILT,
    run_job,
)
from curamoir.scaffold import init_repo
from tests.fake_gh import FakeGh
from tests.test_job_guard import CEILING, gh_item, listing, october, seed_spend

#: Three open sweep PRs: at the default cap of three.
AT_THE_CAP = listing(
    gh_item(41, "curamoir/sweep-2026-09-14"),
    gh_item(44, "curamoir/sweep-2026-09-21"),
    gh_item(47, "curamoir/sweep-2026-09-28"),
)


@pytest.fixture
def repo(harness):
    """The harness repo, initialised as `/curamoir:init` leaves it."""
    init_repo(harness.repo)
    return harness


def job_lines(harness):
    return [entry for entry in harness.ledger().entries() if entry["kind"] == "job"]


def guard_lines(harness):
    return [entry for entry in harness.ledger().entries() if entry["kind"] == "guard"]


def hold_a_live_lock(harness):
    """Another run's lock, taken by the runner's own writer (LockSlot) for a
    `review` job in this process: live, and never released by the run
    under test, which did not write it."""
    held = job_guard.LockSlot()
    assert held(JobContext(job="review", repo=harness.repo)) is None
    return held


def observe_steps(monkeypatch, job):
    """Wrap every E10-R1 step in a spy that appends its name to the returned
    list when it runs, delegating to the real step."""
    steps = []

    def spy_method(cls, name, label):
        original = getattr(cls, name)

        def spy(self, *args, **kwargs):
            steps.append(label)
            return original(self, *args, **kwargs)

        monkeypatch.setattr(cls, name, spy)

    spy_method(KillSwitch, "__call__", "kill switch")
    spy_method(job_guard.LockSlot, "__call__", "lock")
    spy_method(Budget, "__call__", "budget")
    spy_method(OpenPrCap, "__call__", "open-PR cap")
    spy_method(job_guard.LockSlot, "release", "lock release")

    original_record = Ledger.record

    def record(self, *args, **kwargs):
        steps.append(f"ledger {kwargs.get('kind', 'job')}")
        return original_record(self, *args, **kwargs)

    monkeypatch.setattr(Ledger, "record", record)

    original_sync = job_guard.skip_sync

    def sync(run):
        steps.append("sync")
        return original_sync(run)

    monkeypatch.setattr(job_guard, "skip_sync", sync)

    registered = JOBS[job]

    def body(repo, run):
        steps.append("job")
        return registered.body(repo, run)

    monkeypatch.setitem(JOBS, job, dataclasses.replace(registered, body=body))
    return steps


# One registry, one guarded path, one line.

def test_brief_and_sweep_are_registered_and_the_unbuilt_jobs_are_not():
    assert set(JOBS) == {"brief", "sweep"}
    assert set(NOT_YET_BUILT) == {"review", "smoke", "sync", "grade"}


@pytest.mark.parametrize("job", ["brief", "sweep"])
def test_each_job_goes_through_run_guarded_once_and_writes_exactly_one_job_line(
        repo, monkeypatch, job):
    calls = []
    real = runner.run_guarded

    def spy(repo_root, name, *args, **kwargs):
        calls.append(name)
        return real(repo_root, name, *args, **kwargs)

    monkeypatch.setattr(runner, "run_guarded", spy)

    cli.main(["run", job, "--repo", str(repo.repo)])

    assert calls == [job]
    assert [line["job"] for line in job_lines(repo)] == [job]


def test_run_brief_writes_the_brief_and_says_so_in_one_sentence(repo, capsys):
    code = cli.main(["run", "brief", "--repo", str(repo.repo)])

    assert code == EXIT_SUCCESS
    assert (repo.repo / ".curamoir" / "briefs" / "latest.md").is_file()
    line, = job_lines(repo)
    assert (line["outcome"], line["reason"]) == ("success", None)
    assert line["message"] == "wrote .curamoir/briefs/latest.md"
    out = capsys.readouterr().out
    assert out == "cúramóir ran 'brief': success. wrote .curamoir/briefs/latest.md.\n"


def test_run_sweep_fails_loudly_as_not_built_and_never_reports_success(repo, capsys):
    code = cli.main(["run", "sweep", "--repo", str(repo.repo)])

    assert code == EXIT_FAILURE
    line, = job_lines(repo)
    assert line["outcome"] == "failure"
    assert "cm-fkd.4" in line["message"]
    out = capsys.readouterr().out
    assert "failure" in out and "cm-fkd.4" in out
    assert "success" not in out


# E10-R1's order, observed.

def test_brief_runs_the_steps_in_e10_r1_order(repo, monkeypatch):
    steps = observe_steps(monkeypatch, "brief")

    assert run_job(repo.repo, "brief", now=october).exit_code == EXIT_SUCCESS

    assert steps == ["kill switch", "lock", "budget", "sync", "job",
                     "ledger job", "lock release"]


def test_sweep_runs_the_steps_in_e10_r1_order_with_the_cap_after_the_budget(
        repo, monkeypatch):
    """The placeholder body fails, and the line and the release still follow."""
    steps = observe_steps(monkeypatch, "sweep")

    ran = run_job(repo.repo, "sweep", open_pr_listing=listing(), now=october)

    assert ran.exit_code == EXIT_FAILURE
    assert steps == ["kill switch", "lock", "budget", "open-PR cap", "sync", "job",
                     "ledger job", "lock release"]


def test_a_pause_file_refuses_before_the_lock_slot_and_the_budget(repo, monkeypatch):
    """Paused, locked and over the ceiling at once: refused as paused, with
    neither the lock slot nor the budget consulted, and the release slot
    still reached after the line."""
    repo.write(".curamoir/PAUSE", "")
    hold_a_live_lock(repo)
    seed_spend(repo.ledger(), 0.95 * CEILING)
    steps = observe_steps(monkeypatch, "brief")

    ran = run_job(repo.repo, "brief", now=october)

    assert ran.exit_code == EXIT_REFUSED
    assert steps == ["kill switch", "ledger guard", "ledger job", "lock release"]
    line = job_lines(repo)[-1]
    assert (line["outcome"], line["reason"]) == ("refused", "paused")
    # The release slot ran and left the other run's lock where it was.
    assert '"job": "review"' in (repo.repo / ".curamoir" / "state" / "lock").read_text(
        encoding="utf-8")


def test_a_body_that_raises_still_writes_its_line_and_reaches_the_release(
        repo, monkeypatch):
    def broken(_repo, _run):
        raise RuntimeError("disk on fire")

    monkeypatch.setitem(JOBS, "brief", dataclasses.replace(JOBS["brief"], body=broken))
    steps = observe_steps(monkeypatch, "brief")

    ran = run_job(repo.repo, "brief", now=october)

    assert ran.exit_code == EXIT_FAILURE
    assert steps[-3:] == ["job", "ledger job", "lock release"]
    line = job_lines(repo)[-1]
    assert (line["outcome"], line["message"]) == ("failure", "RuntimeError: disk on fire")
    assert ran.sentence == "cúramóir did not complete 'brief': RuntimeError: disk on fire."


# The open-PR cap is the sweep's, not the brief's.

def test_the_brief_is_not_refused_by_the_open_pr_cap(repo, monkeypatch):
    steps = observe_steps(monkeypatch, "brief")

    ran = run_job(repo.repo, "brief", open_pr_listing=AT_THE_CAP, now=october)

    assert ran.exit_code == EXIT_SUCCESS
    assert "open-PR cap" not in steps
    assert job_lines(repo)[-1]["outcome"] == "success"
    assert guard_lines(repo) == []


def test_the_sweep_is_refused_by_the_open_pr_cap(repo):
    ran = run_job(repo.repo, "sweep", open_pr_listing=AT_THE_CAP, now=october)

    assert ran.exit_code == EXIT_REFUSED
    line = job_lines(repo)[-1]
    assert (line["outcome"], line["reason"]) == ("refused", "pr_cap")
    assert "curamoir/sweep-2026-09-28" in line["message"]
    assert ran.sentence.startswith("cúramóir ran 'sweep': refused (pr_cap). 3 open curamoir/*")


def test_the_sweep_without_a_listing_says_when_gh_could_not_list(repo, tmp_path, monkeypatch):
    """With no listing passed, the cap counts with gh's (cm-fkd.10); a gh that
    cannot list is a degraded pr_cap line, never a silent pass. (The labels
    come from tests: this is about the listing.)"""
    gh = FakeGh(tmp_path / "gh")
    gh.feed(fail="gh: To get started with GitHub CLI, please run:  gh auth login")
    monkeypatch.setattr(github, "GH", str(gh.path))

    run_job(repo.repo, "sweep", repo_labels=lambda: [], now=october)

    lines = guard_lines(repo)
    assert [(e["reason"], e["severity"]) for e in lines] == [("pr_cap", "degraded")]
    assert "gh auth login" in lines[0]["message"]


# The sync seam.

@pytest.mark.parametrize("job", ["brief", "sweep"])
def test_the_sync_skip_is_visible_on_the_job_line(repo, job):
    run_job(repo.repo, job, open_pr_listing=listing(), now=october)

    line = job_lines(repo)[-1]
    assert line["sync"] == "skipped"
    assert "cm-eqp.3" in line["sync_reason"]


def test_a_run_refused_before_the_sync_step_does_not_claim_a_sync(repo):
    repo.write(".curamoir/PAUSE", "")

    run_job(repo.repo, "brief", now=october)

    assert "sync" not in job_lines(repo)[-1]


# Jobs that do not exist yet.

@pytest.mark.parametrize(("job", "bead"), [
    ("review", "cm-8ap"), ("smoke", "cm-5f3.1"), ("sync", "cm-eqp.3"), ("grade", "cm-16x.1"),
])
def test_a_job_not_built_yet_exits_non_zero_naming_its_bead_and_writes_no_line(
        repo, capsys, job, bead):
    code = cli.main(["run", job, "--repo", str(repo.repo)])

    assert code == EXIT_NOT_A_JOB
    out = capsys.readouterr().out
    assert bead in out
    assert "success" not in out
    assert repo.ledger().entries() == []


def test_an_unknown_job_exits_non_zero_listing_the_jobs_and_writes_no_line(repo, capsys):
    code = cli.main(["run", "deploy", "--repo", str(repo.repo)])

    assert code == EXIT_NOT_A_JOB
    out = capsys.readouterr().out
    assert "'deploy'" in out and "brief, sweep" in out
    assert repo.ledger().entries() == []


# Exit codes.

def test_the_four_exit_codes_are_distinct_and_only_success_is_zero():
    codes = [EXIT_SUCCESS, EXIT_REFUSED, EXIT_FAILURE, EXIT_UNUSABLE_ROOT]
    assert len(set(codes)) == 4
    assert EXIT_SUCCESS == 0 and 0 not in codes[1:]
    assert EXIT_NOT_A_JOB not in codes


def test_a_repo_root_that_is_not_a_directory_exits_unusable_root(tmp_path, capsys):
    missing = tmp_path / "moved-away"

    code = cli.main(["run", "brief", "--repo", str(missing)])

    assert code == EXIT_UNUSABLE_ROOT
    assert not missing.exists()
    captured = capsys.readouterr()
    assert "moved-away is not a directory" in captured.err
    assert "unusable root" in captured.out


def test_an_uninitialised_repo_exits_unusable_root_and_creates_nothing(harness, capsys):
    """A job line would mkdir `.curamoir/`, after which `init` refuses for
    good (cli._uninitialised's docstring), so nothing is written."""
    code = cli.main(["run", "brief", "--repo", str(harness.repo)])

    assert code == EXIT_UNUSABLE_ROOT
    assert not (harness.repo / ".curamoir").exists()
    assert "/curamoir:init" in capsys.readouterr().out


def test_a_body_that_reports_nothing_exits_as_a_failure(repo, monkeypatch):
    monkeypatch.setitem(JOBS, "brief", dataclasses.replace(
        JOBS["brief"], body=lambda _repo, _run: None))

    ran = run_job(repo.repo, "brief", now=october)

    assert ran.exit_code == EXIT_FAILURE
    assert job_lines(repo)[-1]["outcome"] == "unknown"
    assert ran.sentence.startswith("cúramóir ran 'brief': unknown. job finished without")


def test_a_success_with_no_message_is_one_clean_sentence(repo, monkeypatch):
    monkeypatch.setitem(JOBS, "brief", dataclasses.replace(
        JOBS["brief"], body=lambda _repo, run: run.succeeded("")))

    ran = run_job(repo.repo, "brief", now=october)

    assert ran.sentence == "cúramóir ran 'brief': success."


def test_a_job_cannot_be_registered_without_saying_whether_it_opens_prs():
    """No default, so a PR-opening job cannot skip the cap by omission."""
    with pytest.raises(TypeError, match="opens_prs"):
        runner.Job(body=lambda _repo, run: run.succeeded("done"))


def test_a_subdirectory_of_a_repo_is_told_to_point_at_the_root(repo):
    """Not "run /curamoir:init": the repository above it is initialised."""
    subdirectory = repo.repo / "src"
    subdirectory.mkdir()

    ran = run_job(subdirectory, "brief", now=october)

    assert ran.exit_code == EXIT_UNUSABLE_ROOT
    assert "no .curamoir/ directory" in ran.sentence
    assert "Point --repo at the repository root" in ran.sentence
    assert not (subdirectory / ".curamoir").exists()


def test_an_ascii_stdout_still_gets_the_refused_exit_code_and_no_traceback(repo):
    """A cron job's locale is often C/ASCII, and every sentence has an "ú"."""
    repo.write(".curamoir/PAUSE", "")
    program = ("import sys; from curamoir import cli; "
               "sys.exit(cli.main(['run', 'brief', '--repo', sys.argv[1]]))")

    result = subprocess.run([sys.executable, "-c", program, str(repo.repo)],
                            capture_output=True, check=False,
                            env={**os.environ, "PYTHONIOENCODING": "ascii"})

    assert result.returncode == EXIT_REFUSED
    assert b"Traceback" not in result.stderr
    assert result.stdout.decode("utf-8").startswith(
        "cúramóir ran 'brief': refused (paused).")


def test_a_body_that_calls_sys_exit_0_exits_as_a_failure_matching_its_line(
        repo, monkeypatch):
    def exits(_repo, _run):
        sys.exit(0)

    monkeypatch.setitem(JOBS, "brief", dataclasses.replace(JOBS["brief"], body=exits))
    steps = observe_steps(monkeypatch, "brief")

    ran = run_job(repo.repo, "brief", now=october)

    assert ran.exit_code == EXIT_FAILURE
    assert ran.sentence == "cúramóir did not complete 'brief': SystemExit: 0."
    assert [line["outcome"] for line in job_lines(repo)] == ["failure"]
    assert steps[-2:] == ["ledger job", "lock release"]


def test_a_keyboard_interrupt_is_recorded_released_and_propagated(repo, monkeypatch):
    def interrupted(_repo, _run):
        raise KeyboardInterrupt

    monkeypatch.setitem(JOBS, "brief", dataclasses.replace(JOBS["brief"], body=interrupted))
    steps = observe_steps(monkeypatch, "brief")

    with pytest.raises(KeyboardInterrupt):
        run_job(repo.repo, "brief", now=october)

    assert [line["outcome"] for line in job_lines(repo)] == ["failure"]
    assert steps[-2:] == ["ledger job", "lock release"]


def test_an_invalid_config_refuses_the_brief_without_mentioning_a_cap_it_is_exempt_from(
        repo):
    repo.write(".curamoir/curamoir.yaml", "version: 1\nbudget: [unclosed\n")

    ran = run_job(repo.repo, "brief", now=october)

    assert ran.exit_code == EXIT_REFUSED
    line = job_lines(repo)[-1]
    assert (line["outcome"], line["reason"]) == ("refused", "guard_error")
    assert "The budget cannot be configured" in line["message"]
    assert "open-PR cap" not in line["message"]
    assert "sync" not in line and "sync_reason" not in line


def test_an_invalid_config_refuses_the_sweep_naming_the_budget_and_the_cap(repo):
    repo.write(".curamoir/curamoir.yaml", "version: 1\nbudget: [unclosed\n")

    ran = run_job(repo.repo, "sweep", open_pr_listing=listing(), now=october)

    assert ran.exit_code == EXIT_REFUSED
    assert "The budget and the open-PR cap cannot" in job_lines(repo)[-1]["message"]


def test_a_closed_stdout_still_gets_the_refused_exit_code(repo, monkeypatch):
    """`>&-` leaves sys.stdout None: the sentence goes nowhere, as print()'s
    would, and the scheduler still reads the outcome from the exit code."""
    repo.write(".curamoir/PAUSE", "")
    monkeypatch.setattr(sys, "stdout", None)

    assert cli.main(["run", "brief", "--repo", str(repo.repo)]) == EXIT_REFUSED


def test_the_sentence_lands_after_text_already_printed_to_stdout(monkeypatch):
    """_write_utf8 writes bytes beneath stdout's text layer, so it flushes
    that layer first: otherwise text printed earlier, still buffered, would
    come out after the sentence. A block-buffered stdout over a pipe (a
    cron job's) holds text back; pytest's capture (capfd included) does
    not, so this builds that stdout rather than capturing one."""
    raw = io.BytesIO()
    stdout = io.TextIOWrapper(raw, encoding="utf-8", line_buffering=False,
                              write_through=False)
    monkeypatch.setattr(sys, "stdout", stdout)

    sys.stdout.write("printed first\n")
    cli._write_utf8("written second\n")

    assert raw.getvalue() == b"printed first\nwritten second\n"
