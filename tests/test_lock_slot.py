"""E0-R2's "maintain" half (cm-x5a.6): the runner acquires the supervisor
lock at `.curamoir/state/lock` and releases it on every exit path.

`guards/lock.py`'s SupervisorLock is the READER and tests/guards/test_lock.py
proves it. These prove the WRITER, `job_guard.LockSlot`, through the paths
a scheduler takes: `runner.run_job` and `job_guard.run_guarded` in-process,
and `curamoir run` as a child process where a signal or a kill has to be
real. Every lock here is the one LockSlot wrote, never a hand-written one,
so the shape the reader is checked against is the writer's.
"""

import dataclasses
import errno
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from curamoir import job_guard
from curamoir.guards import JobContext, Reason, SupervisorLock
from curamoir.guards.lock import forget_lock, lock_bytes, lock_file
from curamoir.job_guard import LockSlot, run_guarded
from curamoir.runner import EXIT_FAILURE, EXIT_REFUSED, EXIT_SUCCESS, JOBS, run_job
from curamoir.scaffold import init_repo
from curamoir.termination import TERMINATING_SIGNALS, Terminated, TerminationSignals
from tests.test_job_guard import CEILING, listing, october, seed_spend
from tests.test_runner import AT_THE_CAP, job_lines

#: Long enough for a loaded CI machine to start a Python child, short
#: enough that a hang fails the test rather than the suite.
CHILD_SECONDS = 30


@pytest.fixture
def repo(harness):
    """The harness repo, initialised as `/curamoir:init` leaves it."""
    init_repo(harness.repo)
    return harness


def the_lock(harness):
    return harness.repo / ".curamoir" / "state" / "lock"


def with_body(monkeypatch, job, body):
    monkeypatch.setitem(JOBS, job, dataclasses.replace(JOBS[job], body=body))


# What the lock says while the job runs.

def test_the_lock_names_this_process_host_job_and_start_while_the_body_runs(repo, monkeypatch):
    seen = {}

    def body(_repo, run):
        seen["lock"] = json.loads(the_lock(repo).read_text(encoding="utf-8"))
        seen["judged"] = SupervisorLock()(JobContext(job="sweep", repo=repo.repo))
        run.succeeded("looked at the lock")

    with_body(monkeypatch, "brief", body)
    before = datetime.now(timezone.utc)

    ran = run_job(repo.repo, "brief", now=october)

    after = datetime.now(timezone.utc)
    assert ran.exit_code == EXIT_SUCCESS
    lock = seen["lock"]
    assert lock["pid"] == os.getpid()
    assert lock["host"] == socket.gethostname() == SupervisorLock.this_host()
    assert lock["job"] == "brief"
    # The wall clock, not the run's injected one: the reader ages a lock by
    # the wall clock, and `october` would make a live lock look stale.
    started = datetime.fromisoformat(lock["started"])
    assert started.tzinfo is not None
    assert before - timedelta(seconds=1) <= started <= after
    # The reader calls the writer's lock LIVE, held by this job.
    assert seen["judged"].reason is Reason.LOCKED
    assert "another cúramóir job holds the lock: job 'brief'" in seen["judged"].message
    assert "STALE" not in seen["judged"].message


def test_the_state_directory_is_created_when_it_does_not_exist(harness):
    """scaffold makes `state/`, but nothing guarantees it is still there."""
    state = harness.repo / ".curamoir" / "state"
    assert not state.exists()
    slot = LockSlot()

    assert slot(JobContext(job="brief", repo=harness.repo)) is None

    assert json.loads(the_lock(harness).read_text(encoding="utf-8"))["job"] == "brief"
    slot.release()
    assert not the_lock(harness).exists()


def test_the_lock_is_held_through_the_budget_and_cap_checks(repo, monkeypatch):
    """Acquired BEFORE the budget and the cap (cm-fkd.7's check-then-act),
    and the run's own lock does not refuse it."""
    held_during = []
    for cls in (job_guard.Budget, job_guard.OpenPrCap):
        original = cls.__call__

        def spy(self, context, _original=original, _name=cls.__name__):
            held_during.append((_name, the_lock(repo).exists()))
            return _original(self, context)

        monkeypatch.setattr(cls, "__call__", spy)

    ran = run_job(repo.repo, "sweep", open_pr_listing=listing(), now=october)

    # The body's own refusal (the harness repo has no origin, so no base),
    # after every guard passed: not a guard's.
    assert ran.exit_code == EXIT_REFUSED
    assert repo.ledger().entries()[-1]["reason"] == "sweep_base"
    assert held_during == [("Budget", True), ("OpenPrCap", True)]
    assert not the_lock(repo).exists()


# Released on every exit path.

def test_released_after_success(repo):
    assert run_job(repo.repo, "brief", now=october).exit_code == EXIT_SUCCESS
    assert not the_lock(repo).exists()


