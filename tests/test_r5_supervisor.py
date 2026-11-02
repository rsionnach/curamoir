"""The R5 supervisor's lock, state helper and parallel-block hook (cm-99l).

The user's rule: every R5 pass is its own lens, one reviewer at a time,
strictly in order, under a lock. These tests drive the shell helpers in
.claude/hooks against a scratch root (CURAMOIR_R5_ROOT), never the live
repository's lock or state, and pin the failure modes the correctness review
reproduced: two sessions both acquiring the lock, a crashed holder blocking
every session forever, two dispatches in one message both getting through,
and a stalled reviewer deadlocking the run.
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
    age_lock(r5root, 1000)
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


# ---------------------------------------------------------------- the hook, layer A

def test_another_sessions_live_lock_blocks_a_dispatch(r5root):
    run(LOCK, "acquire", "r5-supervise", "b1", root=r5root, session="HOLDER")
    assert hook(r5root, session="S2").returncode == 2
    assert hook(r5root, session="HOLDER").returncode == 0


def test_a_stale_lock_no_longer_blocks_other_sessions(r5root):
    run(LOCK, "acquire", "r5-supervise", "b1", root=r5root, session="HOLDER")
    age_lock(r5root, 1000)
    assert hook(r5root, session="S2").returncode == 0


def test_no_run_and_no_lock_lets_every_dispatch_through(r5root):
    assert hook(r5root).returncode == 0
    assert not (r5root / ".claude" / "r5-state.json").exists()


# ---------------------------------------------------------------- the hook, layer B

def start(root, bead="b1"):
    assert run(STATE, "init", bead, "base", "head", root=root).returncode == 0


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


def test_findings_close_the_window_and_a_fix_subagent_is_not_recorded(r5root):
    start(r5root)
    run(STATE, "pass", "correctness", root=r5root)
    hook(r5root)
    assert run(STATE, "findings", "correctness", "0", "2", "0", "FIX", root=r5root).returncode == 0
    assert hook(r5root).returncode == 0  # the fix subagent
    assert hook(r5root).returncode == 0  # and another: no reviewer was requested
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
    assert run(STATE, "abandon", "correctness", "stalled", root=r5root).returncode == 0
    assert run(STATE, "request", "correctness", "2", root=r5root).returncode == 0
    assert hook(r5root).returncode == 0
    assert events(r5root)[-3:] == ["reviewer_abandoned", "reviewer_requested", "reviewer_dispatched"]


def test_the_next_pass_cannot_start_while_a_reviewer_is_in_flight(r5root):
    start(r5root)
    run(STATE, "pass", "correctness", root=r5root)
    hook(r5root)
    assert run(STATE, "pass", "clarity", root=r5root).returncode == 2


def test_without_jq_a_run_in_flight_refuses_every_dispatch(r5root, tmp_path_factory):
    start(r5root)
    bindir = tmp_path_factory.mktemp("nojq")
    for tool in ("bash", "git", "date", "cat", "dirname", "grep", "mkdir", "rmdir", "stat", "sleep"):
        found = shutil.which(tool)
        if found:
            (bindir / tool).symlink_to(found)
    blocked = hook(r5root, path=str(bindir))
    assert blocked.returncode == 2 and "jq is missing" in blocked.stderr


# ---------------------------------------------------------------- the state helper

def test_a_pass_is_done_only_when_it_is_current_and_its_last_findings_are_clean(r5root):
    start(r5root)
    run(STATE, "pass", "correctness", root=r5root)
    hook(r5root)
    assert run(STATE, "done", "correctness", "0", root=r5root).returncode == 1  # no findings
    run(STATE, "findings", "correctness", "0", "1", "0", "FIX", root=r5root)
    assert run(STATE, "done", "correctness", "1", root=r5root).returncode == 1  # not clean
    assert run(STATE, "done", "clarity", "1", root=r5root).returncode == 1  # not current
    run(STATE, "request", "correctness", "2", root=r5root)
    hook(r5root)
    run(STATE, "findings", "correctness", "0", "0", "3", "PASS", root=r5root)
    assert run(STATE, "done", "correctness", "1", root=r5root).returncode == 0
    assert state(r5root)["current_pass"] is None
    assert [p["name"] for p in state(r5root)["completed_passes"]] == ["correctness"]


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


def test_bad_json_leaves_the_state_untouched_and_no_temp_file(r5root):
    start(r5root)
    before = (r5root / ".claude" / "r5-state.json").read_text()
    assert run(STATE, "event", "x", "not-json", root=r5root).returncode != 0
    assert (r5root / ".claude" / "r5-state.json").read_text() == before
    assert not list((r5root / ".claude").glob("*.tmp.*"))


# ---------------------------------------------------------------- the resolver

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
