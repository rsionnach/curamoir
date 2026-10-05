"""E0-R2: the supervisor lock.

Exactly one autonomous cúramóir job runs against a repository at a time
(operating assumption 3). A job refuses to start if a live lock exists.
Liveness is a pid/host check, or age below a configured timeout.

Stale locks are REPORTED, not silently overridden — a lock left by a crash is
evidence, and silently stepping over it is how two agents end up writing at once.
"""

import json
import os

from curamoir.guards import Reason
from curamoir.guards.lock import SupervisorLock


def write_lock(harness, pid, host, started, job="sweep"):
    harness.write(
        ".curamoir/state/lock",
        json.dumps({"pid": pid, "host": host, "started": started, "job": job}),
    )


def test_no_lock_means_the_job_may_start(harness):
    result = harness.run(SupervisorLock(), job="sweep")

    result.assert_allowed()


def test_a_live_lock_from_this_host_refuses_the_job(harness):
    write_lock(harness, pid=os.getpid(), host=SupervisorLock.this_host(), started="2026-10-05T20:00:00+00:00")

    result = harness.run(SupervisorLock(), job="review")

    result.assert_refused(Reason.LOCKED)
    assert result.performed is False


def test_refusal_names_the_holder_so_the_developer_can_act(harness):
    write_lock(harness, pid=os.getpid(), host=SupervisorLock.this_host(), started="2026-10-05T20:00:00+00:00", job="sweep")

    result = harness.run(SupervisorLock(), job="review")

    assert str(os.getpid()) in result.refusal.message
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