def test_released_after_the_budget_refuses(repo):
    seed_spend(repo.ledger(), 0.95 * CEILING)

    ran = run_job(repo.repo, "brief", now=october)

    assert ran.exit_code == EXIT_REFUSED
    assert job_lines(repo)[-1]["reason"] == "budget"
    assert not the_lock(repo).exists()


def test_released_after_the_open_pr_cap_refuses(repo):
    ran = run_job(repo.repo, "sweep", open_pr_listing=AT_THE_CAP, now=october)

    assert ran.exit_code == EXIT_REFUSED
    assert job_lines(repo)[-1]["reason"] == "pr_cap"
    assert not the_lock(repo).exists()


def test_released_after_an_invalid_config_refuses(repo):
    repo.write(".curamoir/curamoir.yaml", "version: 1\nbudget: [unclosed\n")

    ran = run_job(repo.repo, "brief", now=october)

    assert job_lines(repo)[-1]["reason"] == "guard_error"
    assert ran.exit_code == EXIT_REFUSED
    assert not the_lock(repo).exists()


@pytest.mark.parametrize("crash", [RuntimeError("disk on fire"), SystemExit(0)],
                         ids=["exception", "system-exit"])
def test_released_after_the_body_crashes(repo, monkeypatch, crash):
    def body(_repo, _run):
        assert the_lock(repo).exists()
        raise crash

    with_body(monkeypatch, "brief", body)

    ran = run_job(repo.repo, "brief", now=october)

    assert ran.exit_code == EXIT_FAILURE
    assert job_lines(repo)[-1]["outcome"] == "failure"
    assert not the_lock(repo).exists()


def test_released_after_a_keyboard_interrupt(repo, monkeypatch):
    def body(_repo, _run):
        assert the_lock(repo).exists()
        raise KeyboardInterrupt

    with_body(monkeypatch, "brief", body)

    with pytest.raises(KeyboardInterrupt):
        run_job(repo.repo, "brief", now=october)

    assert not the_lock(repo).exists()


#: A `curamoir run brief` whose body says it has started (the file named by
#: argv[2]) and then waits to be signalled or killed. sys.stdout.write, not
#: print: the program is source for a child process, not test output.
WAITING_RUN = f"""
import dataclasses, sys, time
from pathlib import Path
from curamoir import cli, runner

def body(_repo, _run):
    Path(sys.argv[2]).write_text("started", encoding="utf-8")
    time.sleep({CHILD_SECONDS})

runner.JOBS["brief"] = dataclasses.replace(runner.JOBS["brief"], body=body)
sys.exit(cli.main(["run", "brief", "--repo", sys.argv[1]]))
"""


def start_waiting_run(harness, tmp_path):
    started = tmp_path / "started"
    child = subprocess.Popen([sys.executable, "-c", WAITING_RUN, str(harness.repo), str(started)],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.monotonic() + CHILD_SECONDS
    while not started.exists():
        if child.poll() is not None or time.monotonic() > deadline:
            child.kill()
            _, err = child.communicate()
            pytest.fail(f"the child run never started its body: {err.decode()}")
        time.sleep(0.02)
    return child


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP],
                         ids=["SIGTERM", "SIGHUP"])
def test_released_when_the_scheduler_signals_the_run(repo, tmp_path, signum):
    """systemd, CI cancellation and `kill` send SIGTERM; a closed terminal
    sends SIGHUP. Python's default for both ends the process without running
    a single `finally`, so the run converts them, releases, records, and
    then dies by the same signal so the scheduler still sees what happened."""
    child = start_waiting_run(repo, tmp_path)
    held = json.loads(the_lock(repo).read_text(encoding="utf-8"))
    assert (held["pid"], held["job"]) == (child.pid, "brief")

    child.send_signal(signum)
    _, err = child.communicate(timeout=CHILD_SECONDS)

    assert child.returncode == -signum, err.decode()
    assert not the_lock(repo).exists()
    line = job_lines(repo)[-1]
    assert line["outcome"] == "failure"
    assert signal.Signals(signum).name in line["message"]


def test_a_killed_run_leaves_a_lock_the_next_run_reports_as_stale(repo, tmp_path):
    """SIGKILL runs no cleanup at all: E0-R2's liveness check is the
    backstop, and it can judge the writer's lock because the writer records
    the pid and host it checks. Reported, not overridden."""
    child = start_waiting_run(repo, tmp_path)
    child.kill()
    child.communicate(timeout=CHILD_SECONDS)
    assert the_lock(repo).exists()

    ran = run_job(repo.repo, "brief", now=october)

    assert ran.exit_code == EXIT_REFUSED
    line = job_lines(repo)[-1]
    assert (line["outcome"], line["reason"]) == ("refused", "locked")
    assert f"pid {child.pid} is not running on this host" in line["message"]
    assert "STALE" in line["message"]
    # Still there: reporting a stale lock is not clearing it.
    assert json.loads(the_lock(repo).read_text(encoding="utf-8"))["pid"] == child.pid


# One run at a time.

