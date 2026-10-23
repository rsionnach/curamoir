"""E0-R2: the supervisor lock.

Exactly one autonomous cúramóir job runs against a repository at a time
(operating assumption 3). A job refuses to start if a live lock exists.
Liveness is a pid/host check, or age below a configured timeout.

Stale locks are REPORTED, not silently overridden — a lock left by a crash is
evidence, and silently stepping over it is how two agents end up writing at once.
"""

import json
import os
from datetime import datetime, timedelta, timezone

from curamoir.guards import Reason
from curamoir.guards.lock import SupervisorLock, lock_bytes


def write_lock(harness, pid, host, started, job="sweep"):
    harness.write(
        ".curamoir/state/lock",
        json.dumps({"pid": pid, "host": host, "started": started, "job": job}),
    )


def test_no_lock_means_the_job_may_start(harness):
    result = harness.run(SupervisorLock(), job="sweep")

    result.assert_allowed()


def test_a_live_lock_from_this_host_refuses_the_job(harness):
    # The parent's pid: alive, and not this process, which would hold the
    # lock only if it had written it (test_a_lock_naming_this_process...).
    write_lock(harness, pid=os.getppid(), host=SupervisorLock.this_host(), started="2026-10-05T20:00:00+00:00")

    result = harness.run(SupervisorLock(), job="review")

    result.assert_refused(Reason.LOCKED)
    assert result.performed is False


def test_refusal_names_the_holder_so_the_developer_can_act(harness):
    write_lock(harness, pid=os.getppid(), host=SupervisorLock.this_host(), started="2026-10-05T20:00:00+00:00", job="sweep")

    result = harness.run(SupervisorLock(), job="review")

    assert str(os.getppid()) in result.refusal.message
    assert "sweep" in result.refusal.message


def test_a_dead_pid_on_this_host_is_a_stale_lock_and_still_refuses(harness):
    """Stale means reported, not overridden. The job does not start, and the
    message says the lock looks stale so a human knows it is safe to clear."""
    write_lock(harness, pid=999999, host=SupervisorLock.this_host(), started="2026-10-05T20:00:00+00:00")

    result = harness.run(SupervisorLock(), job="sweep")

    result.assert_refused(Reason.LOCKED)
    assert "stale" in result.refusal.message.lower()


def test_a_lock_from_another_host_is_not_judged_by_pid(harness):
    """A pid is only meaningful on its own host. A lock from elsewhere is judged
    by age alone, never by whether that number happens to be running here.

    The pid here is DEAD locally and the lock is recent. Judged by age it is
    live; judged by pid it would be called stale. Asserting the message does
    NOT say "stale" is what makes this test discriminate.
    """
    write_lock(harness, pid=999999, host="some-other-machine", started="2026-10-05T20:00:00+00:00")

    result = harness.run(
        SupervisorLock(timeout_seconds=86400, now=lambda: _at("2026-10-05T20:05:00+00:00")),
        job="sweep",
    )

    result.assert_refused(Reason.LOCKED)
    assert "stale" not in result.refusal.message.lower()


def test_a_lock_older_than_the_timeout_is_stale(harness):
    write_lock(harness, pid=os.getpid(), host="some-other-machine", started="2026-10-01T20:00:00+00:00")

    result = harness.run(
        SupervisorLock(timeout_seconds=3600, now=lambda: _at("2026-10-05T20:00:00+00:00")),
        job="sweep",
    )

    result.assert_refused(Reason.LOCKED)
    assert "stale" in result.refusal.message.lower()


def test_an_unparseable_lock_refuses_rather_than_assuming_it_is_free(harness):
    harness.write(".curamoir/state/lock", "this is not json")

    result = harness.run(SupervisorLock(), job="sweep")

    result.assert_refused(Reason.LOCKED)


def _at(timestamp):
    from datetime import datetime
    return datetime.fromisoformat(timestamp)


def test_a_lock_naming_this_process_that_it_did_not_write_is_stale(harness):
    """A container restart or a reused pid: the lock names this process, on
    this host, but this process never took it. Judged by pid alone it would
    be live for ever."""
    write_lock(harness, pid=os.getpid(), host=SupervisorLock.this_host(),
               started=datetime.now(timezone.utc).isoformat())

    result = harness.run(SupervisorLock(), job="review")

    result.assert_refused(Reason.LOCKED)
    assert "STALE" in result.refusal.message
    assert f"its pid {os.getpid()} is this process, which does not hold it" in (
        result.refusal.message)


def test_a_live_pid_on_this_host_with_a_future_start_is_live(harness):
    """This host's clock went back after the lock was written: the pid is
    alive here, so the lock is live, not stale (and not to be deleted)."""
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    write_lock(harness, pid=os.getppid(), host=SupervisorLock.this_host(), started=future)

    result = harness.run(SupervisorLock(), job="review")

    result.assert_refused(Reason.LOCKED)
    assert "STALE" not in result.refusal.message
    assert result.refusal.message.startswith("another cúramóir job holds the lock")
    assert "start time is in the future" in result.refusal.message


def test_a_dangling_symlink_at_the_lock_is_not_a_free_repository(harness, tmp_path):
    (harness.repo / ".curamoir" / "state").mkdir(parents=True)
    (harness.repo / ".curamoir" / "state" / "lock").symlink_to(tmp_path / "nowhere")

    result = harness.run(SupervisorLock(), job="review")

    result.assert_refused(Reason.LOCKED)
    assert "could not be read" in result.refusal.message


def test_two_locks_for_the_same_job_in_the_same_instant_differ_by_their_token(monkeypatch):
    """The release compares bytes, so the bytes must be unique even when
    pid, host, job and start all match: the token is what makes them so."""
    from curamoir.guards import lock as lock_module

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(lock_module, "datetime", Frozen)

    first, second = json.loads(lock_bytes("sweep")), json.loads(lock_bytes("sweep"))

    assert first["started"] == second["started"]
    assert first["token"] != second["token"]
    assert {key: value for key, value in first.items() if key != "token"} == \
        {key: value for key, value in second.items() if key != "token"}
