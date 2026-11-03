"""The R5 supervisor's lock, state helper and parallel-block hook (cm-99l).

The user's rule: every R5 pass is its own lens, one reviewer at a time,
strictly in order, under a lock. These tests drive the shell helpers in
.claude/hooks against a scratch root (CURAMOIR_R5_ROOT), never the live
repository's lock or state, and pin the failure modes the correctness review
reproduced: two sessions both acquiring the lock, a crashed holder blocking
every session forever (through the lock or through the state), two
dispatches in one message both getting through, a stalled reviewer
deadlocking the run, a pass closed while its re-review was still running,
and a killed process leaving a mutex behind.
"""

import datetime
import json
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / ".claude" / "hooks"
LOCK = HOOKS / "r5-lock.sh"
STATE = HOOKS / "r5-state.sh"
HOOK = HOOKS / "r5-parallel-block.sh"

pytestmark = pytest.mark.skipif(shutil.which("jq") is None or shutil.which("bash") is None,
                                reason="the R5 helpers need bash and jq")


@pytest.fixture
def r5root(tmp_path):
    """A scratch root the resolver accepts: a curamoir plugin.json."""
    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin" / "plugin.json").write_text('{"name": "curamoir"}')
    return tmp_path


def run(script, *args, root, session="S1", stdin="", env_extra=None, path=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CURAMOIR_", "R5_"))}
    env.update(CURAMOIR_R5_ROOT=str(root), CLAUDE_CODE_SESSION_ID=session)
    if path is not None:
        env["PATH"] = path
    env.update(env_extra or {})
    return subprocess.run(["bash", str(script), *args], input=stdin, capture_output=True,
                          text=True, env=env, cwd=root, timeout=60)


def lock_file(root):
    return root / ".claude" / "r5-supervisor.lock"


def state(root):
    return json.loads((root / ".claude" / "r5-state.json").read_text())


def events(root):
    return [e["event"] for e in state(root)["audit_trail"]]


def hook(root, session="S1", subagent="general-purpose", **kw):
    payload = json.dumps({"tool_input": {"subagent_type": subagent}})
    return run(HOOK, root=root, session=session, stdin=payload, **kw)


PAST_LEASE = 900 + 100  # older than the lock's lease (lib/lease.sh)


def start(root, bead="b1"):
    assert run(STATE, "init", bead, "base", "head", root=root).returncode == 0


def clear_pass(root, name):
    """Run one pass with a clean review, as the supervisor would."""
    run(STATE, "pass", name, root=root)
    hook(root)
    run(STATE, "findings", name, "0", "0", "0", root=root)
    assert run(STATE, "done", name, "0", root=root).returncode == 0


def age_lock(root, seconds):
    """Rewrite the lock as if it were last refreshed `seconds` ago."""
    data = json.loads(lock_file(root).read_text())
    old = datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=seconds)
    data["refreshed_at"] = old.strftime("%Y-%m-%dT%H:%M:%SZ")
    lock_file(root).write_text(json.dumps(data))


# ---------------------------------------------------------------- the lock

def test_of_many_sessions_acquiring_at_once_exactly_one_holds_the_lock(r5root):
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(
            lambda i: run(LOCK, "acquire", "r5-supervise", f"b{i}", root=r5root, session=f"S{i}"),
            range(8)))
    winners = [i for i, r in enumerate(results) if r.returncode == 0]
    assert len(winners) == 1, [(r.returncode, r.stderr) for r in results]
    assert json.loads(lock_file(r5root).read_text())["session_id"] == f"S{winners[0]}"
    assert all(r.returncode == 2 for i, r in enumerate(results) if i not in winners)


def test_of_many_sessions_reclaiming_a_stale_lock_exactly_one_wins(r5root):
    assert run(LOCK, "acquire", "r5-supervise", "old", root=r5root, session="DEAD").returncode == 0
    age_lock(r5root, PAST_LEASE)
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(
            lambda i: run(LOCK, "acquire", "r5-supervise", f"b{i}", root=r5root, session=f"S{i}"),
            range(8)))
    assert [r.returncode for r in results].count(0) == 1, [(r.returncode, r.stderr) for r in results]
    assert json.loads(lock_file(r5root).read_text())["session_id"] != "DEAD"


