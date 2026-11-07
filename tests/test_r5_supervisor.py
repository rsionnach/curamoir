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
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / ".claude" / "hooks"
LOCK = HOOKS / "r5-lock.sh"
STATE = HOOKS / "r5-state.sh"
HOOK = HOOKS / "r5-parallel-block.sh"

# The helpers need bash, jq and python3 (lib/flock.sh, lib/r5_findings.py).
# Locally a missing tool
# skips the module; in CI it is a failure, so the guard's tests can never
# be skipped silently there.
_MISSING_TOOLS = [t for t in ("bash", "jq", "python3") if shutil.which(t) is None]
if _MISSING_TOOLS and os.environ.get("CI"):
    pytest.fail(f"the R5 helpers need {', '.join(_MISSING_TOOLS)}; CI must install them",
                pytrace=False)
pytestmark = pytest.mark.skipif(bool(_MISSING_TOOLS),
                                reason="the R5 helpers need bash, jq and python3")


@pytest.fixture
def r5root(tmp_path):
    """A scratch root the resolver accepts: a curamoir plugin.json."""
    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin" / "plugin.json").write_text('{"name": "curamoir"}')
    return tmp_path


_REPORTS = {}


@pytest.fixture(autouse=True)
def _report_dir(tmp_path_factory):
    _REPORTS["dir"] = tmp_path_factory.mktemp("reports")


def finding(lens, severity, n=1, level="script", **extra):
    """One finding in the shared schema (docs/r5-gate-v2-curamoir-spec.md §10)."""
    return {"lens": lens, "severity": severity, "path": f"src/m{n}.py",
            "start_line": n, "end_line": n + 2, "claim": f"{severity.lower()} defect {n}",
            "evidence": "the lines", "fix": "change it", "verification_level": level,
            **extra}


def report(pass_name, critical=0, important=0, minor=0, findings=None, prose="Review.\n"):
    """A reviewer's report whose last line is R5-FINDINGS: with these counts
    (or these findings); returns its path."""
    lens = pass_name.replace("-", "_")
    if findings is None:
        findings = ([finding(lens, "CRITICAL", 1 + n) for n in range(critical)]
                    + [finding(lens, "IMPORTANT", 10 + n) for n in range(important)]
                    + [finding(lens, "MINOR", 20 + n) for n in range(minor)])
    path = _REPORTS["dir"] / f"report-{len(list(_REPORTS['dir'].iterdir()))}.md"
    path.write_text(prose + "R5-FINDINGS: " + json.dumps(findings) + "\n")
    return str(path)


_SESSION_VARS = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID")


def env_for(root, session="S1"):
    """The helpers' environment: the scratch root, and `session` as the only
    session id (None: no session id in the environment at all)."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("CURAMOIR_", "R5_")) and k not in _SESSION_VARS}
    env["CURAMOIR_R5_ROOT"] = str(root)
    if session is not None:
        env["CLAUDE_CODE_SESSION_ID"] = session
    return env


def run(script, *args, root, session="S1", stdin="", env_extra=None, path=None):
    env = env_for(root, session)
    if path is not None:
        env["PATH"] = path
    env.update(env_extra or {})
    return subprocess.run(["bash", str(script), *args], input=stdin, capture_output=True,
                          text=True, env=env, cwd=root, timeout=60)


def lock_file(root):
    return root / ".claude" / "r5-supervisor.lock"


def state_file(root):
    return root / ".claude" / "r5-state.json"


def state(root):
    return json.loads(state_file(root).read_text())


def events(root):
    return [e["event"] for e in state(root)["audit_trail"]]


def payload(session="S1", subagent="general-purpose", cwd="/tmp"):
    """A PreToolUse payload for the Agent tool, in the documented shape
    (code.claude.com/docs/en/hooks.md)."""
    return json.dumps({
        "session_id": session, "transcript_path": "/tmp/transcript.jsonl", "cwd": str(cwd),
        "permission_mode": "default", "hook_event_name": "PreToolUse", "tool_name": "Agent",
        "tool_input": {"prompt": "Review the change.", "description": "R5 review",
                       "subagent_type": subagent}})


def hook(root, session="S1", subagent="general-purpose", **kw):
    return run(HOOK, root=root, session=session, stdin=payload(session, subagent, root), **kw)


def write_lock(root, session, supervisor="r5-supervise", target="b1"):
    """A fresh lock held by `session`, written as r5-lock.sh writes one."""
    now = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    lock_file(root).parent.mkdir(parents=True, exist_ok=True)
    lock_file(root).write_text(json.dumps({
        "supervisor": supervisor, "target": target, "session_id": session,
        "acquired_at": now, "refreshed_at": now}))


def edit_state(root, change):
    """Rewrite the state file through `change(data)`; returns the new text."""
    data = state(root)
    change(data)
    state_file(root).write_text(json.dumps(data))
    return state_file(root).read_text()


def no_jq_path(tmp_path_factory):
    """A PATH with the tools the hook needs, but no jq."""
    bindir = tmp_path_factory.mktemp("nojq")
    for tool in ("bash", "git", "date", "cat", "dirname", "grep", "mkdir", "rmdir", "stat", "sleep"):
        found = shutil.which(tool)
        if found:
            (bindir / tool).symlink_to(found)
    return str(bindir)


def hold_mutex(path):
    """A process holding the helpers' flock(2) mutex on `path` until killed."""
    holder = subprocess.Popen(
        ["bash", "-c", f'source "{HOOKS}/lib/flock.sh"; _r5_flock "$1" t; echo held; exec sleep 60',
         "_", str(path)], stdout=subprocess.PIPE, text=True)
    assert holder.stdout.readline().strip() == "held"
    return holder


LEASE = 900  # the lock's lease (lib/lease.sh)
PAST_LEASE = LEASE + 100  # older than the lock's lease


def start(root, bead="b1"):
    assert run(STATE, "init", bead, "base", "head", root=root).returncode == 0


def clear_pass(root, name):
    """Run one pass with a clean review, as the supervisor would."""
    run(STATE, "pass", name, root=root)
    hook(root)
    run(STATE, "findings", name, report(name, 0, 0, 0), root=root)
    assert run(STATE, "done", name, "0", root=root).returncode == 0


def age_lock(root, seconds, field="refreshed_at"):
    """Rewrite the lock as if `field` were set `seconds` ago."""
    data = json.loads(lock_file(root).read_text())
    old = datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=seconds)
    data[field] = old.strftime("%Y-%m-%dT%H:%M:%SZ")
    lock_file(root).write_text(json.dumps(data))


TTL = 86400  # the lock's absolute TTL (lib/lease.sh)
PAST_TTL = TTL + 100  # older than the lock's absolute TTL

# r5-state.sh's refusal on a malformed state file. Matched in full, not as
# "malformed": the message carries the state file's path, and a test's
# tmp_path holds its test name, which may contain that word.
MALFORMED = "is unreadable or malformed, so nothing can proceed on it"


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
    other = run(LOCK, "acquire", "r5-supervise", "b2", root=r5root, session="S2")
    assert other.returncode == 2 and "held by another supervisor" in other.stderr
    nested = run(LOCK, "acquire", "epic-executor", "e1", root=r5root)
    assert nested.returncode == 2 and "don't nest" in nested.stderr
    assert json.loads(lock_file(r5root).read_text())["supervisor"] == "r5-supervise"


def test_the_same_session_cannot_retarget_a_run_in_flight(r5root):
    assert run(LOCK, "acquire", "r5-supervise", "b1", root=r5root).returncode == 0
    start(r5root)
    assert run(LOCK, "acquire", "r5-supervise", "b1", root=r5root).returncode == 0  # idempotent
    other = run(LOCK, "acquire", "r5-supervise", "b2", root=r5root)
    assert other.returncode == 2 and "b1" in other.stderr and "release" in other.stderr
    assert json.loads(lock_file(r5root).read_text())["target"] == "b1"


def test_a_lock_left_from_a_finished_run_takes_the_next_bead(r5root):
    run(LOCK, "acquire", "r5-supervise", "b1", root=r5root)
    start(r5root)
    run(STATE, "finish", "aborted", root=r5root)  # finished, but not released
    assert run(LOCK, "acquire", "r5-supervise", "b2", root=r5root).returncode == 0
    assert json.loads(lock_file(r5root).read_text())["target"] == "b2"


def test_a_malformed_state_is_a_run_in_flight_for_the_lock(r5root):
    run(LOCK, "acquire", "r5-supervise", "b1", root=r5root)
    start(r5root)
    age_lock(r5root, 3600, field="acquired_at")
    before = json.loads(lock_file(r5root).read_text())["acquired_at"]
    (r5root / ".claude" / "r5-state.json").write_text("{trunc")
    run(LOCK, "acquire", "r5-supervise", "b1", root=r5root)
    assert json.loads(lock_file(r5root).read_text())["acquired_at"] == before  # not restamped
    assert run(LOCK, "acquire", "r5-supervise", "b2", root=r5root).returncode == 2


def test_only_the_holder_refreshes_or_releases(r5root):
    assert run(LOCK, "acquire", "r5-supervise", "b1", root=r5root).returncode == 0
    refresh = run(LOCK, "refresh", "r5-supervise", root=r5root, session="S2")
    assert refresh.returncode == 1 and "refresh: lock held by r5-supervise/S1" in refresh.stderr
    release = run(LOCK, "release", "r5-supervise", root=r5root, session="S2")
    assert release.returncode == 1 and "refusing to drop" in release.stderr
    assert lock_file(r5root).exists()
    assert run(LOCK, "release", "r5-supervise", root=r5root).returncode == 0
    assert not lock_file(r5root).exists()


@pytest.mark.parametrize(("op", "fragment"), [
    ("refresh", "refresh: lock held by r5-supervise/S1, not epic-executor/S1"),
    ("release", "refusing to drop"),
    ("renew", "does not hold the lock for epic-executor")])
