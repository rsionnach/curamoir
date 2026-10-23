"""cm-a2s: a job's `CURAMOIR_JOB`, the E0-R5 denylist's primary signal.

The denylist hook (hooks/_entry.in_curamoir_job) knows a job by
CURAMOIR_JOB, falling back to the supervisor lock. The lock is gitignored,
so the sweep's sandbox clone has none, and there the variable is the only
signal. `job_guard.run_guarded` is the one choke point every job passes, so
it sets the variable for the run and restores what was there before on
every exit path; every `claude -p` a job starts inherits it through
git_guard.scrubbed_environment, which drops only GIT_*.

These prove the variable's lifetime through run_guarded and runner.run_job,
then the propagation through the fix step's real spawn path, and last the
whole chain: the real hook, run with the environment the fix step's child
gets, in a clone with no lock, denying `rm -rf` (through a stand-in `claude`
that loads the hook; the real `--restricted` run does not yet, cm-60c).

One part of run_guarded's ordering is held by review, not by a test:
Ordering 5a's "read `before` before the `try`". Only a signal landing
between `try:` and the read could tell the two placements apart.
"""

import json
import os
import signal
import subprocess
import sys
import time

import pytest

from curamoir import job_guard
from curamoir.fix_step import run_fix_step
from curamoir.git_guard import prepare_sandbox
from curamoir.job_guard import LockSlot, run_guarded
from curamoir.runner import EXIT_FAILURE, EXIT_REFUSED, EXIT_SUCCESS, run_job
from curamoir.scaffold import init_repo
from curamoir.termination import Terminated
from tests import claude_payloads
from tests.harness import Harness
from tests.hooks.test_pretooluse_entrypoint import REPO_ROOT
from tests.test_job_guard import CEILING, listing, october, seed_spend
from tests.test_lock_slot import CHILD_SECONDS, with_body

#: A literal on purpose, not job_guard.JOB_VARIABLE: the hook reads this
#: name, so a rename on one side only must fail here.
VARIABLE = "CURAMOIR_JOB"


@pytest.fixture
def repo(harness, monkeypatch):
    """The harness repo, initialised, in a process with no CURAMOIR_JOB of its
    own (the developer's shell could export one)."""
    monkeypatch.delenv(VARIABLE, raising=False)
    init_repo(harness.repo)
    return harness


def seen_by(record):
    """A body that records CURAMOIR_JOB as it runs, then succeeds."""
    def body(_repo, run):
        record.append(os.environ.get(VARIABLE))
        run.succeeded("looked")
    return body


# --- set for the run --------------------------------------------------------------------

def test_set_to_the_job_while_the_body_runs_and_removed_after(repo, monkeypatch):
    during = []
    with_body(monkeypatch, "brief", seen_by(during))

    assert run_job(repo.repo, "brief", now=october).exit_code == EXIT_SUCCESS

    assert during == ["brief"]
    assert VARIABLE not in os.environ


def test_a_value_that_was_there_before_comes_back(repo, monkeypatch):
    monkeypatch.setenv(VARIABLE, "the developer's")
    during = []
    with_body(monkeypatch, "brief", seen_by(during))

    assert run_job(repo.repo, "brief", now=october).exit_code == EXIT_SUCCESS

    assert during == ["brief"]
    assert os.environ[VARIABLE] == "the developer's"


def test_set_before_the_guards_run_and_restored_only_after_the_release(repo, monkeypatch):
    """Set before anything the run does that could start a process: the
    guards' callables (the kill switch's label lookup is a `gh` call). And
    still set while the lock is released, so for the whole run at least one
    of the hook's two signals is present."""
    seen = {}

    def labels():
        seen["guards"] = os.environ.get(VARIABLE)
        return []

    real_release = LockSlot.release

    def release(self):
        seen["release"] = os.environ.get(VARIABLE)
        real_release(self)

    monkeypatch.setattr(LockSlot, "release", release)

    run_job(repo.repo, "brief", repo_labels=labels, now=october)

    assert seen == {"guards": "brief", "release": "brief"}
    assert VARIABLE not in os.environ


# --- restored on every exit path ---------------------------------------------------------