def test_a_live_lock_refuses_another_session_and_nesting(r5root):
    assert run(LOCK, "acquire", "r5-supervise", "b1", root=r5root).returncode == 0
    assert run(LOCK, "acquire", "r5-supervise", "b2", root=r5root, session="S2").returncode == 2
    assert run(LOCK, "acquire", "epic-executor", "e1", root=r5root).returncode == 2


def test_the_same_session_cannot_retarget_its_own_run(r5root):
    assert run(LOCK, "acquire", "r5-supervise", "b1", root=r5root).returncode == 0
    assert run(LOCK, "acquire", "r5-supervise", "b1", root=r5root).returncode == 0  # idempotent
    other = run(LOCK, "acquire", "r5-supervise", "b2", root=r5root)
    assert other.returncode == 2 and "b1" in other.stderr
    assert json.loads(lock_file(r5root).read_text())["target"] == "b1"


def test_only_the_holder_refreshes_or_releases(r5root):
    assert run(LOCK, "acquire", "r5-supervise", "b1", root=r5root).returncode == 0
    assert run(LOCK, "refresh", "r5-supervise", root=r5root, session="S2").returncode == 1
    assert run(LOCK, "release", "r5-supervise", root=r5root, session="S2").returncode == 1
    assert lock_file(r5root).exists()
    assert run(LOCK, "release", "r5-supervise", root=r5root).returncode == 0
    assert not lock_file(r5root).exists()


# ---------------------------------------------------------------- the hook: another session's lock (layer A)

def test_another_sessions_live_lock_blocks_a_dispatch(r5root):
    run(LOCK, "acquire", "r5-supervise", "b1", root=r5root, session="HOLDER")
    assert hook(r5root, session="S2").returncode == 2
    assert hook(r5root, session="HOLDER").returncode == 0


def test_a_stale_lock_no_longer_blocks_other_sessions(r5root):
    run(LOCK, "acquire", "r5-supervise", "b1", root=r5root, session="HOLDER")
    age_lock(r5root, PAST_LEASE)
    assert hook(r5root, session="S2").returncode == 0


def test_no_run_and_no_lock_lets_every_dispatch_through(r5root):
    assert hook(r5root).returncode == 0
    assert not (r5root / ".claude" / "r5-state.json").exists()


# ---------------------------------------------------------------- the run's windows (claim, via the hook)

def test_a_requested_reviewer_is_let_through_once_and_recorded(r5root):
    start(r5root)
    assert run(STATE, "pass", "correctness", root=r5root).returncode == 0
    first = hook(r5root, subagent="pr-review-toolkit:code-reviewer")
    assert first.returncode == 0
    assert events(r5root)[-1] == "reviewer_dispatched"
    assert state(r5root)["audit_trail"][-1]["pass"] == "correctness"
    second = hook(r5root, subagent="general-purpose")
    assert second.returncode == 2 and "in flight" in second.stderr


def test_two_dispatches_issued_together_let_exactly_one_through(r5root):
    start(r5root)
    run(STATE, "pass", "correctness", root=r5root)
    with ThreadPoolExecutor(6) as pool:
        results = list(pool.map(lambda _: hook(r5root), range(6)))
    assert [r.returncode for r in results].count(0) == 1, [r.stderr for r in results]
    assert events(r5root).count("reviewer_dispatched") == 1


def test_in_a_fix_round_one_fix_agent_runs_at_a_time(r5root):
    start(r5root)
    run(STATE, "pass", "correctness", root=r5root)
    hook(r5root)
    assert run(STATE, "findings", "correctness", "0", "2", "0", "FIX", root=r5root).returncode == 0
    assert hook(r5root).returncode == 0  # the fix agent, recorded
    assert events(r5root)[-1] == "fix_dispatched"
    assert hook(r5root).returncode == 2  # a second, while the first is open
    assert run(STATE, "request", "correctness", "2", root=r5root).returncode == 2
    run(STATE, "fixed", "correctness", root=r5root)
    assert run(STATE, "request", "correctness", "2", root=r5root).returncode == 0
    assert events(r5root).count("reviewer_dispatched") == 1