def test_a_second_run_while_the_first_holds_the_lock_refuses_locked(repo, monkeypatch):
    seen = {}

    def body(_repo, run):
        seen["second"] = run_job(repo.repo, "brief", now=october)
        run.succeeded("ran a second job inside the first")

    with_body(monkeypatch, "sweep", body)

    run_job(repo.repo, "sweep", open_pr_listing=listing(), now=october)

    assert seen["second"].exit_code == EXIT_REFUSED
    assert seen["second"].sentence.startswith(
        "cúramóir ran 'brief': refused (locked). another cúramóir job holds the lock: "
        "job 'sweep'")
    # The refused run released nothing: the first run's lock was still there
    # for its own release, which then removed it.
    assert [(line["job"], line["outcome"]) for line in job_lines(repo)] == [
        ("brief", "refused"), ("sweep", "success")]
    assert not the_lock(repo).exists()


def test_the_losing_side_of_the_create_race_refuses_locked(harness, monkeypatch):
    """Both runs pass the check before either writes: the create itself must
    decide, or both would hold the repository."""
    context = JobContext(job="sweep", repo=harness.repo)
    winner = LockSlot()
    real_create = job_guard.create_lock

    def create(repo, written):
        if json.loads(written)["job"] == "sweep" and not the_lock(harness).exists():
            # The other run's create lands between this run's check and its own.
            assert winner(JobContext(job="review", repo=harness.repo)) is None
        return real_create(repo, written)

    monkeypatch.setattr(job_guard, "create_lock", create)
    loser = LockSlot()

    refusal = loser(context)

    assert refusal.reason is Reason.LOCKED
    assert "job 'review'" in refusal.message
    loser.release()
    assert json.loads(the_lock(harness).read_text(encoding="utf-8"))["job"] == "review"
    winner.release()
    assert not the_lock(harness).exists()


#: Waits for the start signal (argv[3]) and then takes the lock for real,
#: with the reader's check switched off so every child reaches the create:
#: only the create's atomicity can then keep it to one. Reports what it got
#: on stdout and holds what it took long enough for the others to collide.
RACING_SLOT = """
import sys, time
from pathlib import Path
from curamoir import job_guard
from curamoir.guards import JobContext

job_guard.SupervisorLock.__call__ = lambda self, context: None
slot = job_guard.LockSlot()
Path(sys.argv[2]).write_text("ready", encoding="utf-8")
while not Path(sys.argv[3]).exists():
    time.sleep(0.001)
verdict = slot(JobContext(job="sweep", repo=Path(sys.argv[1])))
sys.stdout.write("acquired" if verdict is None else str(verdict.reason))
sys.stdout.flush()
time.sleep(1.5)
slot.release()
"""


def test_concurrent_runs_racing_for_the_create_admit_exactly_one(harness, tmp_path):
    go = tmp_path / "go"
    children, ready = [], []
    for index in range(6):
        ready.append(tmp_path / f"ready-{index}")
        children.append(subprocess.Popen(
            [sys.executable, "-c", RACING_SLOT, str(harness.repo), str(ready[-1]), str(go)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE))
    deadline = time.monotonic() + CHILD_SECONDS
    while not all(path.exists() for path in ready):
        assert time.monotonic() < deadline, "the racing children never became ready"
        time.sleep(0.02)

    go.write_text("go", encoding="utf-8")
    results = []
    for child in children:
        out, err = child.communicate(timeout=CHILD_SECONDS)
        assert child.returncode == 0, err.decode()
        results.append(out.decode())

    assert sorted(results) == ["acquired"] + ["locked"] * 5
    assert not the_lock(harness).exists()


def test_a_paused_repo_never_creates_the_lock(repo, monkeypatch):
    created = []
    real_create = job_guard.create_lock

    def create(repo_root, written):
        created.append(written)
        return real_create(repo_root, written)

    monkeypatch.setattr(job_guard, "create_lock", create)
    repo.write(".curamoir/PAUSE", "")

    ran = run_job(repo.repo, "brief", now=october)

    assert ran.exit_code == EXIT_REFUSED
    assert created == []
    assert not the_lock(repo).exists()


# Release removes only this slot's lock, and never masks the job's own error.

class _FrozenClock(datetime):
    """`datetime` whose `now` is pinned, so two locks written by this
    process for the same job differ only by their token."""

    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc).astimezone(tz)


def test_release_leaves_a_lock_this_slot_did_not_write(harness, capsys, monkeypatch):
    """Someone deleted this run's lock and another run of the SAME job took
    the repository, in the same second, from the same process: pid, host,
    job and start all match, so only the token tells the two locks apart,
    and the first run's release must not free the second run's lock."""
    from curamoir.guards import lock as lock_module

    monkeypatch.setattr(lock_module, "datetime", _FrozenClock)
    context = JobContext(job="sweep", repo=harness.repo)
    first = LockSlot()
    assert first(context) is None
    the_lock(harness).unlink()
    second = LockSlot()
    assert second(context) is None
    seconds = the_lock(harness).read_bytes()

    first.release()

    assert the_lock(harness).read_bytes() == seconds
    assert "not the lock this run wrote" in capsys.readouterr().err
    second.release()
    assert not the_lock(harness).exists()