@pytest.mark.parametrize("before", [None, "outer"], ids=["absent", "previous-value"])
def test_restored_after_a_guard_refuses(repo, monkeypatch, before):
    if before is not None:
        monkeypatch.setenv(VARIABLE, before)
    seed_spend(repo.ledger(), 0.95 * CEILING)

    assert run_job(repo.repo, "brief", now=october).exit_code == EXIT_REFUSED

    assert os.environ.get(VARIABLE) == before


@pytest.mark.parametrize("before", [None, "outer"], ids=["absent", "previous-value"])
@pytest.mark.parametrize("crash", [RuntimeError("disk on fire"), SystemExit(0)],
                         ids=["exception", "system-exit"])
def test_restored_after_the_body_raises(repo, monkeypatch, crash, before):
    if before is not None:
        monkeypatch.setenv(VARIABLE, before)

    def body(_repo, _run):
        assert os.environ[VARIABLE] == "brief"
        raise crash

    with_body(monkeypatch, "brief", body)

    assert run_job(repo.repo, "brief", now=october).exit_code == EXIT_FAILURE

    assert os.environ.get(VARIABLE) == before


@pytest.mark.parametrize("before", [None, "outer"], ids=["absent", "previous-value"])
def test_restored_after_a_keyboard_interrupt(repo, monkeypatch, before):
    if before is not None:
        monkeypatch.setenv(VARIABLE, before)

    def body(_repo, _run):
        raise KeyboardInterrupt

    with_body(monkeypatch, "brief", body)

    with pytest.raises(KeyboardInterrupt):
        run_job(repo.repo, "brief", now=october)

    assert os.environ.get(VARIABLE) == before


@pytest.mark.parametrize("before", [None, "outer"], ids=["absent", "previous-value"])
def test_restored_after_a_real_sigterm_ends_the_run(repo, monkeypatch, before):
    """A scheduler's SIGTERM, turned into Terminated by run_guarded
    (curamoir.termination). The absorbing handler stands in for an
    embedding process, so the re-sent signal does not end pytest."""
    if before is not None:
        monkeypatch.setenv(VARIABLE, before)

    def body(_repo, _run):
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(CHILD_SECONDS)

    with_body(monkeypatch, "brief", body)
    previous = signal.signal(signal.SIGTERM, lambda *_: None)
    try:
        ran = run_job(repo.repo, "brief", now=october)
    finally:
        signal.signal(signal.SIGTERM, previous)

    assert ran.exit_code == EXIT_FAILURE and "Terminated" in ran.sentence
    assert os.environ.get(VARIABLE) == before


def test_restored_after_terminated_propagates_from_run_guarded(repo):
    """run_guarded itself, which lets Terminated through to its caller."""
    def body(_run):
        raise Terminated(signal.SIGTERM)

    with pytest.raises(Terminated):
        run_guarded(repo.repo, "brief", None, body, now=october, opens_prs=False)

    assert VARIABLE not in os.environ


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT], ids=["SIGTERM", "SIGINT"])
def test_restored_when_a_signal_lands_during_the_restore_itself(repo, monkeypatch, signum):
    """The restore runs in the held phase, so a signal arriving as the
    variable is put back does not cut it short. It arrives once the run is
    over: SIGTERM is re-sent (to an absorbing handler, standing in for an
    embedding process, so pytest survives), and ^C (SIGINT, under Python's
    default handler, as in a terminal) as KeyboardInterrupt."""
    real = job_guard._restore_job_variable
    absorbed, restored = [], []

    def signalled_restore(before):
        os.kill(os.getpid(), signum)
        real(before)
        restored.append(VARIABLE not in os.environ)

    monkeypatch.setattr(job_guard, "_restore_job_variable", signalled_restore)
    handler = (signal.default_int_handler if signum == signal.SIGINT
               else lambda received, _frame: absorbed.append(received))
    previous = signal.signal(signum, handler)
    interrupted = False
    try:
        run_job(repo.repo, "brief", now=october)
    except KeyboardInterrupt:
        interrupted = True
    finally:
        signal.signal(signum, previous)

    assert restored == [True]
    assert VARIABLE not in os.environ
    if signum == signal.SIGINT:
        assert interrupted and absorbed == []
    else:
        assert not interrupted and absorbed == [signal.SIGTERM]