def test_a_re_dispatch_is_requested_then_claimed(r5root):
    start(r5root)
    run(STATE, "pass", "correctness", root=r5root)
    hook(r5root)
    run(STATE, "findings", "correctness", "0", "1", "0", "FIX", root=r5root)
    assert run(STATE, "request", "correctness", "2", root=r5root).returncode == 0
    assert hook(r5root).returncode == 0
    assert hook(r5root).returncode == 2
    assert events(r5root).count("reviewer_dispatched") == 2


def test_a_stalled_reviewer_is_abandoned_then_requested_again(r5root):
    start(r5root)
    run(STATE, "pass", "correctness", root=r5root)
    hook(r5root)
    assert run(STATE, "request", "correctness", "2", root=r5root).returncode == 2
    assert run(STATE, "abandon", "clarity", "stalled", root=r5root).returncode == 2  # not current
    assert run(STATE, "abandon", "correctness", "stalled", root=r5root).returncode == 0
    assert hook(r5root).returncode == 2  # nothing until the reviewer is requested again
    assert run(STATE, "request", "correctness", "2", root=r5root).returncode == 0
    assert hook(r5root).returncode == 0
    assert events(r5root)[-3:] == ["reviewer_abandoned", "reviewer_requested", "reviewer_dispatched"]


def test_a_bookkeeping_event_never_reopens_a_window(r5root):
    start(r5root)
    run(STATE, "pass", "correctness", root=r5root)
    hook(r5root)
    run(STATE, "event", "session_restarted", root=r5root)
    assert hook(r5root).returncode == 2
    assert run(STATE, "event", "reviewer_abandoned", root=r5root).returncode == 1
    assert run(STATE, "event", "findings_received", root=r5root).returncode == 1
    assert events(r5root)[-1] == "session_restarted"


def test_fixed_only_follows_the_current_pass_s_findings(r5root):
    start(r5root)
    run(STATE, "pass", "correctness", root=r5root)
    assert run(STATE, "fixed", "correctness", root=r5root).returncode == 2  # requested
    hook(r5root)
    assert run(STATE, "fixed", "correctness", root=r5root).returncode == 2  # running
    assert hook(r5root).returncode == 2
    assert events(r5root)[-1] == "reviewer_dispatched"
    run(STATE, "findings", "correctness", "0", "1", "0", "FIX", root=r5root)
    assert run(STATE, "fixed", "correctness", root=r5root).returncode == 0
    assert state(r5root)["audit_trail"][-1]["pass"] == "correctness"


# ---------------------------------------------------------------- run ownership across sessions

def test_another_sessions_crashed_run_stops_blocking_once_its_lease_runs_out(r5root):
    run(LOCK, "acquire", "r5-supervise", "b1", root=r5root, session="S1")
    start(r5root)
    run(STATE, "pass", "correctness", root=r5root)
    assert hook(r5root, session="S2").returncode == 2  # S1's lease is live
    hook(r5root)  # S1 dispatches its reviewer, then crashes
    age_lock(r5root, PAST_LEASE)
    before = events(r5root)
    assert hook(r5root, session="S2").returncode == 0
    assert hook(r5root, session="S2").returncode == 0
    assert events(r5root) == before  # S2's dispatches are not the run's


def test_a_crash_before_the_dispatch_does_not_record_another_sessions_agent(r5root):
    run(LOCK, "acquire", "r5-supervise", "b1", root=r5root, session="S1")
    start(r5root)
    run(STATE, "pass", "correctness", root=r5root)  # S1 crashes here
    age_lock(r5root, PAST_LEASE)
    assert hook(r5root, session="S2").returncode == 0
    assert events(r5root)[-1] == "pass_started"


def test_another_session_is_refused_unless_the_owner_s_lease_has_lapsed(r5root):
    start(r5root)  # S1 owns the run, with no lock at all
    run(STATE, "pass", "correctness", root=r5root)
    refused = hook(r5root, session="S2")
    assert refused.returncode == 2 and "belongs to session" in refused.stderr and "another session's run" in refused.stderr
    run(LOCK, "acquire", "r5-supervise", "b1", root=r5root, session="S3")
    age_lock(r5root, PAST_LEASE)  # a third session's lapsed lock is not the owner's
    assert hook(r5root, session="S2").returncode == 2
    assert events(r5root)[-1] == "pass_started"