def test_release_of_a_lock_already_gone_says_so_and_does_not_raise(harness, capsys):
    slot = LockSlot()
    assert slot(JobContext(job="sweep", repo=harness.repo)) is None
    the_lock(harness).unlink()

    slot.release()

    assert "already gone" in capsys.readouterr().err


def test_a_slot_that_refused_releases_nothing(harness):
    """It never wrote, so the lock that refused it is not its to remove."""
    holder = LockSlot()
    assert holder(JobContext(job="review", repo=harness.repo)) is None
    refused = LockSlot()

    assert refused(JobContext(job="sweep", repo=harness.repo)).reason is Reason.LOCKED
    refused.release()

    assert json.loads(the_lock(harness).read_text(encoding="utf-8"))["job"] == "review"
    holder.release()


def test_a_release_that_fails_does_not_mask_the_bodys_exception(repo, monkeypatch, capsys):
    real_unlink = Path.unlink

    def unlink(self, *args, **kwargs):
        if self.name == "lock":
            raise PermissionError(13, "Permission denied", str(self))
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)

    def body(_run):
        raise RuntimeError("disk on fire")

    with pytest.raises(RuntimeError, match="disk on fire"):
        run_guarded(repo.repo, "brief", None, body, now=october, opens_prs=False)

    err = capsys.readouterr().err
    assert "could not release the supervisor lock" in err
    assert "Permission denied" in err
    assert job_lines(repo)[-1]["message"] == "RuntimeError: disk on fire"


def test_a_lock_that_cannot_be_written_refuses_rather_than_running_unlocked(
        repo, monkeypatch):
    def create(_repo, _written):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(job_guard, "create_lock", create)

    ran = run_job(repo.repo, "brief", now=october)

    assert ran.exit_code == EXIT_REFUSED
    line = job_lines(repo)[-1]
    assert (line["outcome"], line["reason"]) == ("refused", "locked")
    assert "could not write the supervisor lock" in line["message"]


def test_lock_file_is_where_the_reader_looks(harness):
    assert lock_file(harness.repo) == the_lock(harness)


def test_a_state_that_is_not_a_directory_is_not_reported_as_a_lost_race(harness):
    """mkdir's FileExistsError is a `state` FILE, not another run's lock."""
    harness.write(".curamoir/state", "not a directory")
    slot = LockSlot()

    refusal = slot(JobContext(job="sweep", repo=harness.repo))

    assert refusal.reason is Reason.LOCKED
    assert refusal.message.startswith("could not write the supervisor lock at ")
    assert "another cúramóir job" not in refusal.message
    slot.release()
    assert (harness.repo / ".curamoir" / "state").read_text(encoding="utf-8") == "not a directory"


def test_a_signal_just_after_the_create_still_leaves_the_slot_able_to_release(
        harness, monkeypatch, capsys):
    """The slot knows its bytes before the file exists, so a Terminated that
    lands after the create but before the slot hears back does not leak the
    lock; and an interruption before any file existed says nothing."""
    real_create = job_guard.create_lock

    def create_then_signalled(repo, written):
        real_create(repo, written)
        raise Terminated(signal.SIGTERM)

    monkeypatch.setattr(job_guard, "create_lock", create_then_signalled)
    slot = LockSlot()
    with pytest.raises(Terminated):
        slot(JobContext(job="sweep", repo=harness.repo))
    assert the_lock(harness).exists()

    slot.release()

    assert not the_lock(harness).exists()

    def signalled_before(_repo, _written):
        raise Terminated(signal.SIGTERM)

    monkeypatch.setattr(job_guard, "create_lock", signalled_before)
    early = LockSlot()
    with pytest.raises(Terminated):
        early(JobContext(job="sweep", repo=harness.repo))
    early.release()

    assert capsys.readouterr().err == ""

    # Interrupted before its create, while another run's create won: that
    # lock is not this slot's, and this slot has nothing to say about it.
    others = lock_bytes("review")

    def another_run_won_then_signalled(repo, _written):
        real_create(repo, others)
        raise Terminated(signal.SIGTERM)

    monkeypatch.setattr(job_guard, "create_lock", another_run_won_then_signalled)
    beaten = LockSlot()
    with pytest.raises(Terminated):
        beaten(JobContext(job="sweep", repo=harness.repo))
    beaten.release()

    assert capsys.readouterr().err == ""
    assert the_lock(harness).read_bytes() == others
    forget_lock(others)
    the_lock(harness).unlink()