def test_the_holder_s_session_under_another_supervisor_name_cannot_touch_the_lock(
        r5root, op, fragment):
    assert run(LOCK, "acquire", "r5-supervise", "b1", root=r5root).returncode == 0
    age_lock(r5root, 60)
    age_lock(r5root, 120, field="acquired_at")
    before = lock_file(r5root).read_text()
    refused = run(LOCK, op, "epic-executor", root=r5root)
    assert refused.returncode == 1 and fragment in refused.stderr
    assert lock_file(r5root).read_text() == before


@pytest.mark.parametrize(("age", "rc", "fragment"), [
    (LEASE - 10, 2, "held by another supervisor"), (LEASE + 10, 0, "stale lease")])
def test_another_session_reclaims_only_once_the_lease_has_run_out(r5root, age, rc, fragment):
    run(LOCK, "acquire", "r5-supervise", "b1", root=r5root, session="HOLDER")
    age_lock(r5root, age)
    other = run(LOCK, "acquire", "r5-supervise", "b2", root=r5root, session="S2")
    assert other.returncode == rc and fragment in other.stderr
    assert json.loads(lock_file(r5root).read_text())["session_id"] == ("S2" if rc == 0 else "HOLDER")


@pytest.mark.parametrize(("age", "rc", "fragment"), [
    (TTL - 100, 2, "held by another supervisor"), (TTL + 100, 0, "absolute TTL exceeded")])
def test_another_session_reclaims_a_freshly_refreshed_lock_only_past_its_ttl(
        r5root, age, rc, fragment):
    run(LOCK, "acquire", "r5-supervise", "b1", root=r5root, session="HOLDER")
    age_lock(r5root, age, field="acquired_at")
    other = run(LOCK, "acquire", "r5-supervise", "b2", root=r5root, session="S2")
    assert other.returncode == rc and fragment in other.stderr
    assert json.loads(lock_file(r5root).read_text())["session_id"] == ("S2" if rc == 0 else "HOLDER")


@pytest.mark.parametrize(("age", "rc", "fragment"), [
    (TTL - 100, 0, ""), (TTL + 100, 4, "past its 86400s limit")])
def test_the_holder_refreshes_up_to_the_ttl_and_not_past_it(r5root, age, rc, fragment):
    run(LOCK, "acquire", "r5-supervise", "b1", root=r5root)
    start(r5root)  # a run in flight: the TTL is not restamped
    age_lock(r5root, age, field="acquired_at")
    refreshed = run(LOCK, "refresh", "r5-supervise", root=r5root)
    assert refreshed.returncode == rc and fragment in refreshed.stderr


@pytest.mark.parametrize(("op", "args", "rc", "fragment"), [
    ("acquire", ("b1",), 2, "held by another supervisor"),
    ("refresh", (), 1, "held by r5-supervise/S2"),
    ("release", (), 1, "held by r5-supervise/S2"),
    ("renew", (), 1, "does not hold the lock for r5-supervise")])