def test_a_new_session_adopts_a_run_only_while_holding_the_lock(r5root):
    run(LOCK, "acquire", "r5-supervise", "b1", root=r5root, session="S1")
    start(r5root)
    run(STATE, "pass", "correctness", root=r5root)
    hook(r5root)
    age_lock(r5root, PAST_LEASE)
    assert run(STATE, "adopt", root=r5root, session="S2").returncode == 2  # no lock yet
    assert run(LOCK, "acquire", "r5-supervise", "b1", root=r5root, session="S2").returncode == 0
    assert run(STATE, "adopt", root=r5root, session="S2").returncode == 0
    assert state(r5root)["session_id"] == "S2"
    assert hook(r5root, session="S2").returncode == 2  # S1's reviewer is still recorded in flight
    run(STATE, "abandon", "correctness", "its session crashed", root=r5root, session="S2")
    run(STATE, "request", "correctness", "2", root=r5root, session="S2")
    assert hook(r5root, session="S2").returncode == 0


def test_a_session_whose_run_was_adopted_can_no_longer_change_it(r5root):
    run(LOCK, "acquire", "r5-supervise", "b1", root=r5root, session="S1")
    start(r5root)
    run(STATE, "pass", "correctness", root=r5root)
    hook(r5root)
    age_lock(r5root, PAST_LEASE)
    run(LOCK, "acquire", "r5-supervise", "b1", root=r5root, session="S2")
    assert run(STATE, "adopt", root=r5root, session="S2").returncode == 0
    for args in (("finish", "aborted"), ("findings", "correctness", "0", "0", "0"),
                 ("abandon", "correctness", "x"), ("event", "note")):
        stale = run(STATE, *args, root=r5root, session="S1")
        assert stale.returncode == 4 and "adopt it first" in stale.stderr, args
    assert (r5root / ".claude" / "r5-state.json").exists()
    assert run(STATE, "abandon", "correctness", "S1 is gone", root=r5root, session="S2").returncode == 0


# ---------------------------------------------------------------- passes and the run

def test_the_next_pass_cannot_start_while_a_reviewer_is_in_flight(r5root):
    start(r5root)
    run(STATE, "pass", "correctness", root=r5root)
    hook(r5root)
    assert run(STATE, "pass", "clarity", root=r5root).returncode == 2


def test_passes_run_in_order_and_one_at_a_time(r5root):
    start(r5root)
    assert run(STATE, "pass", "clarity", root=r5root).returncode == 2
    assert run(STATE, "pass", "excellence", root=r5root).returncode == 2
    assert run(STATE, "pass", "correctness", root=r5root).returncode == 0
    hook(r5root)
    run(STATE, "findings", "correctness", "0", "0", "0", "PASS", root=r5root)
    assert run(STATE, "pass", "clarity", root=r5root).returncode == 2  # correctness not done
    run(STATE, "done", "correctness", "0", root=r5root)
    assert run(STATE, "pass", "edge-cases", root=r5root).returncode == 2
    assert run(STATE, "pass", "clarity", root=r5root).returncode == 0


def test_a_pass_is_done_only_when_it_is_current_and_its_last_findings_are_clean(r5root):
    start(r5root)
    run(STATE, "pass", "correctness", root=r5root)
    hook(r5root)
    assert run(STATE, "done", "correctness", "0", root=r5root).returncode == 2  # no findings
    run(STATE, "findings", "correctness", "0", "1", "0", "FIX", root=r5root)
    assert run(STATE, "done", "correctness", "1", root=r5root).returncode == 2  # not clean
    assert run(STATE, "done", "clarity", "1", root=r5root).returncode == 2  # not current
    run(STATE, "request", "correctness", "2", root=r5root)
    hook(r5root)
    run(STATE, "findings", "correctness", "0", "0", "3", "PASS", root=r5root)
    assert run(STATE, "done", "correctness", "1", root=r5root).returncode == 0
    assert state(r5root)["current_pass"] is None
    assert [p["name"] for p in state(r5root)["completed_passes"]] == ["correctness"]


def test_a_pass_cannot_be_done_while_its_re_review_is_requested_or_running(r5root):
    start(r5root)
    run(STATE, "pass", "correctness", root=r5root)
    hook(r5root)
    run(STATE, "findings", "correctness", "0", "0", "1", "PASS", root=r5root)
    run(STATE, "fixed", "correctness", root=r5root)
    run(STATE, "request", "correctness", "2", root=r5root)
    assert run(STATE, "done", "correctness", "1", root=r5root).returncode == 2  # requested
    hook(r5root)
    assert run(STATE, "done", "correctness", "1", root=r5root).returncode == 2  # running
    assert run(STATE, "pass", "clarity", root=r5root).returncode == 2