def test_a_nested_run_restores_the_outer_runs_value(repo, tmp_path):
    """A run inside a run (on another repository) puts back the outer job's
    name, not nothing: the outer job is still running."""
    other = Harness(tmp_path / "other")
    init_repo(other.repo)
    seen = []

    def inner(run):
        seen.append(os.environ.get(VARIABLE))
        run.succeeded("inner")

    def outer(run):
        seen.append(os.environ.get(VARIABLE))
        run_guarded(other.repo, "brief", None, inner, now=october, opens_prs=False)
        seen.append(os.environ.get(VARIABLE))
        run.succeeded("outer")

    run_guarded(repo.repo, "sweep", listing(), outer, now=october)

    assert seen == ["sweep", "brief", "sweep"]
    assert VARIABLE not in os.environ


@pytest.mark.parametrize("job, error", [("", ValueError), (None, TypeError),
                                        ("a\0b", ValueError)],
                         ids=["empty", "none", "nul"])
def test_a_job_name_the_hook_could_not_read_is_refused_before_anything_runs(
        repo, monkeypatch, job, error):
    """'' would set a value the hook takes for "not in a job"; None and a NUL
    would raise from os.environ mid-run. All three are refused up front:
    no body, no job line, no lock, the variable untouched."""
    monkeypatch.setenv(VARIABLE, "outer")
    ledger = repo.repo / ".curamoir" / "state" / "ledger.jsonl"
    before = ledger.read_bytes() if ledger.exists() else None
    handlers = signal.getsignal(signal.SIGTERM)
    called = []

    with pytest.raises(error, match="job's name"):
        run_guarded(repo.repo, job, None, called.append, now=october, opens_prs=False)

    assert called == []
    assert (ledger.read_bytes() if ledger.exists() else None) == before
    assert not (repo.repo / ".curamoir" / "state" / "lock").exists()
    assert os.environ[VARIABLE] == "outer"
    assert signal.getsignal(signal.SIGTERM) is handlers


def test_a_signal_inside_the_set_leaves_no_variable_for_children(repo, monkeypatch):
    """The torn set: a SIGTERM landing inside `os.environ[...] = job` after
    its putenv() and before os.environ records the key. os.environ then has
    no key to pop, but the C environment, which a child started with
    env=None inherits, still has the job's name; the restore's unsetenv()
    is what clears it."""
    real_setitem = os._Environ.__setitem__

    def torn(self, key, value):
        if self is os.environ and key == VARIABLE:
            os.putenv(key, value)
            raise Terminated(signal.SIGTERM)
        real_setitem(self, key, value)

    monkeypatch.setattr(os._Environ, "__setitem__", torn)
    try:
        with pytest.raises(Terminated):
            run_guarded(repo.repo, "brief", None, lambda run: None, now=october,
                        opens_prs=False)
        monkeypatch.setattr(os._Environ, "__setitem__", real_setitem)
        child = subprocess.run(
            [sys.executable, "-c", f"import os; print(os.environ.get({VARIABLE!r}))"],
            capture_output=True, text=True, check=True)
    finally:
        # Never leak the job's name into the rest of the suite's children.
        os.unsetenv(VARIABLE)

    assert VARIABLE not in os.environ
    assert child.stdout.strip() == "None"


def test_an_unusable_root_never_sets_it(tmp_path, monkeypatch):
    """Nothing runs, so there is nothing for the variable to describe."""
    monkeypatch.delenv(VARIABLE, raising=False)
    observed = []

    result = run_guarded(tmp_path / "gone", "brief", None,
                         lambda run: observed.append(1), opens_prs=False)

    assert result.outcome == job_guard.UNUSABLE_ROOT and observed == []
    assert VARIABLE not in os.environ


# --- through the fix step's spawn path ----------------------------------------------------

def committed(harness):
    """Commit `.curamoir/` as /curamoir:init leaves it, so a clone carries it
    (the hook does nothing in a repo without one) and not the gitignored lock."""
    subprocess.run(["git", "add", ".curamoir"], cwd=harness.repo, check=True)
    subprocess.run(["git", "commit", "-qm", "curamoir init"], cwd=harness.repo, check=True)