#: A `curamoir run brief` whose job-line write sends the process SIGTERM
#: just before it writes: the signal must wait for the line and the release.
SIGNALLED_DURING_THE_LINE = """
import os, signal, sys
from curamoir import cli
from curamoir.job_run import JobRun

real_write_line = JobRun._write_line

def write_line(self):
    os.kill(os.getpid(), signal.SIGTERM)
    return real_write_line(self)

JobRun._write_line = write_line
sys.exit(cli.main(["run", "brief", "--repo", sys.argv[1]]))
"""


def test_a_signal_during_the_job_line_write_waits_for_the_line_and_the_release(repo):
    child = subprocess.run([sys.executable, "-c", SIGNALLED_DURING_THE_LINE, str(repo.repo)],
                           capture_output=True, timeout=CHILD_SECONDS, check=False)

    assert child.returncode == -signal.SIGTERM, child.stderr.decode()
    line, = job_lines(repo)
    assert line["outcome"] == "success"
    assert not the_lock(repo).exists()


#: A `curamoir run brief` whose body swallows the scheduler's signal and
#: reports success: the signal is still delivered once the run is done.
SWALLOWING_BODY = """
import dataclasses, sys, time
from pathlib import Path
from curamoir import cli, runner

def body(_repo, run):
    Path(sys.argv[2]).write_text("started", encoding="utf-8")
    try:
        time.sleep(%d)
    except BaseException:
        pass
    run.succeeded("carried on regardless")

runner.JOBS["brief"] = dataclasses.replace(runner.JOBS["brief"], body=body)
sys.exit(cli.main(["run", "brief", "--repo", sys.argv[1]]))
"""