def test_findings_need_the_current_pass_and_a_dispatched_reviewer(r5root):
    start(r5root)
    run(STATE, "pass", "correctness", root=r5root)
    assert run(STATE, "findings", "correctness", "0", "0", "0", root=r5root).returncode == 2
    hook(r5root)
    assert run(STATE, "findings", "clarity", "0", "0", "0", root=r5root).returncode == 2
    assert run(STATE, "advisory", "0", "0", "0", "PASS", root=r5root).returncode == 2
    assert run(STATE, "findings", "correctness", "0", "0", "0", root=r5root).returncode == 0


def test_a_dirty_tree_after_provenance_fails_the_pass_until_it_is_re_reviewed(r5root):
    start(r5root)
    for name in ("correctness", "clarity", "edge-cases"):
        clear_pass(r5root, name)
    run(STATE, "pass", "provenance", root=r5root)
    hook(r5root)
    run(STATE, "findings", "provenance", "0", "0", "0", root=r5root)
    assert run(STATE, "tree-dirty", "provenance", root=r5root).returncode == 0
    assert run(STATE, "done", "provenance", "0", root=r5root).returncode == 2
    assert run(STATE, "request", "provenance", "2", root=r5root).returncode == 0
    assert hook(r5root).returncode == 0
    assert events(r5root)[-1] == "reviewer_dispatched"


def test_finish_completed_needs_every_pass_and_the_advisory_read(r5root):
    start(r5root)
    run(STATE, "pass", "correctness", root=r5root)
    refused = run(STATE, "finish", "completed", root=r5root)
    assert refused.returncode == 2 and "clarity" in refused.stderr
    hook(r5root)
    run(STATE, "findings", "correctness", "0", "0", "0", root=r5root)
    run(STATE, "done", "correctness", "0", root=r5root)
    for name in ("clarity", "edge-cases", "provenance"):
        clear_pass(r5root, name)
    assert run(STATE, "finish", "completed", root=r5root).returncode == 2  # no advisory read
    run(STATE, "pass", "excellence", root=r5root)
    hook(r5root)
    assert run(STATE, "findings", "excellence", "0", "0", "0", root=r5root).returncode == 1
    assert run(STATE, "done", "excellence", "0", root=r5root).returncode == 1
    assert run(STATE, "advisory", "0", "0", "1", "PASS", root=r5root).returncode == 0
    assert run(STATE, "finish", "completed", root=r5root).returncode == 0


def test_reopen_runs_a_done_pass_and_every_later_one_again(r5root):
    start(r5root)
    clear_pass(r5root, "correctness")
    clear_pass(r5root, "clarity")
    assert run(STATE, "reopen", "edge-cases", root=r5root).returncode == 2  # not done
    assert run(STATE, "reopen", "correctness", root=r5root).returncode == 0
    assert state(r5root)["completed_passes"] == []
    assert state(r5root)["audit_trail"][-1]["dropped"] == ["correctness", "clarity"]
    assert run(STATE, "pass", "clarity", root=r5root).returncode == 2
    clear_pass(r5root, "correctness")
    run(STATE, "pass", "clarity", root=r5root)
    hook(r5root)
    assert run(STATE, "reopen", "correctness", root=r5root).returncode == 2  # a reviewer in flight


def test_the_excellence_in_flight_refusal_names_advisory(r5root):
    start(r5root)
    for name in ("correctness", "clarity", "edge-cases", "provenance"):
        clear_pass(r5root, name)
    run(STATE, "pass", "excellence", root=r5root)
    hook(r5root)
    refused = hook(r5root)
    assert refused.returncode == 2 and "`advisory ...`" in refused.stderr


def test_a_second_run_cannot_start_while_one_is_in_flight(r5root):
    start(r5root, "b1")
    assert run(STATE, "init", "b2", "x", "y", root=r5root).returncode == 2
    assert state(r5root)["target_bead"] == "b1"