def stand_in_claude(tmp_path, source):
    """An executable `claude` at tmp_path/bin running the Python `source`,
    which ends by printing a `--output-format json` success, as claude -p does."""
    script = tmp_path / "bin" / "claude"
    script.parent.mkdir(parents=True, exist_ok=True)
    success = json.dumps(claude_payloads.success("done", cost=0.0))
    script.write_text(f"#!{sys.executable}\n{source}\nprint({success!r})\n",
                      encoding="utf-8")
    script.chmod(0o755)
    return script


def test_the_fix_steps_child_is_started_with_the_jobs_variable(repo, tmp_path):
    """run_fix_step builds its child's environment (scrubbed_environment, no
    GIT_*) from the process's: inside a guarded run that carries the job."""
    committed(repo)
    log = tmp_path / "env.json"
    claude = stand_in_claude(tmp_path, (
        "import json, os\n"
        f"open({str(log)!r}, 'w').write(json.dumps(os.environ.get({VARIABLE!r})))"))

    def body(run):
        with prepare_sandbox(repo.repo, "HEAD", parent=tmp_path) as sandbox:
            run_fix_step(sandbox, "p", model="m", claude=str(claude))
        run.succeeded("fixed")

    run_guarded(repo.repo, "sweep", listing(), body, now=october)

    assert json.loads(log.read_text(encoding="utf-8")) == "sweep"


#: A stand-in `claude` that does what Claude Code does for a PreToolUse hook
#: (DEC-5; https://code.claude.com/docs/en/hooks: "A hook process inherits
#: the parent environment"): runs cúramóir's real hook with its own
#: environment, the payload's `cwd` its working directory, and records the
#: hook's answer. It runs the command hooks/hooks.json registers,
#: `"${CLAUDE_PLUGIN_ROOT}"/scripts/curamoir-hook-pretooluse`, with
#: CLAUDE_PLUGIN_ROOT set as Claude Code sets it for a plugin hook, and
#: nothing put on PYTHONPATH (the wrapper does that). It loads the hook the
#: way a `claude` with cúramóir's plugin would; the real fix step's
#: `--restricted` run does not load it yet (cm-60c).
HOOK_RUNNING_CLAUDE = """\
import json, os, subprocess
payload = {{"hook_event_name": "PreToolUse", "tool_name": "Bash",
           "tool_input": {{"command": "rm -rf build/"}}, "cwd": os.getcwd()}}
env = dict(os.environ, CLAUDE_PLUGIN_ROOT={root!r})
hook = subprocess.run([os.path.join({root!r}, "scripts", "curamoir-hook-pretooluse")],
                      input=json.dumps(payload), capture_output=True, text=True, env=env)
lock = os.path.join(os.getcwd(), ".curamoir", "state", "lock")
open({log!r}, "w").write(json.dumps({{
    "stdout": hook.stdout, "stderr": hook.stderr, "returncode": hook.returncode,
    "curamoir": os.path.isdir(".curamoir"), "lock": os.path.exists(lock)}}))
"""


def test_the_real_hook_denies_rm_rf_inside_the_fix_steps_lockless_clone(repo, tmp_path):
    """cm-a2s's acceptance test: the whole chain, with no hand-built env.
    `curamoir run sweep`'s run_guarded, the fix step's spawn in a sandbox
    clone that has `.curamoir/` but no lock, and the real hook process.

    It proves the signal reaches the hook, not that the hook is loaded: the
    stand-in runs the hook as a `claude` with cúramóir's plugin would, and
    the real fix step's `--restricted` run loads no plugin hook yet (cm-60c)."""
    committed(repo)
    log = tmp_path / "hook.json"
    script = stand_in_claude(tmp_path, HOOK_RUNNING_CLAUDE.format(
        root=str(REPO_ROOT), log=str(log)))

    def body(run):
        with prepare_sandbox(repo.repo, "HEAD", parent=tmp_path) as sandbox:
            run_fix_step(sandbox, "p", model="m", claude=str(script))
        run.succeeded("fixed")

    run_guarded(repo.repo, "sweep", listing(), body, now=october)

    seen = json.loads(log.read_text(encoding="utf-8"))
    assert seen["curamoir"] and not seen["lock"], "the clone must have .curamoir/ and no lock"
    assert seen["returncode"] == 0, seen["stderr"]
    assert seen["stdout"].strip(), "the hook allowed rm -rf in the clone: " + seen["stderr"]
    decision = json.loads(seen["stdout"])["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