def test_a_lock_call_waits_for_the_mutex_and_then_sees_the_lock_written_meanwhile(
        r5root, op, args, rc, fragment):
    """Only the lock's mutex protects this: S1's call is made while another
    process holds the mutex, and S2 takes the lock before the mutex is let
    go. Without the mutex, S1 would read its own lock and succeed."""
    assert run(LOCK, "acquire", "r5-supervise", "b1", root=r5root).returncode == 0
    holder = hold_mutex(r5root / ".claude" / "r5-supervisor.lock.flock")
    blocked = None
    try:
        blocked = subprocess.Popen(["bash", str(LOCK), op, "r5-supervise", *args],
                                   env=env_for(r5root, "S1"), cwd=r5root, text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        with pytest.raises(subprocess.TimeoutExpired):
            blocked.wait(timeout=1)  # waits on the held mutex
        write_lock(r5root, "S2")
        holder.kill()
        _, err = blocked.communicate(timeout=10)
        assert blocked.returncode == rc and fragment in err, err
    finally:
        for proc in (holder, blocked):
            if proc is not None:
                proc.kill()
                proc.wait()
        holder.stdout.close()
    assert json.loads(lock_file(r5root).read_text())["session_id"] == "S2"


# ---------------------------------------------------------------- the hook: another session's lock (layer A)

def test_another_sessions_live_lock_blocks_a_dispatch(r5root):
    run(LOCK, "acquire", "r5-supervise", "b1", root=r5root, session="HOLDER")
    refused = hook(r5root, session="S2")
    assert refused.returncode == 2 and "held by another session" in refused.stderr
    assert hook(r5root, session="HOLDER").returncode == 0


def test_claude_session_id_is_the_fallback_session_variable(r5root):
    env = {"CLAUDE_SESSION_ID": "FALLBACK"}
    assert run(LOCK, "acquire", "r5-supervise", "b1", root=r5root, session=None,
               env_extra=env).returncode == 0
    assert json.loads(lock_file(r5root).read_text())["session_id"] == "FALLBACK"


def test_claude_code_session_id_wins_over_the_fallback(r5root):
    env = {"CLAUDE_SESSION_ID": "FALLBACK"}
    assert run(LOCK, "acquire", "r5-supervise", "b1", root=r5root, session="PRIMARY",
               env_extra=env).returncode == 0
    assert json.loads(lock_file(r5root).read_text())["session_id"] == "PRIMARY"


def test_with_no_session_in_the_environment_the_payload_s_session_id_is_honoured(r5root):
    run(LOCK, "acquire", "r5-supervise", "b1", root=r5root, session="HOLDER")
    refused = run(HOOK, root=r5root, session=None, stdin=payload("S2"))
    assert refused.returncode == 2 and "held by another session" in refused.stderr
    assert "from session S2" in refused.stderr
    assert run(HOOK, root=r5root, session=None, stdin=payload("HOLDER")).returncode == 0
    run(STATE, "init", "b1", "x", "y", root=r5root, session="HOLDER")
    run(STATE, "pass", "correctness", root=r5root, session="HOLDER")
    assert run(HOOK, root=r5root, session=None, stdin=payload("HOLDER")).returncode == 0
    assert events(r5root)[-1] == "reviewer_dispatched"  # claimed as the owner's dispatch


def test_a_stale_lock_no_longer_blocks_other_sessions(r5root):
    run(LOCK, "acquire", "r5-supervise", "b1", root=r5root, session="HOLDER")
    age_lock(r5root, PAST_LEASE)
    assert hook(r5root, session="S2").returncode == 0


@pytest.mark.parametrize(("field", "age", "rc"), [
    ("refreshed_at", LEASE - 10, 2), ("refreshed_at", LEASE + 10, 0),
    ("acquired_at", TTL - 100, 2), ("acquired_at", TTL + 100, 0),
    ("acquired_at", -600, 0)])
def test_another_session_s_lock_blocks_only_while_its_lease_and_ttl_hold(r5root, field, age, rc):
    run(LOCK, "acquire", "r5-supervise", "b1", root=r5root, session="HOLDER")
    age_lock(r5root, age, field=field)
    result = hook(r5root, session="S2")
    assert result.returncode == rc
    assert ("held by another session" in result.stderr) == (rc == 2), result.stderr


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
    recorded = state(r5root)["audit_trail"][-1]
    assert recorded["pass"] == "correctness"
    assert recorded["subagent_type"] == "pr-review-toolkit:code-reviewer"
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
    assert run(STATE, "findings", "correctness", report("correctness", 0, 2, 0), "FIX", root=r5root).returncode == 0
    assert hook(r5root, subagent="feature-dev:code-architect").returncode == 0  # the fix agent
    assert events(r5root)[-1] == "fix_dispatched"
    assert state(r5root)["audit_trail"][-1]["subagent_type"] == "feature-dev:code-architect"
    second = hook(r5root)  # a second, while the first is open
    assert second.returncode == 2 and "a fix agent is open" in second.stderr
    early = run(STATE, "request", "correctness", "2", root=r5root)
    assert early.returncode == 2 and "a fix agent is still open" in early.stderr
    run(STATE, "fixed", "correctness", root=r5root)
    assert run(STATE, "request", "correctness", "2", root=r5root).returncode == 0
    assert events(r5root).count("reviewer_dispatched") == 1


def test_a_re_dispatch_is_requested_then_claimed(r5root):
    start(r5root)
    run(STATE, "pass", "correctness", root=r5root)
    hook(r5root)
    run(STATE, "findings", "correctness", report("correctness", 0, 1, 0), "FIX", root=r5root)
    assert run(STATE, "request", "correctness", "2", root=r5root).returncode == 0
    assert hook(r5root).returncode == 0
    second = hook(r5root)
    assert second.returncode == 2 and "reviewer is in flight" in second.stderr
    assert events(r5root).count("reviewer_dispatched") == 2


def test_a_stalled_reviewer_is_abandoned_then_requested_again(r5root):
    start(r5root)
    run(STATE, "pass", "correctness", root=r5root)
    hook(r5root)
    early = run(STATE, "request", "correctness", "2", root=r5root)
    assert early.returncode == 2 and "still in flight" in early.stderr
    other = run(STATE, "abandon", "clarity", "stalled", root=r5root)
    assert other.returncode == 2 and "clarity is not the current pass" in other.stderr
    assert run(STATE, "abandon", "correctness", "stalled", root=r5root).returncode == 0
    waiting = hook(r5root)  # nothing until the reviewer is requested again
    assert waiting.returncode == 2 and "was abandoned" in waiting.stderr
    assert run(STATE, "request", "correctness", "2", root=r5root).returncode == 0
    assert hook(r5root).returncode == 0
    assert events(r5root)[-3:] == ["reviewer_abandoned", "reviewer_requested", "reviewer_dispatched"]


def test_a_bookkeeping_event_never_reopens_a_window(r5root):
    start(r5root)
    run(STATE, "pass", "correctness", root=r5root)
    hook(r5root)
    run(STATE, "event", "session_restarted", root=r5root)
    still = hook(r5root)
    assert still.returncode == 2 and "reviewer is in flight" in still.stderr
    for name in ("reviewer_abandoned", "findings_received"):
        forged = run(STATE, "event", name, root=r5root)
        assert forged.returncode == 1 and "is a window event" in forged.stderr, name
    assert events(r5root)[-1] == "session_restarted"


def test_fixed_only_follows_the_current_pass_s_findings(r5root):
    start(r5root)
    run(STATE, "pass", "correctness", root=r5root)
    requested = run(STATE, "fixed", "correctness", root=r5root)
    assert requested.returncode == 2 and "fixed cannot follow pass_started" in requested.stderr
    hook(r5root)
    running = run(STATE, "fixed", "correctness", root=r5root)
    assert running.returncode == 2 and "fixed cannot follow reviewer_dispatched" in running.stderr
    assert hook(r5root).returncode == 2
    assert events(r5root)[-1] == "reviewer_dispatched"
    run(STATE, "findings", "correctness", report("correctness", 0, 1, 0), "FIX", root=r5root)
    assert run(STATE, "fixed", "correctness", '{"pass": "clarity"}', root=r5root).returncode == 0
    assert state(r5root)["audit_trail"][-1]["pass"] == "correctness"  # not the extra's


def test_a_fix_may_follow_a_dirty_tree_but_a_dirty_tree_only_follows_findings(r5root):
    start(r5root)
    run(STATE, "pass", "correctness", root=r5root)
    hook(r5root)
    run(STATE, "findings", "correctness", report("correctness", 0, 0, 0), root=r5root)
    assert run(STATE, "tree-dirty", "correctness", root=r5root).returncode == 0
    assert run(STATE, "fixed", "correctness", root=r5root).returncode == 0
    assert events(r5root)[-2:] == ["reviewer_left_tree_dirty", "fixes_applied"]
    late = run(STATE, "tree-dirty", "correctness", root=r5root)
    assert late.returncode == 2 and "tree-dirty cannot follow fixes_applied" in late.stderr
    assert events(r5root)[-1] == "fixes_applied"


def test_a_dispatch_between_passes_goes_through_and_is_not_recorded(r5root):
    start(r5root)
    clear_pass(r5root, "correctness")
    before = state_file(r5root).read_text()
    assert hook(r5root).returncode == 0
    assert state_file(r5root).read_text() == before


def test_a_dispatch_against_a_finished_but_unarchived_run_goes_through_unrecorded(r5root):
    start(r5root)
    run(STATE, "pass", "correctness", root=r5root)  # a reviewer is requested
    finished_but_not_archived(r5root)
    before = state_file(r5root).read_text()
    assert hook(r5root).returncode == 0
    assert state_file(r5root).read_text() == before


def test_open_window_names_an_open_reviewer_and_nothing_once_the_run_is_finished(r5root):
    start(r5root)
    run(STATE, "pass", "correctness", root=r5root)
    hook(r5root)
    assert run(STATE, "open-window", root=r5root).stdout == "S1 b1 reviewer\n"
    finished_but_not_archived(r5root)
    finished = run(STATE, "open-window", root=r5root)
    assert finished.returncode == 0 and finished.stdout == ""


# ---------------------------------------------------------------- run ownership across sessions

def test_another_sessions_crashed_run_stops_blocking_once_its_lease_runs_out(r5root):
    run(LOCK, "acquire", "r5-supervise", "b1", root=r5root, session="S1")
    start(r5root)
    run(STATE, "pass", "correctness", root=r5root)
    live = hook(r5root, session="S2")  # S1's lease is live
    assert live.returncode == 2 and "held by another session" in live.stderr
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
    assert "adopt it first, then r5-state.sh finish aborted" in refused.stderr
    run(LOCK, "acquire", "r5-supervise", "b1", root=r5root, session="S3")
    age_lock(r5root, PAST_LEASE)  # a third session's lapsed lock is not the owner's
    still = hook(r5root, session="S2")
    assert still.returncode == 2 and "belongs to session S1" in still.stderr
    assert events(r5root)[-1] == "pass_started"


def test_a_long_review_keeps_the_lock_from_another_supervisor(r5root):
    run(LOCK, "acquire", "r5-supervise", "b1", root=r5root, session="S1")
    start(r5root)
    run(STATE, "pass", "correctness", root=r5root)
    hook(r5root)  # S1's reviewer runs past the lease
    age_lock(r5root, PAST_LEASE)
    other = run(LOCK, "acquire", "epic-executor", "e1", root=r5root, session="S2")
    assert other.returncode == 2 and "Not reclaiming" in other.stderr
    assert run(LOCK, "acquire", "r5-supervise", "b2", root=r5root, session="S2").returncode == 2
    assert json.loads(lock_file(r5root).read_text())["session_id"] == "S1"
    assert run(LOCK, "acquire", "r5-supervise", "b1", root=r5root, session="S2").returncode == 0


@pytest.mark.parametrize(("age", "rc", "fragment"), [
    (TTL - 100, 2, "Not reclaiming"), (TTL + 100, 0, "stale lease")])
def test_the_long_review_guard_holds_until_the_ttl_and_not_past_it(r5root, age, rc, fragment):
    run(LOCK, "acquire", "r5-supervise", "b1", root=r5root, session="S1")
    start(r5root)
    run(STATE, "pass", "correctness", root=r5root)
    hook(r5root)  # S1's reviewer is in flight
    age_lock(r5root, PAST_LEASE)
    age_lock(r5root, age, field="acquired_at")
    other = run(LOCK, "acquire", "epic-executor", "e1", root=r5root, session="S2")
    assert other.returncode == rc and fragment in other.stderr
    assert json.loads(lock_file(r5root).read_text())["session_id"] == ("S2" if rc == 0 else "S1")


def test_the_ttl_counts_from_the_run_s_start_and_refresh_fails_past_it(r5root):
    run(LOCK, "acquire", "r5-supervise", "b1", root=r5root)
    age_lock(r5root, PAST_TTL, field="acquired_at")
    run(LOCK, "acquire", "r5-supervise", "b1", root=r5root)  # no run in flight: restamped
    assert run(LOCK, "refresh", "r5-supervise", root=r5root).returncode == 0
    start(r5root)
    age_lock(r5root, PAST_TTL, field="acquired_at")
    run(LOCK, "acquire", "r5-supervise", "b1", root=r5root)  # a run in flight: not restamped
    late = run(LOCK, "refresh", "r5-supervise", root=r5root)
    assert late.returncode == 4 and "renew" in late.stderr
    assert run(LOCK, "renew", "r5-supervise", root=r5root, session="S2").returncode == 1
    assert run(LOCK, "renew", "r5-supervise", root=r5root).returncode == 0  # with the user's OK
    assert run(LOCK, "refresh", "r5-supervise", root=r5root).returncode == 0
    assert state(r5root)["status"] == "in_progress"


def test_a_lock_acquired_in_the_future_fails_refresh_and_can_be_reclaimed(r5root):
    run(LOCK, "acquire", "r5-supervise", "b1", root=r5root, session="S1")
    age_lock(r5root, -600, field="acquired_at")
    assert run(LOCK, "refresh", "r5-supervise", root=r5root, session="S1").returncode == 4
    assert run(LOCK, "acquire", "r5-supervise", "b2", root=r5root, session="S2").returncode == 0


def test_the_long_review_guard_covers_a_fix_agent_and_an_unknown_owner(r5root):
    run(LOCK, "acquire", "r5-supervise", "b1", root=r5root, session="S1")
    run(STATE, "init", "b1", "x", "y", root=r5root, session="")  # an owner never recorded
    run(STATE, "pass", "correctness", root=r5root, session="")
    hook(r5root, session="")
    run(STATE, "findings", "correctness", report("correctness", 0, 1, 0), root=r5root, session="")
    hook(r5root, session="")  # a fix agent
    assert events(r5root)[-1] == "fix_dispatched"
    age_lock(r5root, PAST_LEASE)
    other = run(LOCK, "acquire", "epic-executor", "e1", root=r5root, session="S2")
    assert other.returncode == 2 and "Not reclaiming" in other.stderr and "fix-agent" in other.stderr


@pytest.mark.parametrize("content", [
    "{trunc", "", "[]", '{"status": "in_progress"}', "TRAIL"])
def test_an_unreadable_run_state_stops_a_reclaim(r5root, content):
    run(LOCK, "acquire", "r5-supervise", "b1", root=r5root, session="S1")
    start(r5root)
    path = r5root / ".claude" / "r5-state.json"
    if content == "TRAIL":  # the right type, but its entries are not events
        data = json.loads(path.read_text())
        data["audit_trail"] = [1, 2]
        content = json.dumps(data)
    path.write_text(content)
    window = run(STATE, "open-window", root=r5root, session="S2")
    assert window.returncode == 2 and MALFORMED in window.stderr and window.stdout == ""
    age_lock(r5root, PAST_LEASE)
    refused = run(LOCK, "acquire", "epic-executor", "e1", root=r5root, session="S2")
    assert refused.returncode == 2 and "cannot be read" in refused.stderr


def test_resuming_a_crashed_long_review_then_ending_it(r5root):
    run(LOCK, "acquire", "r5-supervise", "b1", root=r5root, session="S1")
    start(r5root)
    run(STATE, "pass", "correctness", root=r5root)
    hook(r5root)
    age_lock(r5root, PAST_LEASE)
    assert run(LOCK, "acquire", "r5-supervise", "b1", root=r5root, session="S2").returncode == 0
    assert run(STATE, "adopt", root=r5root, session="S2").returncode == 0
    assert run(STATE, "finish", "aborted", root=r5root, session="S2").returncode == 0
    assert run(LOCK, "release", "r5-supervise", root=r5root, session="S2").returncode == 0
    assert run(LOCK, "acquire", "epic-executor", "e1", root=r5root, session="S3").returncode == 0


def test_a_lock_from_the_future_counts_as_expired(r5root):
    run(LOCK, "acquire", "r5-supervise", "b1", root=r5root, session="HOLDER")
    age_lock(r5root, -3600)
    assert hook(r5root, session="S2").returncode == 0
    assert run(LOCK, "acquire", "r5-supervise", "b2", root=r5root, session="S2").returncode == 0


def test_a_new_session_adopts_a_run_only_while_holding_the_lock(r5root):
    run(LOCK, "acquire", "r5-supervise", "b1", root=r5root, session="S1")
    start(r5root)
    run(STATE, "pass", "correctness", root=r5root)
    hook(r5root)
    age_lock(r5root, PAST_LEASE)
    early = run(STATE, "adopt", root=r5root, session="S2")  # no lock yet
    assert early.returncode == 2 and "does not hold the R5 lock" in early.stderr
    assert run(LOCK, "acquire", "r5-supervise", "b1", root=r5root, session="S2").returncode == 0
    assert run(STATE, "adopt", root=r5root, session="S2").returncode == 0
    assert state(r5root)["session_id"] == "S2"
    inherited = hook(r5root, session="S2")  # S1's reviewer is still recorded in flight
    assert inherited.returncode == 2 and "reviewer is in flight" in inherited.stderr
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
    for args in (("finish", "aborted"), ("findings", "correctness", report("correctness")),
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
    refused = run(STATE, "pass", "clarity", root=r5root)
    assert refused.returncode == 2 and "a reviewer is still in flight" in refused.stderr


def test_passes_run_in_order_and_one_at_a_time(r5root):
    start(r5root)
    for name in ("clarity", "excellence"):
        early = run(STATE, "pass", name, root=r5root)
        assert early.returncode == 2 and f"the next pass is correctness, not {name}" in early.stderr
    assert run(STATE, "pass", "correctness", root=r5root).returncode == 0
    again = run(STATE, "pass", "correctness", root=r5root)
    assert again.returncode == 2 and "correctness is still the current pass" in again.stderr
    hook(r5root)
    run(STATE, "findings", "correctness", report("correctness", 0, 0, 0), "PASS", root=r5root)
    undone = run(STATE, "pass", "clarity", root=r5root)  # correctness not done
    assert undone.returncode == 2 and "correctness is still the current pass" in undone.stderr
    run(STATE, "done", "correctness", "0", root=r5root)
    skipped = run(STATE, "pass", "edge-cases", root=r5root)
    assert skipped.returncode == 2 and "the next pass is clarity, not edge-cases" in skipped.stderr
    assert run(STATE, "pass", "clarity", root=r5root).returncode == 0


def test_a_pass_is_done_only_when_it_is_current_and_its_last_findings_are_clean(r5root):
    start(r5root)
    run(STATE, "pass", "correctness", root=r5root)
    hook(r5root)
    unreported = run(STATE, "done", "correctness", "0", root=r5root)  # no findings
    assert unreported.returncode == 2 and "followed by later activity" in unreported.stderr
    run(STATE, "findings", "correctness", report("correctness", 0, 1, 0), "FIX", root=r5root)
    dirty = run(STATE, "done", "correctness", "1", root=r5root)
    assert dirty.returncode == 2 and "its findings are not clean" in dirty.stderr
    other = run(STATE, "done", "clarity", "1", root=r5root)
    assert other.returncode == 2 and "clarity is not the current pass" in other.stderr
    run(STATE, "request", "correctness", "2", root=r5root)
    hook(r5root)
    # Silence never closes: the re-review closes the IMPORTANT explicitly.
    fp = state(r5root)["audit_trail"][-3]["open_set"][0]["fingerprint"]
    closure = {**finding("correctness", "IMPORTANT", 99), "resolves": fp}
    run(STATE, "findings", "correctness",
        report("correctness", findings=[closure] + [finding("correctness", "MINOR", 20 + n) for n in range(3)]),
        "PASS", root=r5root)
    assert run(STATE, "done", "correctness", "1", '{"name": "clarity", "fix_iterations": 9}',
               root=r5root).returncode == 0
    assert state(r5root)["current_pass"] is None
    assert [(p["name"], p["fix_iterations"]) for p in state(r5root)["completed_passes"]] \
        == [("correctness", 1)]  # not the extra's


def test_a_critical_finding_alone_keeps_a_pass_from_being_done(r5root):
    start(r5root)
    run(STATE, "pass", "correctness", root=r5root)
    hook(r5root)
    run(STATE, "findings", "correctness", report("correctness", 1, 0, 0), "FIX", root=r5root)
    refused = run(STATE, "done", "correctness", "0", root=r5root)
    assert refused.returncode == 2 and "its findings are not clean" in refused.stderr
    assert state(r5root)["completed_passes"] == []


def test_a_pass_cannot_be_done_while_its_re_review_is_requested_or_running(r5root):
    start(r5root)
    run(STATE, "pass", "correctness", root=r5root)
    hook(r5root)
    run(STATE, "findings", "correctness", report("correctness", 0, 0, 1), "PASS", root=r5root)
    run(STATE, "fixed", "correctness", root=r5root)
    run(STATE, "request", "correctness", "2", root=r5root)
    for when in ("requested", "running"):
        refused = run(STATE, "done", "correctness", "1", root=r5root)
        assert refused.returncode == 2 and "followed by later activity" in refused.stderr, when
        if when == "requested":
            hook(r5root)
    nxt = run(STATE, "pass", "clarity", root=r5root)
    assert nxt.returncode == 2 and "still in flight" in nxt.stderr


def test_findings_need_the_current_pass_and_a_dispatched_reviewer(r5root):
    start(r5root)
    run(STATE, "pass", "correctness", root=r5root)
    early = run(STATE, "findings", "correctness", report("correctness", 0, 0, 0), root=r5root)
    assert early.returncode == 2 and "no reviewer is in flight" in early.stderr
    hook(r5root)
    other = run(STATE, "findings", "clarity", report("clarity", 0, 0, 0), root=r5root)
    assert other.returncode == 2 and "clarity is not the current pass" in other.stderr
    advisory = run(STATE, "advisory", report("excellence", 0, 0, 0), "PASS", root=r5root)
    assert advisory.returncode == 2 and "excellence is not the current pass" in advisory.stderr
    assert run(STATE, "findings", "correctness", report("correctness", 0, 0, 0), root=r5root).returncode == 0


def test_a_dirty_tree_after_provenance_fails_the_pass_until_it_is_re_reviewed(r5root):
    start(r5root)
    for name in ("correctness", "clarity", "edge-cases"):
        clear_pass(r5root, name)
    run(STATE, "pass", "provenance", root=r5root)
    hook(r5root)
    run(STATE, "findings", "provenance", report("provenance", 0, 0, 0), root=r5root)
    assert run(STATE, "tree-dirty", "provenance", root=r5root).returncode == 0
    refused = run(STATE, "done", "provenance", "0", root=r5root)
    assert refused.returncode == 2 and "followed by later activity" in refused.stderr
    assert run(STATE, "request", "provenance", "2", root=r5root).returncode == 0
    assert hook(r5root).returncode == 0
    assert events(r5root)[-1] == "reviewer_dispatched"


def test_finish_completed_needs_every_pass_and_the_advisory_read(r5root):
    start(r5root)
    run(STATE, "pass", "correctness", root=r5root)
    refused = run(STATE, "finish", "completed", root=r5root)
    assert refused.returncode == 2 and "clarity" in refused.stderr
    hook(r5root)
    run(STATE, "findings", "correctness", report("correctness", 0, 0, 0), root=r5root)
    run(STATE, "done", "correctness", "0", root=r5root)
    for name in ("clarity", "edge-cases", "provenance"):
        clear_pass(r5root, name)
    unread = run(STATE, "finish", "completed", root=r5root)  # no advisory read
    assert unread.returncode == 2 and "the run is not complete: excellence" in unread.stderr
    run(STATE, "pass", "excellence", root=r5root)
    hook(r5root)
    for args in (("findings", "excellence", report("excellence")), ("done", "excellence", "0")):
        bad = run(STATE, *args, root=r5root)
        assert bad.returncode == 1 and "excellence is not a blocking pass" in bad.stderr, args
    assert run(STATE, "advisory", report("excellence", 0, 0, 1), "PASS", root=r5root).returncode == 0
    assert run(STATE, "finish", "completed", root=r5root).returncode == 0


def test_finish_completed_is_refused_while_a_second_excellence_read_is_in_flight(r5root):
    start(r5root)
    for name in ("correctness", "clarity", "edge-cases", "provenance"):
        clear_pass(r5root, name)
    advisory_read(r5root)
    run(STATE, "pass", "excellence", root=r5root)  # a second read, after a touch-up
    hook(r5root)
    refused = run(STATE, "finish", "completed", root=r5root)
    assert refused.returncode == 2 and "excellence is still current" in refused.stderr
    assert state(r5root)["status"] == "in_progress"
    assert not list((r5root / ".claude").glob("r5-state-b1-completed-*.json"))


def test_reopen_runs_a_done_pass_and_every_later_one_again(r5root):
    start(r5root)
    clear_pass(r5root, "correctness")
    clear_pass(r5root, "clarity")
    undone = run(STATE, "reopen", "edge-cases", root=r5root)
    assert undone.returncode == 2 and "nothing to reopen" in undone.stderr
    assert run(STATE, "reopen", "correctness", root=r5root).returncode == 0
    assert state(r5root)["completed_passes"] == []
    assert state(r5root)["audit_trail"][-1]["dropped"] == ["correctness", "clarity"]
    assert run(STATE, "pass", "clarity", root=r5root).returncode == 2
    clear_pass(r5root, "correctness")
    run(STATE, "pass", "clarity", root=r5root)
    hook(r5root)
    busy = run(STATE, "reopen", "correctness", root=r5root)  # a reviewer in flight
    assert busy.returncode == 2 and "still in flight" in busy.stderr
    run(STATE, "findings", "clarity", report("clarity", 0, 1, 0), root=r5root)
    hook(r5root)  # a fix agent
    assert events(r5root)[-1] == "fix_dispatched"
    fixing = run(STATE, "reopen", "correctness", root=r5root)
    assert fixing.returncode == 2 and "a fix agent is still open" in fixing.stderr


def advisory_read(root):
    run(STATE, "pass", "excellence", root=root)
    hook(root)
    assert run(STATE, "advisory", report("excellence", 0, 0, 0), "PASS", root=root).returncode == 0


def test_reopen_drops_the_advisory_read_so_the_run_needs_a_new_one(r5root):
    start(r5root)
    for name in ("correctness", "clarity", "edge-cases", "provenance"):
        clear_pass(r5root, name)
    advisory_read(r5root)
    assert run(STATE, "reopen", "clarity", root=r5root).returncode == 0
    assert state(r5root)["advisory_results"] == []
    assert [p["name"] for p in state(r5root)["completed_passes"]] == ["correctness"]
    for name in ("clarity", "edge-cases", "provenance"):
        clear_pass(r5root, name)
    stale = run(STATE, "finish", "completed", root=r5root)
    assert stale.returncode == 2 and "the run is not complete: excellence" in stale.stderr
    advisory_read(r5root)
    assert run(STATE, "finish", "completed", root=r5root).returncode == 0


def test_reopen_while_a_later_pass_is_current_clears_the_current_pass(r5root):
    start(r5root)
    clear_pass(r5root, "correctness")
    run(STATE, "pass", "clarity", root=r5root)
    hook(r5root)
    run(STATE, "findings", "clarity", report("clarity", 0, 0, 0), root=r5root)  # no reviewer in flight
    assert run(STATE, "reopen", "correctness", root=r5root).returncode == 0
    assert state(r5root)["current_pass"] is None
    assert run(STATE, "pass", "correctness", root=r5root).returncode == 0


def test_the_excellence_in_flight_refusal_names_advisory(r5root):
    start(r5root)
    for name in ("correctness", "clarity", "edge-cases", "provenance"):
        clear_pass(r5root, name)
    run(STATE, "pass", "excellence", root=r5root)
    hook(r5root)
    refused = hook(r5root)
    assert refused.returncode == 2 and "`advisory ...`" in refused.stderr


@pytest.mark.parametrize("content", ["", "[]", '{"status": "in_progress", "audit_trail": "x"}',
                                     "TRUNCATE"])
def test_a_malformed_state_file_refuses_every_dispatch_and_every_change(r5root, content):
    start(r5root)
    run(STATE, "pass", "correctness", root=r5root)
    hook(r5root)
    path = r5root / ".claude" / "r5-state.json"
    path.write_text(path.read_text()[:200] if content == "TRUNCATE" else content)
    refused = hook(r5root)
    assert refused.returncode == 2 and MALFORMED in refused.stderr
    for args in (("finish", "aborted"), ("init", "b2", "x", "y")):
        change = run(STATE, *args, root=r5root)
        assert change.returncode == 2 and MALFORMED in change.stderr, args


@pytest.mark.parametrize("change", [
    lambda d: d.pop("status"),
    lambda d: d.update(passes=None),
    lambda d: d.update(completed_passes="x"),
    lambda d: d.pop("advisory_results"),
    lambda d: d.update(audit_trail=[1, 2]),
    lambda d: d["audit_trail"].append({"ts": "2026-11-03T00:00:00Z"}),
    lambda d: d["audit_trail"].append({"ts": "2026-11-03T00:00:00Z", "event": 3}),
], ids=["status-missing", "passes-null", "completed-passes-a-string", "advisory-results-missing",
        "trail-of-non-objects", "trail-entry-without-event", "trail-entry-with-a-number-event"])
def test_a_state_of_the_right_type_but_a_broken_clause_refuses_and_records_nothing(
        r5root, change):
    start(r5root)
    run(STATE, "pass", "correctness", root=r5root)  # a valid file would let one reviewer through
    before = edit_state(r5root, change)
    refused = hook(r5root)
    assert refused.returncode == 2 and MALFORMED in refused.stderr
    assert state_file(r5root).read_text() == before


def test_a_bead_id_that_cannot_name_the_archive_is_refused(r5root):
    for bead in ("cm/1", "a b", "../x", ".hidden"):
        assert run(STATE, "init", bead, "x", "y", root=r5root).returncode == 1, bead
    assert not (r5root / ".claude" / "r5-state.json").exists()


def finished_but_not_archived(root):
    """The state a finish leaves when its archive move fails."""
    path = root / ".claude" / "r5-state.json"
    data = json.loads(path.read_text())
    data["status"] = "aborted"
    data["audit_trail"].append({"ts": "2026-11-03T00:00:00Z", "event": "supervisor_aborted"})
    path.write_text(json.dumps(data))


def archived_aborted_events(root, bead):
    archived = list((root / ".claude").glob(f"r5-state-{bead}-aborted-*.json"))
    assert len(archived) == 1
    return [e["event"] for e in json.loads(archived[0].read_text())["audit_trail"]]


def test_repeating_a_finish_whose_archive_failed_only_retries_the_move(r5root):
    start(r5root)
    finished_but_not_archived(r5root)
    assert run(STATE, "finish", "aborted", root=r5root).returncode == 0
    assert archived_aborted_events(r5root, "b1").count("supervisor_aborted") == 1


def test_init_archives_a_finished_but_unarchived_run_before_starting(r5root):
    start(r5root)
    finished_but_not_archived(r5root)
    assert run(STATE, "init", "b2", "x", "y", root=r5root).returncode == 0
    assert "supervisor_aborted" in archived_aborted_events(r5root, "b1")
    assert state(r5root)["target_bead"] == "b2"


def test_a_hand_edited_bead_or_status_cannot_name_a_path_outside_the_archive(r5root):
    start(r5root)
    finished_but_not_archived(r5root)
    path = r5root / ".claude" / "r5-state.json"
    data = json.loads(path.read_text())
    data["target_bead"], data["status"] = "../../escaped", "done/x"
    path.write_text(json.dumps(data))
    assert run(STATE, "finish", "aborted", root=r5root).returncode == 0
    archived = [p.name for p in (r5root / ".claude").glob("r5-state-*.json")]
    assert len(archived) == 1 and archived[0].startswith("r5-state-.._.._escaped-done_x-")
    assert not list(r5root.parent.glob("escaped*"))


def test_a_second_run_cannot_start_while_one_is_in_flight(r5root):
    start(r5root, "b1")
    second = run(STATE, "init", "b2", "x", "y", root=r5root)
    assert second.returncode == 2 and "a run is in flight on b1" in second.stderr
    assert state(r5root)["target_bead"] == "b1"


def test_finish_archives_the_trail_under_a_timestamp(r5root):
    start(r5root)
    assert run(STATE, "finish", "aborted", root=r5root).returncode == 0
    archived = list((r5root / ".claude").glob("r5-state-b1-aborted-*.json"))
    assert len(archived) == 1
    assert json.loads(archived[0].read_text())["status"] == "aborted"
    assert not (r5root / ".claude" / "r5-state.json").exists()


# ---------------------------------------------------------------- findings, rounds and the verdict (cm-70j)

def _load_findings():
    spec = importlib.util.spec_from_file_location("r5_findings", HOOKS / "lib" / "r5_findings.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


r5f = _load_findings()


def parse(findings, lens="correctness", floor="script", prose="Review.\n"):
    return r5f.parse_report(prose + "R5-FINDINGS: " + json.dumps(findings) + "\n", lens, floor)


def test_the_fingerprint_ignores_case_whitespace_line_references_and_shas():
    a = r5f.fingerprint("correctness", "src/a.py", "Off by one at line 41 in abc1234def")
    b = r5f.fingerprint("correctness", "src/a.py", "off  by one at line 7 in 9f8e7d6c5b")
    assert a == b and len(a) == 16 and int(a, 16) >= 0
    assert a != r5f.fingerprint("clarity", "src/a.py", "off by one at line 41 in abc1234def")
    assert a != r5f.fingerprint("correctness", "src/b.py", "off by one at line 41 in abc1234def")
    assert a != r5f.fingerprint("correctness", "src/a.py", "off by two at line 41 in abc1234def")


@pytest.mark.parametrize("one, other", [
    ("see lines 4-9 here", "see lines 12 - 30 here"),
    ("broken at a.py:42", "broken at a.py:7-9"),
    ("see L42 for it", "see L7 for it"),
])
def test_every_line_reference_form_is_removed(one, other):
    assert r5f.normalise_claim(one) == r5f.normalise_claim(other)


@pytest.mark.parametrize("one, other", [
    ("exits 2 instead of 3", "exits 4 instead of 1"),
    ("retries 10 times", "retries 3 times"),
    ("the defaced cache is stale", "the cache is stale"),
])
def test_other_numbers_and_hex_letter_words_keep_claims_apart(one, other):
    assert r5f.normalise_claim(one) != r5f.normalise_claim(other)


def test_the_parser_sets_fingerprint_and_status_itself_and_counts_by_severity():
    raw = [finding("correctness", "CRITICAL", 1, fingerprint="forged", status="fixed"),
           finding("correctness", "IMPORTANT", 2), finding("correctness", "MINOR", 3)]
    out = parse(raw)
    assert out["counts"] == {"critical": 1, "important": 1, "minor": 1}
    first = out["findings"][0]
    assert first["status"] == "open"
    assert first["fingerprint"] == r5f.fingerprint("correctness", "src/m1.py", "critical defect 1")
    assert first["regression"] is False and first["resolves"] is None


def test_a_finding_may_omit_lens_but_not_claim_another_reviewer_s():
    raw = finding("edge_cases", "MINOR")
    del raw["lens"]
    assert parse([raw], lens="edge_cases")["findings"][0]["lens"] == "edge_cases"
    with pytest.raises(r5f.ReportError, match="not this reviewer"):
        parse([finding("clarity", "MINOR")], lens="edge_cases")


@pytest.mark.parametrize("text, reason", [
    ("", "empty"),
    ("The review found nothing.\n", "does not start with R5-FINDINGS:"),
    ("R5-FINDINGS: [oops]\n", "not JSON"),
    ('R5-FINDINGS: {"a": 1}\n', "not a JSON array"),
    ('R5-FINDINGS: []\ntrailing prose\n', "does not start with R5-FINDINGS:"),
])
def test_a_report_off_the_contract_is_refused_with_its_reason(text, reason):
    with pytest.raises(r5f.ReportError, match=reason):
        r5f.parse_report(text, "correctness")


@pytest.mark.parametrize("change, reason", [
    ({"severity": "P1"}, "severity"),
    ({"start_line": 0}, "start_line"),
    ({"start_line": 9, "end_line": 3}, "start_line"),
    ({"end_line": True}, "start_line"),
    ({"path": ""}, "path"),
    ({"claim": "  "}, "claim"),
    ({"evidence": None}, "evidence"),
    ({"fix": 3}, "fix"),
    ({"regression": "yes"}, "regression"),
    ({"verification_level": "proven"}, "verification_level"),
])
def test_each_field_of_the_schema_is_checked(change, reason):
    with pytest.raises(r5f.ReportError, match=reason):
        parse([{**finding("correctness", "IMPORTANT"), **change}])


def prior(*findings_):
    """A previous round's open set: parsed findings, as r5-state.sh hands them on."""
    return parse(list(findings_))["open_set"]


def reparse(records, prior_open, lens="correctness"):
    return r5f.parse_report("R5-FINDINGS: " + json.dumps(records), lens, prior_open=prior_open)


def test_a_valid_closure_is_never_counted_whatever_its_severity():
    earlier = prior(finding("correctness", "CRITICAL", 1))
    fp = earlier[0]["fingerprint"]
    closure = {**finding("correctness", "CRITICAL", 50, level="asserted"), "resolves": fp,
               "claim": "the bound is now checked before the slice", "evidence": "src/m50.py:50-52"}
    out = reparse([closure], earlier)
    assert out["counts"] == {"critical": 0, "important": 0, "minor": 0}
    assert out["closures"] == [fp] and out["open_set"] == [] and out["unproven"] == []
    assert out["closure_rejected"] == [] and len(out["findings"]) == 1


def test_the_closure_s_own_fingerprint_is_never_compared_to_resolves():
    earlier = prior(finding("correctness", "IMPORTANT", 1))
    closure = {**finding("correctness", "IMPORTANT", 2), "claim": "fixed by the new guard",
               "resolves": earlier[0]["fingerprint"]}
    out = reparse([closure], earlier)
    assert out["findings"][0]["fingerprint"] != earlier[0]["fingerprint"]
    assert out["closures"] == [earlier[0]["fingerprint"]]


@pytest.mark.parametrize("make, reason", [
    (lambda fp: {"resolves": "0123456789abcdef"}, "names no fingerprint"),
    (lambda fp: {"resolves": fp, "evidence": ""}, "needs evidence"),
    (lambda fp: {"resolves": fp, "evidence": "   "}, "needs evidence"),
])
def test_an_invalid_closure_is_ignored_and_rejected_with_its_reason(make, reason):
    earlier = prior(finding("correctness", "IMPORTANT", 1), finding("correctness", "MINOR", 2))
    closure = {**finding("correctness", "CRITICAL", 30), **make(earlier[0]["fingerprint"])}
    out = reparse([closure], earlier)
    assert len(out["closure_rejected"]) == 1 and reason in out["closure_rejected"][0]["reason"]
    assert out["closures"] == []
    # Ignored: not counted itself, and the prior finding it named stays open.
    assert out["counts"] == {"critical": 0, "important": 1, "minor": 1}


def test_a_second_closure_of_the_same_fingerprint_is_dropped():
    earlier = prior(finding("correctness", "IMPORTANT", 1))
    fp = earlier[0]["fingerprint"]
    one = {**finding("correctness", "IMPORTANT", 40), "resolves": fp}
    two = {**finding("correctness", "CRITICAL", 41), "resolves": fp}
    out = reparse([one, two], earlier)
    assert out["closures"] == [fp]
    assert [r["reason"] for r in out["closure_rejected"]] == ["a second closure of the same fingerprint is dropped"]
    assert out["counts"]["critical"] == 0


def test_silence_never_closes_an_unmentioned_finding_is_carried_and_unaccounted():
    earlier = prior(finding("correctness", "IMPORTANT", 1), finding("correctness", "MINOR", 2))
    out = reparse([], earlier)
    assert out["counts"] == {"critical": 0, "important": 1, "minor": 1}
    assert sorted(out["unaccounted"]) == sorted(f["fingerprint"] for f in earlier)
    assert all(f["unaccounted"] for f in out["open_set"])


def test_a_verbatim_re_raise_stays_open_once_and_is_accounted_for():
    earlier = prior(finding("correctness", "IMPORTANT", 1))
    again = finding("correctness", "IMPORTANT", 1)  # the prior record copied verbatim
    out = reparse([again, again], earlier)
    assert out["counts"]["important"] == 1 and out["unaccounted"] == []
    assert [f["fingerprint"] for f in out["open_set"]] == [earlier[0]["fingerprint"]]
    assert "unaccounted" not in out["open_set"][0]


def test_a_re_raise_beside_a_closure_of_the_same_finding_keeps_it_open():
    earlier = prior(finding("correctness", "IMPORTANT", 1))
    fp = earlier[0]["fingerprint"]
    out = reparse([{**finding("correctness", "MINOR", 9), "resolves": fp},
                   finding("correctness", "IMPORTANT", 1)], earlier)
    assert out["closures"] == [] and out["counts"]["important"] == 1


def test_closing_every_prior_finding_with_empty_evidence_is_a_contract_failure():
    earlier = prior(finding("correctness", "IMPORTANT", 1), finding("correctness", "MINOR", 2))
    closures = [{**finding("correctness", "MINOR", 60 + n), "resolves": f["fingerprint"], "evidence": ""}
                for n, f in enumerate(earlier)]
    with pytest.raises(r5f.ReportError, match="empty evidence"):
        reparse(closures, earlier)


def test_a_closure_without_an_evidence_key_is_rejected_not_malformed():
    earlier = prior(finding("correctness", "IMPORTANT", 1), finding("correctness", "MINOR", 2))
    closure = {**finding("correctness", "MINOR", 70), "resolves": earlier[0]["fingerprint"]}
    del closure["evidence"]
    out = reparse([closure], earlier)
    assert "needs evidence" in out["closure_rejected"][0]["reason"]


def test_a_first_review_s_closure_is_rejected_ignored_and_logged(r5root):
    start(r5root)
    run(STATE, "pass", "correctness", root=r5root)
    hook(r5root)
    forged = finding("correctness", "CRITICAL", level="reproduced", resolves="anything", regression=True)
    out = run(STATE, "findings", "correctness", report("correctness", findings=[forged]), root=r5root)
    assert out.returncode == 0 and "closure(s) rejected and ignored" in out.stderr
    trail = state(r5root)["audit_trail"]
    rejected = [e for e in trail if e["event"] == "closure_rejected"]
    assert len(rejected) == 1 and rejected[0]["resolves"] == "anything"
    assert "names no fingerprint" in rejected[0]["reason"]
    received = [e for e in trail if e["event"] == "findings_received"][-1]
    assert received["counts"]["critical"] == 0 and received["open_set"] == []


def test_a_re_review_closes_what_the_last_round_left_open_and_carries_the_rest(r5root):
    start(r5root)
    run(STATE, "pass", "correctness", root=r5root)
    hook(r5root)
    run(STATE, "findings", "correctness", report("correctness", 0, 2, 1), root=r5root)
    first = state(r5root)["audit_trail"][-1]["open_set"]
    open_set = json.loads(run(STATE, "open-set", "correctness", root=r5root).stdout)
    assert [f["fingerprint"] for f in open_set] == [f["fingerprint"] for f in first]
    hook(r5root)
    run(STATE, "fixed", "correctness", root=r5root)
    run(STATE, "request", "correctness", "2", root=r5root)
    hook(r5root)
    closure = {**finding("correctness", "IMPORTANT", 80), "resolves": first[0]["fingerprint"],
               "claim": "the guard now covers it"}
    run(STATE, "findings", "correctness", report("correctness", findings=[closure]), root=r5root)
    second = state(r5root)["audit_trail"][-1]
    assert second["closures"] == [first[0]["fingerprint"]]
    assert sorted(second["unaccounted"]) == sorted(f["fingerprint"] for f in first[1:])
    assert second["counts"] == {"critical": 0, "important": 1, "minor": 1}
    entry = state(r5root)["rounds"][-1]["lenses"]["correctness"]
    assert entry["closures"] == [first[0]["fingerprint"]] and entry["unaccounted"] == 2
    refused = run(STATE, "done", "correctness", "1", root=r5root)
    assert refused.returncode == 2 and "its findings are not clean" in refused.stderr


def test_a_report_that_closes_everything_without_evidence_is_unavailable(r5root):
    start(r5root)
    run(STATE, "pass", "correctness", root=r5root)
    hook(r5root)
    run(STATE, "findings", "correctness", report("correctness", 0, 1, 0), root=r5root)
    fp = state(r5root)["audit_trail"][-1]["open_set"][0]["fingerprint"]
    hook(r5root)
    run(STATE, "fixed", "correctness", root=r5root)
    run(STATE, "request", "correctness", "2", root=r5root)
    hook(r5root)
    empty = {**finding("correctness", "IMPORTANT", 90), "resolves": fp, "evidence": ""}
    refused = run(STATE, "findings", "correctness", report("correctness", findings=[empty]), root=r5root)
    assert refused.returncode == 2 and "empty evidence" in refused.stderr
    assert events(r5root)[-1] == "reviewer_unavailable"


def test_a_bad_prior_open_set_file_is_a_usage_error(tmp_path):
    bad = tmp_path / "prior.json"
    bad.write_text('{"not": "a list"}')
    good = tmp_path / "r.md"
    good.write_text("R5-FINDINGS: []\n")
    assert r5f.main(["parse", "correctness", str(good), "--prior", str(bad)]) == 1


def test_a_re_review_whose_closures_carry_their_old_severity_can_be_done(r5root):
    start(r5root)
    run(STATE, "pass", "correctness", root=r5root)
    hook(r5root)
    run(STATE, "findings", "correctness", report("correctness", 0, 1, 0), root=r5root)
    prior = state(r5root)["audit_trail"][-1]["findings"][0]["fingerprint"]
    hook(r5root)
    run(STATE, "fixed", "correctness", root=r5root)
    run(STATE, "request", "correctness", "2", root=r5root)
    hook(r5root)
    closure = report("correctness", findings=[finding("correctness", "IMPORTANT", 10, resolves=prior)])
    run(STATE, "findings", "correctness", closure, root=r5root)
    assert run(STATE, "done", "correctness", "1", root=r5root).returncode == 0


def test_a_blocking_finding_below_the_floor_is_unproven_and_a_minor_never_is():
    raw = [finding("correctness", "IMPORTANT", 1, level="cited"),
           finding("correctness", "CRITICAL", 2, level="script"),
           finding("correctness", "IMPORTANT", 3, level="reproduced"),
           finding("correctness", "MINOR", 4, level="asserted")]
    no_level = finding("correctness", "IMPORTANT", 5)
    del no_level["verification_level"]
    out = parse([*raw, no_level])
    fps = {f["start_line"]: f["fingerprint"] for f in out["findings"]}
    assert out["unproven"] == [fps[1], fps[5]]  # a missing level is "asserted"
    assert parse(raw, floor="reproduced")["unproven"] == [fps[1], fps[2]]
    assert parse([finding("excellence", "IMPORTANT", level="asserted")], lens="excellence")["unproven"] == []


def test_findings_are_counted_from_the_report_and_block_done(r5root):
    start(r5root)
    run(STATE, "pass", "correctness", root=r5root)
    hook(r5root)
    assert run(STATE, "findings", "correctness", report("correctness", 0, 2, 1), "FIX",
               root=r5root).returncode == 0
    last = state(r5root)["audit_trail"][-1]
    assert last["counts"] == {"critical": 0, "important": 2, "minor": 1}
    assert len(last["findings"]) == 3 and all(f["status"] == "open" for f in last["findings"])
    refused = run(STATE, "done", "correctness", "0", root=r5root)
    assert refused.returncode == 2 and "its findings are not clean" in refused.stderr


def test_a_report_off_the_contract_records_the_reviewer_unavailable(r5root):
    start(r5root)
    run(STATE, "pass", "correctness", root=r5root)
    hook(r5root)
    bad = _REPORTS["dir"] / "prose-only.md"
    bad.write_text("Looks fine to me.\n")
    refused = run(STATE, "findings", "correctness", str(bad), root=r5root)
    assert refused.returncode == 2 and "recorded UNAVAILABLE" in refused.stderr
    assert events(r5root)[-1] == "reviewer_unavailable"
    assert state(r5root)["rounds"][-1]["lenses"]["correctness"]["status"] == "UNAVAILABLE"
    blocked = hook(r5root)
    assert blocked.returncode == 2 and "UNAVAILABLE; request it again" in blocked.stderr
    again = run(STATE, "findings", "correctness", report("correctness"), root=r5root)
    assert again.returncode == 2 and "no reviewer is in flight" in again.stderr
    undone = run(STATE, "done", "correctness", "0", root=r5root)
    assert undone.returncode == 2 and "followed by later activity" in undone.stderr
    assert run(STATE, "request", "correctness", "2", root=r5root).returncode == 0
    assert hook(r5root).returncode == 0
    assert run(STATE, "findings", "correctness", report("correctness"), root=r5root).returncode == 0
    assert run(STATE, "done", "correctness", "1", root=r5root).returncode == 0


def test_rounds_record_review_fix_and_verify_in_the_shared_shape(r5root):
    start(r5root)
    run(STATE, "pass", "correctness", root=r5root)
    hook(r5root, subagent="pr-review-toolkit:code-reviewer")
    meta = '{"model": "claude-opus-5-5", "tier": "deep", "tokens_in": 1200, "tokens_out": 300, "duration_s": 41, "status": "forged"}'
    run(STATE, "findings", "correctness", report("correctness", 0, 1, 0), "FIX", meta, root=r5root)
    hook(r5root)  # the fix agent
    run(STATE, "fixed", "correctness", '{"statuses": {"abc": "fixed"}}', root=r5root)
    run(STATE, "fixed", "correctness", '{"statuses": {"def": "superseded"}}', root=r5root)
    run(STATE, "event", "commit_made", '{"sha": "0123abc"}', root=r5root)
    run(STATE, "request", "correctness", "2", root=r5root)
    hook(r5root)
    run(STATE, "findings", "correctness", report("correctness"), "PASS", root=r5root)
    rounds = state(r5root)["rounds"]
    assert [(r["n"], r["kind"]) for r in rounds] == [(1, "review"), (2, "fix"), (3, "verify")]
    first = rounds[0]["lenses"]["correctness"]
    assert (first["mode"], first["status"], first["model"], first["tier"]) == ("review", "ok", "claude-opus-5-5", "deep")
    assert (first["tokens_in"], first["tokens_out"], first["duration_s"]) == (1200, 300, 41)
    assert len(first["findings"]) == 1 and rounds[0]["ended"] is not None
    assert rounds[1]["fix"] == {"commit": "0123abc", "statuses": {"abc": "fixed", "def": "superseded"},
                                "scope_breaches": []}
    assert rounds[2]["lenses"]["correctness"]["mode"] == "verify"
    assert rounds[2]["lenses"]["correctness"]["findings"] == []


def test_an_unproven_blocking_finding_is_listed_in_its_round_and_still_blocks(r5root):
    start(r5root)
    run(STATE, "pass", "correctness", root=r5root)
    hook(r5root)
    weak = report("correctness", findings=[finding("correctness", "IMPORTANT", level="cited")])
    run(STATE, "findings", "correctness", weak, root=r5root)
    entry = state(r5root)["rounds"][0]["lenses"]["correctness"]
    assert entry["unproven"] == [entry["findings"][0]["fingerprint"]]
    assert run(STATE, "done", "correctness", "0", root=r5root).returncode == 2
    lowered = report("correctness", findings=[finding("correctness", "IMPORTANT", 2, level="cited")])
    run(STATE, "request", "correctness", "2", root=r5root)
    hook(r5root)
    run(STATE, "findings", "correctness", lowered, root=r5root,
        env_extra={"R5_VERIFICATION_FLOOR": "cited"})
    assert state(r5root)["rounds"][-1]["lenses"]["correctness"]["unproven"] == []


def test_a_fix_with_non_object_statuses_is_refused_before_anything_is_written(r5root):
    start(r5root)
    run(STATE, "pass", "correctness", root=r5root)
    hook(r5root)
    run(STATE, "findings", "correctness", report("correctness", 0, 1, 0), root=r5root)
    before = state_file(r5root).read_text()
    bad = run(STATE, "fixed", "correctness", '{"statuses": ["x"]}', root=r5root)
    assert bad.returncode == 1 and "statuses must be an object" in bad.stderr
    assert state_file(r5root).read_text() == before


def test_a_checkout_with_no_commit_records_null_shas_not_the_word_head(r5root):
    subprocess.run(["git", "init", "-q", str(r5root)], check=True)
    start(r5root)
    run(STATE, "pass", "correctness", root=r5root)
    hook(r5root)
    assert state(r5root)["rounds"][0]["head"] is None
    run(STATE, "finish", "failed", root=r5root)
    archived = json.loads(next((r5root / ".claude").glob("r5-state-b1-failed-*.json")).read_text())
    assert archived["verdict"]["state"] == "ESCALATED"
    assert archived["verdict"]["tree_sha"] is None and archived["verdict"]["head_sha"] is None


def test_abandoning_a_reviewer_closes_its_round_entry(r5root):
    start(r5root)
    run(STATE, "pass", "correctness", root=r5root)
    hook(r5root)
    run(STATE, "abandon", "correctness", "stalled", root=r5root)
    assert state(r5root)["rounds"][0]["lenses"]["correctness"]["status"] == "abandoned"


def test_the_excellence_report_is_parsed_and_a_bad_one_is_unavailable(r5root):
    start(r5root)
    for name in ("correctness", "clarity", "edge-cases", "provenance"):
        clear_pass(r5root, name)
    run(STATE, "pass", "excellence", root=r5root)
    hook(r5root)
    bad = _REPORTS["dir"] / "excellence-prose.md"
    bad.write_text("Ship it.\n")
    refused = run(STATE, "advisory", str(bad), "SHIP IT", root=r5root)
    assert refused.returncode == 2 and "recorded UNAVAILABLE" in refused.stderr
    run(STATE, "request", "excellence", "2", root=r5root)
    hook(r5root)
    assert run(STATE, "advisory", report("excellence", 0, 1, 2), "SHIP WITH MINOR FIXES",
               root=r5root).returncode == 0
    read = state(r5root)["advisory_results"][-1]
    assert read["counts"] == {"critical": 0, "important": 1, "minor": 2} and len(read["findings"]) == 3
    assert state(r5root)["rounds"][-1]["lenses"]["excellence"]["unproven"] == []


def git_root(root):
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "a.txt").write_text("a\n")
    subprocess.run(["git", "-C", str(root), "add", "a.txt"], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-q", "-m", "a"], check=True)
    return subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD^{tree}", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.split()


def test_finish_writes_a_verdict_keyed_to_the_tree(r5root):
    tree, head = git_root(r5root)
    start(r5root)
    record = state(r5root)
    assert (record["bead"], record["repo"], record["base"], record["head"]) == ("b1", r5root.name, "base", "head")
    assert record["branch"] and record["rounds"] == [] and record["verdict"] is None
    for name in ("correctness", "clarity", "edge-cases", "provenance"):
        clear_pass(r5root, name)
    run(STATE, "pass", "excellence", root=r5root)
    hook(r5root)
    run(STATE, "advisory", report("excellence"), "SHIP IT", root=r5root)
    assert run(STATE, "finish", "completed", root=r5root).returncode == 0
    archived = json.loads(next((r5root / ".claude").glob("r5-state-b1-completed-*.json")).read_text())
    verdict = archived["verdict"]
    assert (verdict["state"], verdict["tree_sha"], verdict["head_sha"]) == ("CLEAN", tree, head)
    assert archived["rounds"][0]["head"] == head


@pytest.mark.parametrize("status, expected", [("failed", "ESCALATED"), ("aborted", None)])
def test_a_failed_run_is_escalated_and_an_aborted_one_has_no_verdict(r5root, status, expected):
    git_root(r5root)
    start(r5root)
    run(STATE, "finish", status, root=r5root)
    archived = json.loads(next((r5root / ".claude").glob(f"r5-state-b1-{status}-*.json")).read_text())
    assert (archived["verdict"] or {}).get("state") == expected


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
    for args in (("findings", "correctness", "no-such-report.md"), ("findings", "correctness"),
                 ("request", "correctness", "two"), ("init",), ("abandon", "correctness", "")):
        bad = run(STATE, *args, root=r5root)
        assert bad.returncode == 1 and bad.stderr.startswith("r5-state: "), (args, bad.stderr)
    assert events(r5root)[-1] == "reviewer_dispatched"


def test_bad_json_leaves_the_state_untouched_and_no_temp_file(r5root):
    start(r5root)
    before = (r5root / ".claude" / "r5-state.json").read_text()
    bad = run(STATE, "event", "x", "not-json", root=r5root)
    assert bad.returncode == 1 and "expected a JSON object" in bad.stderr
    assert (r5root / ".claude" / "r5-state.json").read_text() == before
    assert not list((r5root / ".claude").glob("*.tmp.*"))


def test_a_killed_process_holding_the_mutex_does_not_block_the_next_call(r5root):
    start(r5root)
    holder = hold_mutex(r5root / ".claude" / "r5-state.json.flock")
    blocked = None
    try:
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
    blocked = hook(r5root, path=no_jq_path(tmp_path_factory))
    assert blocked.returncode == 2 and "jq is missing" in blocked.stderr


def test_without_jq_a_lock_alone_refuses_every_dispatch(r5root, tmp_path_factory):
    run(LOCK, "acquire", "r5-supervise", "b1", root=r5root, session="HOLDER")
    assert not state_file(r5root).exists()
    blocked = hook(r5root, path=no_jq_path(tmp_path_factory))
    assert blocked.returncode == 2 and "jq is missing" in blocked.stderr


def test_an_inherited_r5_root_is_ignored(r5root, tmp_path_factory):
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    assert run(LOCK, "acquire", "r5-supervise", "b1", root=r5root,
               env_extra={"R5_ROOT": str(elsewhere)}).returncode == 0
    assert lock_file(r5root).exists()
    assert not (elsewhere / ".claude").exists()


def hooks_in(repo, curamoir=True):
    """A copy of the helpers inside a scratch git repository, so a test can run
    them with no override and a stray cwd without resolving to this checkout."""
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    if curamoir:
        (repo / ".claude-plugin").mkdir()
        (repo / ".claude-plugin" / "plugin.json").write_text('{"name": "curamoir"}')
    shutil.copytree(HOOKS, repo / ".claude" / "hooks")
    return repo / ".claude" / "hooks"


def bare_env(session="S1"):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("CURAMOIR_", "R5_")) and k not in _SESSION_VARS}
    env["CLAUDE_CODE_SESSION_ID"] = session
    return env


def bare(script, *args, cwd, stdin="", session="S1"):
    """Run a copied helper with no override: it resolves its own repository."""
    return subprocess.run(["bash", str(script), *args], input=stdin, capture_output=True,
                          text=True, env=bare_env(session), cwd=cwd, timeout=60)


def test_outside_a_curamoir_checkout_the_helpers_refuse_and_the_hook_allows(tmp_path):
    hooks = hooks_in(tmp_path / "other", curamoir=False)
    lock = subprocess.run(["bash", str(hooks / "r5-lock.sh"), "status"], capture_output=True,
                          text=True, env=bare_env(), cwd=tmp_path, timeout=60)
    assert lock.returncode == 3 and "not inside a curamoir checkout" in lock.stderr
    allowed = subprocess.run(["bash", str(hooks / "r5-parallel-block.sh")], input="{}",
                             capture_output=True, text=True, env=bare_env(), cwd=tmp_path,
                             timeout=60)
    assert allowed.returncode == 0


def test_the_hook_checks_its_own_repository_whatever_the_session_s_cwd(tmp_path):
    repo = tmp_path / "repo"
    hooks = hooks_in(repo)
    for args in (("init", "b1", "x", "y"), ("pass", "correctness")):
        assert subprocess.run(["bash", str(hooks / "r5-state.sh"), *args], env=bare_env(),
                              cwd=repo, capture_output=True, timeout=60).returncode == 0
    elsewhere = tmp_path / "elsewhere"
    subprocess.run(["git", "init", "-q", str(elsewhere)], check=True)
    results = [subprocess.run(["bash", str(hooks / "r5-parallel-block.sh")], input=payload(cwd=cwd),
                              capture_output=True, text=True, env=bare_env(), cwd=cwd, timeout=60)
               for cwd in (repo, tmp_path, elsewhere)]
    assert [r.returncode for r in results] == [0, 2, 2]  # the reviewer, then two refusals
    assert all("reviewer is in flight" in r.stderr for r in results[1:])


def test_the_hook_works_through_a_symlink_and_with_cdpath_set(tmp_path):
    repo = tmp_path / "repo"
    hooks = hooks_in(repo)
    for args in (("init", "b1", "x", "y"), ("pass", "correctness")):
        subprocess.run(["bash", str(hooks / "r5-state.sh"), *args], env=bare_env(), cwd=repo,
                       capture_output=True, timeout=60, check=True)
    link = tmp_path / "linked-hook.sh"
    link.symlink_to(hooks / "r5-parallel-block.sh")
    env = {**bare_env(), "CDPATH": f".:{tmp_path}"}
    first = subprocess.run(["bash", str(link)], input=payload(cwd=tmp_path), capture_output=True,
                           text=True, env=env, cwd=tmp_path, timeout=60)
    second = subprocess.run(["bash", ".claude/hooks/r5-parallel-block.sh"], input=payload(cwd=repo),
                            capture_output=True, text=True, env=env, cwd=repo, timeout=60)
    assert (first.returncode, second.returncode) == (0, 2), (first.stderr, second.stderr)


def test_a_hook_without_its_libraries_refuses(tmp_path):
    lone = tmp_path / "lone.sh"
    shutil.copy(HOOK, lone)
    refused = subprocess.run(["bash", str(lone)], input="{}", capture_output=True, text=True,
                             env=bare_env(), cwd=tmp_path, timeout=60)
    assert refused.returncode == 2 and "installed wrongly" in refused.stderr


def test_a_hook_with_the_resolver_but_without_the_lease_library_refuses(tmp_path):
    (tmp_path / "hooks" / "lib").mkdir(parents=True)
    shutil.copy(HOOK, tmp_path / "hooks" / "r5-parallel-block.sh")
    shutil.copy(HOOKS / "lib" / "resolve-repo-root.sh", tmp_path / "hooks" / "lib")
    refused = bare(tmp_path / "hooks" / "r5-parallel-block.sh", cwd=tmp_path, stdin=payload())
    assert refused.returncode == 2 and "installed wrongly" in refused.stderr


def test_a_state_helper_that_fails_makes_the_hook_refuse(tmp_path):
    repo = tmp_path / "repo"
    hooks = hooks_in(repo)
    (hooks / "r5-state.sh").write_text("#!/usr/bin/env bash\nexit 3\n")
    (repo / ".claude" / "r5-state.json").write_text('{"target_bead": "b1"}')
    refused = bare(hooks / "r5-parallel-block.sh", cwd=repo, stdin=payload(cwd=repo))
    assert refused.returncode == 2 and "the state helper failed (exit 3)" in refused.stderr


def test_a_checkout_of_another_plugin_is_not_curamoir(tmp_path):
    repo = tmp_path / "other"
    hooks = hooks_in(repo, curamoir=False)
    (repo / ".claude-plugin").mkdir()
    (repo / ".claude-plugin" / "plugin.json").write_text('{"name": "other"}')
    write_lock(repo, "HOLDER")  # would refuse S1's dispatch, were this curamoir
    for script in ("r5-lock.sh", "r5-state.sh"):
        refused = bare(hooks / script, "status" if script == "r5-lock.sh" else "show", cwd=repo)
        assert refused.returncode == 3 and "not inside a curamoir checkout" in refused.stderr, script
    assert bare(hooks / "r5-parallel-block.sh", cwd=repo, stdin=payload(cwd=repo)).returncode == 0


def test_a_sibling_worktree_shares_the_main_worktree_s_lock_and_state(tmp_path):
    repo = tmp_path / "repo"
    hooks_in(repo)
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
           "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null"]
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "hooks"], check=True)
    sibling = tmp_path / "sibling"
    subprocess.run([*git, "worktree", "add", "-q", str(sibling)], check=True)
    sib_hooks = sibling / ".claude" / "hooks"
    assert bare(sib_hooks / "r5-lock.sh", "acquire", "r5-supervise", "b1", cwd=sibling).returncode == 0
    assert bare(sib_hooks / "r5-state.sh", "init", "b1", "x", "y", cwd=sibling).returncode == 0
    assert state(repo)["target_bead"] == "b1"
    assert json.loads(lock_file(repo).read_text())["session_id"] == "S1"
    assert not state_file(sibling).exists() and not lock_file(sibling).exists()


def test_a_missing_tool_fails_the_module_in_ci_and_skips_it_locally(tmp_path):
    """Run this module in a child pytest with an empty PATH (no bash, jq or
    python3): with CI set it must not collect; without, its tests are skipped."""
    env = {k: v for k, v in os.environ.items() if k != "CI"}
    env["PATH"] = str(tmp_path)
    cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(Path(__file__)),
           "-k", "test_no_run_and_no_lock_lets_every_dispatch_through"]
    local = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=ROOT, timeout=120)
    assert local.returncode == 0 and "1 skipped" in local.stdout, local.stdout
    ci = subprocess.run(cmd, capture_output=True, text=True, env={**env, "CI": "true"}, cwd=ROOT,
                        timeout=120)
    assert ci.returncode != 0 and "CI must install them" in ci.stdout, ci.stdout