def test_finish_archives_the_trail_under_a_timestamp(r5root):
    start(r5root)
    assert run(STATE, "finish", "aborted", root=r5root).returncode == 0
    archived = list((r5root / ".claude").glob("r5-state-b1-aborted-*.json"))
    assert len(archived) == 1
    assert json.loads(archived[0].read_text())["status"] == "aborted"
    assert not (r5root / ".claude" / "r5-state.json").exists()


# ---------------------------------------------------------------- arguments and infrastructure

def test_an_event_cannot_rename_itself_or_take_a_non_object(r5root):
    start(r5root)
    run(STATE, "pass", "correctness", root=r5root)
    hook(r5root)
    assert run(STATE, "event", "note", '{"event": "findings_received", "ts": "x"}',
               root=r5root).returncode == 0
    last = state(r5root)["audit_trail"][-1]
    assert last["event"] == "note" and last["ts"] != "x"
    assert hook(r5root).returncode == 2
    assert run(STATE, "event", "note", "[1]", root=r5root).returncode == 1


def test_a_bad_or_missing_argument_exits_1_not_as_a_refusal(r5root):
    start(r5root)
    run(STATE, "pass", "correctness", root=r5root)
    hook(r5root)
    for args in (("findings", "correctness", "x", "0", "0"), ("findings", "correctness"),
                 ("request", "correctness", "two"), ("init",)):
        bad = run(STATE, *args, root=r5root)
        assert bad.returncode == 1 and bad.stderr.startswith("r5-state: "), (args, bad.stderr)
    assert events(r5root)[-1] == "reviewer_dispatched"


def test_bad_json_leaves_the_state_untouched_and_no_temp_file(r5root):
    start(r5root)
    before = (r5root / ".claude" / "r5-state.json").read_text()
    assert run(STATE, "event", "x", "not-json", root=r5root).returncode != 0
    assert (r5root / ".claude" / "r5-state.json").read_text() == before
    assert not list((r5root / ".claude").glob("*.tmp.*"))


def test_a_killed_process_holding_the_mutex_does_not_block_the_next_call(r5root):
    start(r5root)
    holder = subprocess.Popen(
        ["bash", "-c", f'source "{HOOKS}/lib/flock.sh"; _r5_flock "$1" t; echo held; exec sleep 60',
         "_", str(r5root / ".claude" / "r5-state.json.flock")],
        stdout=subprocess.PIPE, text=True)
    blocked = None
    try:
        assert holder.stdout.readline().strip() == "held"
        blocked = subprocess.Popen(["bash", str(STATE), "show"], env={
            **os.environ, "CURAMOIR_R5_ROOT": str(r5root)}, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        with pytest.raises(subprocess.TimeoutExpired):
            blocked.wait(timeout=1)  # waits on the held mutex
        holder.kill()
        assert blocked.wait(timeout=5) == 0
    finally:
        for proc in (holder, blocked):
            if proc is not None:
                proc.kill()
                proc.wait()
        holder.stdout.close()


def test_without_jq_a_run_in_flight_refuses_every_dispatch(r5root, tmp_path_factory):
    start(r5root)
    bindir = tmp_path_factory.mktemp("nojq")
    for tool in ("bash", "git", "date", "cat", "dirname", "grep", "mkdir", "rmdir", "stat", "sleep"):
        found = shutil.which(tool)
        if found:
            (bindir / tool).symlink_to(found)
    blocked = hook(r5root, path=str(bindir))
    assert blocked.returncode == 2 and "jq is missing" in blocked.stderr


def test_an_inherited_r5_root_is_ignored(r5root, tmp_path_factory):
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    assert run(LOCK, "acquire", "r5-supervise", "b1", root=r5root,
               env_extra={"R5_ROOT": str(elsewhere)}).returncode == 0
    assert lock_file(r5root).exists()
    assert not (elsewhere / ".claude").exists()


def test_outside_a_curamoir_checkout_the_helpers_refuse_and_the_hook_allows(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CURAMOIR_", "R5_"))}
    lock = subprocess.run(["bash", str(LOCK), "status"], capture_output=True, text=True,
                          env=env, cwd=tmp_path, timeout=60)
    assert lock.returncode == 3
    allowed = subprocess.run(["bash", str(HOOK)], input="{}", capture_output=True, text=True,
                             env=env, cwd=tmp_path, timeout=60)
    assert allowed.returncode == 0