def test_a_body_that_swallows_the_signal_does_not_swallow_it_for_the_process(
        repo, tmp_path):
    started = tmp_path / "started"
    child = subprocess.Popen(
        [sys.executable, "-c", SWALLOWING_BODY.replace("%d", str(CHILD_SECONDS)),
         str(repo.repo), str(started)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.monotonic() + CHILD_SECONDS
    while not started.exists():
        assert child.poll() is None and time.monotonic() < deadline, "never started"
        time.sleep(0.02)

    child.send_signal(signal.SIGTERM)
    _, err = child.communicate(timeout=CHILD_SECONDS)

    assert child.returncode == -signal.SIGTERM, err.decode()
    assert job_lines(repo)[-1]["outcome"] == "success"
    assert not the_lock(repo).exists()


def test_a_signal_an_embedding_handler_absorbs_ends_the_run_with_a_sentence(
        repo, monkeypatch):
    """A Python handler installed before the run that returns rather than
    exits: the run stops, releases, and run_job reports it as a failure,
    not a traceback."""
    absorbed = []

    def body(_repo, _run):
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(CHILD_SECONDS)

    with_body(monkeypatch, "brief", body)
    previous = signal.signal(signal.SIGTERM, lambda signum, _frame: absorbed.append(signum))
    try:
        ran = run_job(repo.repo, "brief", now=october)
    finally:
        signal.signal(signal.SIGTERM, previous)

    assert ran.exit_code == EXIT_FAILURE
    assert ran.sentence == ("cúramóir did not complete 'brief': "
                            "Terminated: received SIGTERM.")
    assert absorbed == [signal.SIGTERM]
    assert job_lines(repo)[-1]["message"] == "Terminated: received SIGTERM"
    assert not the_lock(repo).exists()


def test_a_ctrl_c_during_the_release_waits_for_it_and_then_interrupts():
    """After `hold()` a ^C is deferred, so it cannot abandon the release; it
    arrives as KeyboardInterrupt once the run's block exits."""
    steps = []
    with pytest.raises(KeyboardInterrupt), TerminationSignals() as signals:
        signals.hold()
        os.kill(os.getpid(), signal.SIGINT)
        time.sleep(0.05)
        steps.append("released")

    assert steps == ["released"]
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


# Edge cases: odd files where the lock goes, forks, threads, nesting.

def test_a_dangling_symlink_at_the_lock_refuses_naming_the_file(harness, tmp_path):
    """Not "another job took the lock as this one was taking it", which
    would be false on every run: the file is there and is not a lock."""
    (harness.repo / ".curamoir" / "state").mkdir(parents=True)
    the_lock(harness).symlink_to(tmp_path / "nowhere")
    slot = LockSlot()

    refusal = slot(JobContext(job="sweep", repo=harness.repo))

    assert refusal.reason is Reason.LOCKED
    assert str(the_lock(harness)) in refusal.message
    assert "took the lock" not in refusal.message
    slot.release()
    assert the_lock(harness).is_symlink()


def test_a_symlink_the_check_cannot_see_is_named_by_the_slot(harness, tmp_path, monkeypatch):
    """The slot's own fallback, for a file the reader does not see: the
    create's EEXIST is then no race, and the message says what is there."""
    (harness.repo / ".curamoir" / "state").mkdir(parents=True)
    the_lock(harness).symlink_to(tmp_path / "nowhere")
    monkeypatch.setattr(SupervisorLock, "__call__", lambda self, context: None)

    refusal = LockSlot()(JobContext(job="sweep", repo=harness.repo))

    assert refusal.reason is Reason.LOCKED
    assert refusal.message == (f"a file at {the_lock(harness)} is not a readable lock "
                               "(a symlink?); delete it if no cúramóir job is running.")


def test_a_state_that_is_a_dangling_symlink_refuses_rather_than_running_unlocked(
        harness, tmp_path):
    (harness.repo / ".curamoir").mkdir()
    (harness.repo / ".curamoir" / "state").symlink_to(tmp_path / "nowhere")
    slot = LockSlot()

    refusal = slot(JobContext(job="sweep", repo=harness.repo))

    assert refusal.reason is Reason.LOCKED
    assert refusal.message.startswith("could not write the supervisor lock at ")
    assert not (tmp_path / "nowhere").exists()


def test_a_state_that_links_to_a_directory_elsewhere_holds_the_lock_there(harness, tmp_path):
    """The ledger follows the same link, so the lock does too: the repo's
    state lives where its `state` points, and is released from there."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (harness.repo / ".curamoir").mkdir()
    (harness.repo / ".curamoir" / "state").symlink_to(elsewhere)
    slot = LockSlot()

    assert slot(JobContext(job="sweep", repo=harness.repo)) is None

    assert json.loads((elsewhere / "lock").read_text(encoding="utf-8"))["job"] == "sweep"
    slot.release()
    assert not (elsewhere / "lock").exists()


def test_a_partial_write_then_a_full_disk_leaves_no_lock_and_refuses(harness, monkeypatch):
    real_write = os.write
    calls = []

    def write(descriptor, data):
        calls.append(len(data))
        if len(calls) == 1:
            return real_write(descriptor, bytes(data[:5]))
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(os, "write", write)
    slot = LockSlot()

    refusal = slot(JobContext(job="sweep", repo=harness.repo))

    monkeypatch.undo()
    assert len(calls) == 2
    assert refusal.reason is Reason.LOCKED
    assert "No space left on device" in refusal.message
    assert not os.path.lexists(the_lock(harness))
    slot.release()


def test_off_the_main_thread_signals_are_left_alone_and_the_lock_still_works(repo):
    before = {signum: signal.getsignal(signum) for signum in (*TERMINATING_SIGNALS, signal.SIGINT)}
    seen = {}

    def body(run):
        seen["held"] = the_lock(repo).exists()
        seen["handlers"] = {signum: signal.getsignal(signum) for signum in before}
        run.succeeded("ran in a worker thread")

    def worker():
        with TerminationSignals() as signals:
            signals.hold()
            seen["after hold"] = {signum: signal.getsignal(signum) for signum in before}
        seen["result"] = run_guarded(repo.repo, "brief", None, body, now=october,
                                     opens_prs=False)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(CHILD_SECONDS)

    assert seen["after hold"] == before
    assert seen["handlers"] == before
    assert seen["held"] is True
    assert seen["result"].outcome == "success"
    assert not the_lock(repo).exists()


def test_a_nested_run_on_the_same_repo_refuses_and_leaves_the_outer_lock_alone(repo):
    seen = {}

    def inner(run):
        run.succeeded("never")

    def outer(run):
        seen["before"] = the_lock(repo).read_bytes()
        seen["inner"] = run_guarded(repo.repo, "brief", None, inner, now=october,
                                    opens_prs=False)
        seen["after"] = the_lock(repo).read_bytes()
        run.succeeded("outer")

    run_guarded(repo.repo, "sweep", listing(), outer, now=october)

    assert (seen["inner"].outcome, seen["inner"].reason) == ("refused", Reason.LOCKED)
    assert "STALE" not in seen["inner"].message
    assert seen["after"] == seen["before"]
    assert not the_lock(repo).exists()


#: A run whose body forks; the child unwinds through run_guarded (its body
#: raises) and then exits, and the parent checks, after the child is gone,
#: that its own lock is still there. Reports on argv[2].
FORKING_BODY = """
import dataclasses, os, sys
from pathlib import Path
from curamoir import runner

parent = os.getpid()
lock = Path(sys.argv[1]) / ".curamoir" / "state" / "lock"

def body(_repo, run):
    child = os.fork()
    if child == 0:
        raise RuntimeError("the child unwinds")
    _, status = os.waitpid(child, 0)
    Path(sys.argv[2]).write_text(
        f"{os.waitstatus_to_exitcode(status)} {lock.exists()}", encoding="utf-8")
    run.succeeded("forked")

runner.JOBS["brief"] = dataclasses.replace(runner.JOBS["brief"], body=body)
ran = runner.run_job(sys.argv[1], "brief")
if os.getpid() != parent:
    os._exit(0)
sys.exit(ran.exit_code)
"""


def test_a_forked_child_does_not_release_its_parents_lock(repo, tmp_path):
    report = tmp_path / "report"

    child = subprocess.run([sys.executable, "-c", FORKING_BODY, str(repo.repo), str(report)],
                           capture_output=True, timeout=CHILD_SECONDS, check=False)

    assert child.returncode == EXIT_SUCCESS, child.stderr.decode()
    assert report.read_text(encoding="utf-8") == "0 True"
    assert not the_lock(repo).exists()


def test_a_forked_child_restores_the_handlers_and_does_not_resend_its_parents_signal(
        monkeypatch):
    """A forked child exiting through TerminationSignals restores the
    handlers (they are per process; left in place, it could never die by a
    SIGTERM) but does not re-send a signal its parent received."""
    from curamoir import termination

    before = signal.getsignal(signal.SIGTERM)
    signals = TerminationSignals()
    signals.__enter__()
    try:
        signals.hold()
        signals._handle(signal.SIGTERM, None)  # the parent's signal while held
        sent = []
        monkeypatch.setattr(termination.os, "getpid", lambda: -1)  # now "the child"
        monkeypatch.setattr(termination.os, "kill", lambda pid, signum: sent.append(signum))

        signals.__exit__(None, None, None)

        assert signal.getsignal(signal.SIGTERM) == before
        assert sent == []
    finally:
        monkeypatch.undo()
        for signum, previous in signals._previous.items():
            signal.signal(signum, previous)


#: A run whose body forks while the signals are still raising. The child
#: unwinds through run_guarded, says so on argv[2], and waits; the parent
#: waits for the child and reports how it ended on argv[3].
FORKED_CHILD_SIGNALLED = """
import dataclasses, os, sys, time
from pathlib import Path
from curamoir import runner

parent = os.getpid()

def body(_repo, run):
    child = os.fork()
    if child == 0:
        raise RuntimeError("the child unwinds")
    _, status = os.waitpid(child, 0)
    Path(sys.argv[3]).write_text(str(os.waitstatus_to_exitcode(status)), encoding="utf-8")
    run.succeeded("forked")

runner.JOBS["brief"] = dataclasses.replace(runner.JOBS["brief"], body=body)
ran = runner.run_job(sys.argv[1], "brief")
if os.getpid() != parent:
    Path(sys.argv[2]).write_text(str(os.getpid()), encoding="utf-8")
    time.sleep(%d)
    os._exit(42)
sys.exit(ran.exit_code)
"""


def test_a_forked_child_that_has_unwound_still_dies_by_sigterm(repo, tmp_path):
    unwound, ended = tmp_path / "unwound", tmp_path / "ended"
    parent = subprocess.Popen(
        [sys.executable, "-c", FORKED_CHILD_SIGNALLED.replace("%d", str(CHILD_SECONDS)),
         str(repo.repo), str(unwound), str(ended)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.monotonic() + CHILD_SECONDS
    while not unwound.exists() or not unwound.read_text(encoding="utf-8"):
        assert parent.poll() is None and time.monotonic() < deadline, "the child never unwound"
        time.sleep(0.02)

    os.kill(int(unwound.read_text(encoding="utf-8")), signal.SIGTERM)
    _, err = parent.communicate(timeout=CHILD_SECONDS)

    assert parent.returncode == EXIT_SUCCESS, err.decode()
    assert ended.read_text(encoding="utf-8") == str(-signal.SIGTERM)
    assert not the_lock(repo).exists()


def test_a_release_whose_unlink_fails_still_forgets_the_lock(harness, monkeypatch, capsys):
    """The lock is left behind, and this process no longer holds it: the
    same process then judges it stale rather than its own live lock."""
    context = JobContext(job="sweep", repo=harness.repo)
    slot = LockSlot()
    assert slot(context) is None
    real_unlink = Path.unlink

    def unlink(self, *args, **kwargs):
        if self.name == "lock":
            raise PermissionError(13, "Permission denied", str(self))
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    slot.release()
    monkeypatch.undo()

    assert "could not release the supervisor lock" in capsys.readouterr().err
    assert the_lock(harness).exists()
    refusal = SupervisorLock()(context)
    assert refusal.reason is Reason.LOCKED
    assert f"its pid {os.getpid()} is this process, which does not hold it" in refusal.message


# The signal phases, each against a real signal in a child `curamoir run`.

#: Waits in the child for a marker file under argv[2], failing loudly (the
#: child's stderr) rather than hanging when the test never writes it.
#: Shared by the child programs below; source for a child, not test code.
CHILD_MARKS = f"""
import dataclasses, os, signal, sys, time
from pathlib import Path
from curamoir import cli, job_guard, runner, termination
from curamoir.termination import Terminated

marks = Path(sys.argv[2])

def mark(name):
    (marks / name).write_text(name, encoding="utf-8")

def wait_for(name):
    deadline = time.monotonic() + {CHILD_SECONDS}
    while not (marks / name).exists():
        if time.monotonic() > deadline:
            raise RuntimeError("the test never wrote " + name)
        time.sleep(0.01)
"""


def start_child(program, harness, marks, first_mark):
    """Run `program` as `curamoir run`'s child, and wait for its first mark."""
    marks.mkdir(exist_ok=True)
    child = subprocess.Popen(
        [sys.executable, "-c", CHILD_MARKS + program, str(harness.repo), str(marks)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    wait_for_mark(child, marks / first_mark)
    return child


def wait_for_mark(child, path):
    deadline = time.monotonic() + CHILD_SECONDS
    while not path.exists():
        if child.poll() is not None or time.monotonic() > deadline:
            child.kill()
            _, err = child.communicate()
            pytest.fail(f"the child never wrote {path.name}: {err.decode()}")
        time.sleep(0.02)


#: The scheduler's SIGTERM lands as the job's block exits, before
#: HoldOnExit's hold(), so the run unwinds without HoldOnExit ever holding;
#: a ^C then arrives during the release. Only run_guarded's own hold() in
#: its `finally` stands between that ^C and an abandoned release.
SIGNALLED_BEFORE_THE_HOLD = """
real_hold_on_exit = termination.HoldOnExit.__exit__
real_release = job_guard.LockSlot.release

def hold_on_exit(self, *exc_info):
    os.kill(os.getpid(), signal.SIGTERM)
    return real_hold_on_exit(self, *exc_info)

def release(self):
    os.kill(os.getpid(), signal.SIGINT)
    return real_release(self)

termination.HoldOnExit.__exit__ = hold_on_exit
job_guard.LockSlot.release = release
sys.exit(cli.main(["run", "brief", "--repo", sys.argv[1]]))
"""


def test_a_ctrl_c_during_a_release_that_never_passed_the_hold_on_exit_still_releases(
        repo, tmp_path):
    child = subprocess.run(
        [sys.executable, "-c", CHILD_MARKS + SIGNALLED_BEFORE_THE_HOLD,
         str(repo.repo), str(tmp_path)],
        capture_output=True, timeout=CHILD_SECONDS, check=False)

    assert child.returncode == -signal.SIGTERM, child.stderr.decode()
    assert not the_lock(repo).exists()
    line, = job_lines(repo)
    assert line["outcome"] == "failure"
    assert "SIGTERM" in line["message"]


#: Started as `nohup` leaves a process, SIGHUP ignored; the body waits
#: until the test has sent its SIGHUP, then succeeds.
IGNORING_SIGHUP = """
signal.signal(signal.SIGHUP, signal.SIG_IGN)

def body(_repo, run):
    mark("started")
    wait_for("hung up")
    time.sleep(0.2)
    run.succeeded("ran through the hangup")

runner.JOBS["brief"] = dataclasses.replace(runner.JOBS["brief"], body=body)
sys.exit(cli.main(["run", "brief", "--repo", sys.argv[1]]))
"""


def test_a_run_started_ignoring_sighup_is_not_stopped_by_one(repo, tmp_path):
    """`nohup`: a SIGHUP stops nothing, so the run must not turn it into a
    Terminated that would."""
    marks = tmp_path / "marks"
    child = start_child(IGNORING_SIGHUP, repo, marks, "started")

    child.send_signal(signal.SIGHUP)
    (marks / "hung up").write_text("", encoding="utf-8")
    _, err = child.communicate(timeout=CHILD_SECONDS)

    assert child.returncode == EXIT_SUCCESS, err.decode()
    line, = job_lines(repo)
    assert (line["outcome"], line["message"]) == ("success", "ran through the hangup")
    assert not the_lock(repo).exists()


#: A body that cleans up on Terminated, taking its time: the test sends a
#: second, different signal (SIGHUP) while the cleanup runs.
CLEANING_UP_BODY = f"""
def body(_repo, _run):
    mark("started")
    try:
        time.sleep({CHILD_SECONDS})
    except Terminated:
        mark("caught")
        wait_for("second sent")
        time.sleep(0.2)
        mark("cleaned up")
        raise

runner.JOBS["brief"] = dataclasses.replace(runner.JOBS["brief"], body=body)
sys.exit(cli.main(["run", "brief", "--repo", sys.argv[1]]))
"""


def test_a_second_signal_neither_interrupts_the_cleanup_nor_replaces_the_first(
        repo, tmp_path):
    """Terminated is raised once, so a repeat cannot cut the body's cleanup
    short; and the first signal wins, so the process dies by SIGTERM, the
    signal that stopped the run, not by the SIGHUP that followed it."""
    marks = tmp_path / "marks"
    child = start_child(CLEANING_UP_BODY, repo, marks, "started")

    child.send_signal(signal.SIGTERM)
    wait_for_mark(child, marks / "caught")
    child.send_signal(signal.SIGHUP)
    (marks / "second sent").write_text("", encoding="utf-8")
    _, err = child.communicate(timeout=CHILD_SECONDS)

    assert (marks / "cleaned up").exists(), err.decode()
    assert child.returncode == -signal.SIGTERM, err.decode()
    assert not the_lock(repo).exists()
    line, = job_lines(repo)
    assert line["outcome"] == "failure"
    assert "SIGTERM" in line["message"]
