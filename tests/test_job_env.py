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
gets, in a clone with no lock, denying `rm -rf`: first through a stand-in
`claude` that runs the plugin's hook as a plugin-loading `claude` would,
then (cm-60c) through one that loads hooks only from the fix step's own
`--settings`, which is all the real `--restricted` run loads.

One part of run_guarded's ordering is held by review, not by a test:
Ordering 5a's "read `before` before the `try`". Only a signal landing
between `try:` and the read could tell the two placements apart.
"""

import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from functools import partial

import pytest

from curamoir import fix_step, job_guard
from curamoir.config import LOCAL_RUNNER, Runner, RunnerKind
from curamoir.fix_step import fix_tools, run_fix_step
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
#: `--restricted` run loads it from its `--settings` instead
#: (SETTINGS_LOADING_CLAUDE below, cm-60c).
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
    stand-in runs the hook as a `claude` with cúramóir's plugin would. That
    the fix step loads it is the next test's (cm-60c)."""
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


#: A stand-in `claude` that loads PreToolUse hooks the way the real fix
#: step's `--restricted` run can: ONLY from its `--settings` argument (cm-60c).
#: For a Bash call it runs each matching entry's command through a shell, as
#: Claude Code runs a command hook, with its own environment and the
#: payload's `cwd` as the working directory, and records every answer.
SETTINGS_LOADING_CLAUDE = """\
import json, os, re, subprocess, sys
settings = json.loads(sys.argv[sys.argv.index("--settings") + 1])
payload = {{"hook_event_name": "PreToolUse", "tool_name": "Bash",
           "tool_input": {{"command": "rm -rf build/"}}, "cwd": os.getcwd()}}
answers = []
for entry in settings.get("hooks", {{}}).get("PreToolUse", []):
    if re.fullmatch(entry.get("matcher") or ".*", "Bash"):
        for hook in entry["hooks"]:
            ran = subprocess.run(["/bin/sh", "-c", hook["command"]], input=json.dumps(payload),
                                 capture_output=True, text=True, timeout=hook["timeout"])
            answers.append({{"stdout": ran.stdout, "stderr": ran.stderr,
                             "returncode": ran.returncode}})
open({log!r}, "w").write(json.dumps({{"answers": answers, "events": sorted(settings["hooks"])}}))
"""


@pytest.mark.parametrize("hosted", [False, True], ids=["local", "hosted"])
def test_the_fix_steps_own_settings_load_the_denylist_that_denies_rm_rf(
        repo, tmp_path, monkeypatch, hosted):
    """cm-60c's acceptance test: through run_guarded and the fix step's real
    spawn into a lockless clone, the hook the fix step's argv registers (and
    nothing a plugin would add) denies `rm -rf`. A CLAUDE_PLUGIN_ROOT left in
    the environment by some other plugin does not send it elsewhere. On the
    hosted runner too (cm-2qp.3, user decision 2026-10-29), where the fix
    agent has Bash: the denylist --settings still load, under --restricted."""
    committed(repo)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path / "some-other-plugin"))
    log = tmp_path / "hooks.json"
    script = stand_in_claude(tmp_path, SETTINGS_LOADING_CLAUDE.format(log=str(log)))
    runner = Runner(RunnerKind.HOSTED) if hosted else LOCAL_RUNNER
    argvs = []

    def body(run):
        with prepare_sandbox(repo.repo, "HEAD", parent=tmp_path) as sandbox:
            argvs.append(run_fix_step(sandbox, "p", model="m", claude=str(script),
                                      tools=fix_tools(runner, "pytest -q"), runner=runner,
                                      test_command="pytest -q").argv)
        run.succeeded("fixed")

    run_guarded(repo.repo, "sweep", listing(), body, now=october)

    (argv,) = argvs
    assert "--restricted" in argv
    assert ("Bash" in argv[argv.index("--tools") + 1]) is hosted

    seen = json.loads(log.read_text(encoding="utf-8"))
    assert seen["events"] == ["PreToolUse"]
    (answer,) = seen["answers"]
    assert answer["returncode"] == 0, answer["stderr"]
    assert answer["stdout"].strip(), "the hook allowed rm -rf in the clone: " + answer["stderr"]
    assert json.loads(answer["stdout"])["hookSpecificOutput"]["permissionDecision"] == "deny"


# --- the denylist is checked before the agent starts (cm-60c, R5 edge cases) --------------
#
# A command hook that says nothing, or cannot run, ALLOWS the call. So before
# a fix agent with Bash starts, run_fix_step runs each hook command its own
# --settings carry, as Claude Code will, and refuses unless it denies. Every
# test here goes through the real run_fix_step with a stand-in `claude` that
# only records that it was started: a refusal must leave no such record.

HOSTED = Runner(RunnerKind.HOSTED)


def started_marker(tmp_path):
    """A stand-in `claude` that writes `started` beside itself, and that
    file's path: absent after a run, the agent was never started."""
    marker = tmp_path / "started"
    script = stand_in_claude(tmp_path, f"open({str(marker)!r}, 'w').write('started')")
    return script, marker


def hosted_fix_step(repo, tmp_path, script, **options):
    """The hosted fix step with Bash, as sweep_job calls it, in a clone of
    `repo`. The caller sets CURAMOIR_JOB (or leaves it unset on purpose).
    Returns (FixStepRun, the number of before_call calls)."""
    called = []
    options.setdefault("tools", fix_tools(HOSTED, "pytest -q"))
    with prepare_sandbox(repo.repo, "HEAD", parent=tmp_path) as sandbox:
        run = run_fix_step(sandbox, "p", model="m", claude=str(script), runner=HOSTED,
                           test_command="pytest -q",
                           before_call=lambda: called.append(True), **options)
    return run, len(called)


def refused_hosted_fix_step(repo, tmp_path, script, marker, match):
    """The same call, which must raise ValueError matching `match` before
    before_call and before the stand-in starts. Returns the message."""
    called = []
    with (prepare_sandbox(repo.repo, "HEAD", parent=tmp_path) as sandbox,
          pytest.raises(ValueError, match=match) as refused):
        run_fix_step(sandbox, "p", model="m", claude=str(script), runner=HOSTED,
                     tools=fix_tools(HOSTED, "pytest -q"), test_command="pytest -q",
                     before_call=lambda: called.append(True))
    assert not marker.exists(), "the agent was started with a denylist that does not deny"
    assert called == [], "before_call ran, so the call was noted as possibly spent"
    return str(refused.value)


def plugin_copy(tmp_path):
    """A copy of what the checkout's PreToolUse hook command needs (its
    launcher and src/curamoir), for a test to break: the repository's own
    files are never touched."""
    root = tmp_path / "plugin copy"
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "scripts" / "curamoir-hook-pretooluse", root / "scripts")
    shutil.copytree(REPO_ROOT / "src" / "curamoir", root / "src" / "curamoir",
                    ignore=shutil.ignore_patterns("__pycache__"))
    return root


def settings_from(monkeypatch, **options):
    """Make the fix step build its --settings with pretooluse_settings'
    own seams (`root`, `installed`, `python`): the real function, other
    arguments."""
    monkeypatch.setattr(fix_step, "pretooluse_settings",
                        partial(fix_step.pretooluse_settings, **options))


def test_a_healthy_copy_of_the_checkout_starts_the_hosted_fix_step(repo, tmp_path, monkeypatch):
    """The control for the three breakages below: the copy, unbroken, passes
    the check, the agent starts, and the hook it loads is the copy's."""
    committed(repo)
    monkeypatch.setenv(VARIABLE, "sweep")
    root = plugin_copy(tmp_path)
    settings_from(monkeypatch, root=root)
    log = tmp_path / "hooks.json"
    script = stand_in_claude(tmp_path, SETTINGS_LOADING_CLAUDE.format(log=str(log)))

    run, before_calls = hosted_fix_step(repo, tmp_path, script)

    assert run.exit_code == 0 and before_calls == 1
    settings = json.loads(run.argv[run.argv.index("--settings") + 1])
    assert shlex.split(settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]) == [
        str(root / "scripts" / "curamoir-hook-pretooluse")]
    (answer,) = json.loads(log.read_text(encoding="utf-8"))["answers"]
    assert json.loads(answer["stdout"])["hookSpecificOutput"]["permissionDecision"] == "deny"


def _no_launcher_sh(root):
    (root / "src" / "curamoir" / "launcher.sh").unlink()


def _launcher_not_executable(root):
    (root / "scripts" / "curamoir-hook-pretooluse").chmod(0o644)


def _launcher_missing(root):
    (root / "scripts" / "curamoir-hook-pretooluse").unlink()


@pytest.mark.parametrize("breakage, exit_code", [
    (_no_launcher_sh, 0), (_launcher_not_executable, 126), (_launcher_missing, 127)],
    ids=["no-launcher.sh", "launcher-not-executable", "launcher-missing"])
def test_a_checkout_whose_hook_cannot_run_never_starts_a_fix_agent_with_bash(
        repo, tmp_path, monkeypatch, breakage, exit_code):
    """From a checkout the hook command is the launcher script. Without
    src/curamoir/launcher.sh it exits 0 in silence; without its exec bit, or
    missing, the shell exits 126 or 127: each is a hook that allows
    `rm -rf`. The fix step is refused, and says what came back."""
    committed(repo)
    monkeypatch.setenv(VARIABLE, "sweep")
    root = plugin_copy(tmp_path)
    breakage(root)
    settings_from(monkeypatch, root=root)
    script, marker = started_marker(tmp_path)

    message = refused_hosted_fix_step(repo, tmp_path, script, marker, "did not deny")

    assert "curamoir-hook-pretooluse" in message
    assert f"exit code {exit_code}" in message


@pytest.mark.parametrize("python3", [None, "#!/bin/sh\nexit 1\n"],
                         ids=["no-python", "python3-below-the-floor"])
def test_a_path_with_no_qualifying_python_never_starts_a_fix_agent_with_bash(
        repo, tmp_path, monkeypatch, python3):
    """The sweep's own interpreter need not be on PATH (an editable install
    started as /path/.venv/bin/curamoir), and the launcher takes its Python
    from PATH: with none at 3.12 or later there (none at all, or a `python3`
    whose floor probe fails) it exits 0 in silence. The repository's real
    launcher, a PATH that holds only what it and git need."""
    committed(repo)
    monkeypatch.setenv(VARIABLE, "sweep")
    script, marker = started_marker(tmp_path)
    bare = tmp_path / "bare-bin"
    bare.mkdir()
    for name in ("env", "bash", "sh", "dirname", "git"):
        (bare / name).symlink_to(shutil.which(name))
    if python3 is not None:
        (bare / "python3").write_text(python3, encoding="utf-8")
        (bare / "python3").chmod(0o755)
    monkeypatch.setenv("PATH", str(bare))

    message = refused_hosted_fix_step(repo, tmp_path, script, marker, "did not deny")

    assert "exit code 0" in message and "curamoir-hook-pretooluse" in message


def test_the_installed_hook_command_is_checked_through_the_fix_steps_spawn(
        repo, tmp_path, monkeypatch):
    """The installed form (`python -I -m curamoir.hooks.pretooluse`) through
    the real run_fix_step, not only at the command level: it passes the
    check, the agent starts, and that command denies in the clone. (The
    interpreter is the test's own; tests/plugin/test_installed_package.py
    builds a real wheel.)"""
    committed(repo)
    monkeypatch.setenv(VARIABLE, "sweep")
    settings_from(monkeypatch, installed=True, python=sys.executable)
    log = tmp_path / "hooks.json"
    script = stand_in_claude(tmp_path, SETTINGS_LOADING_CLAUDE.format(log=str(log)))

    run, before_calls = hosted_fix_step(repo, tmp_path, script)

    assert run.exit_code == 0 and before_calls == 1
    settings = json.loads(run.argv[run.argv.index("--settings") + 1])
    assert shlex.split(settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]) == [
        sys.executable, "-I", "-m", "curamoir.hooks.pretooluse"]
    (answer,) = json.loads(log.read_text(encoding="utf-8"))["answers"]
    assert json.loads(answer["stdout"])["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_an_installed_hook_that_imports_but_says_nothing_never_starts_the_agent(
        repo, tmp_path, monkeypatch):
    """The installed form's import probe passes for an interpreter that can
    import the module and still does not answer as the hook (here a stand-in
    that runs this interpreter for `-c` and exits 0 in silence for `-m`):
    only running the command shows it, so the check does."""
    committed(repo)
    monkeypatch.setenv(VARIABLE, "sweep")
    python = tmp_path / "python"
    python.write_text(
        "#!/bin/sh\nfor a in \"$@\"; do [ \"$a\" = -m ] && exit 0; done\n"
        f"exec {shlex.quote(sys.executable)} \"$@\"\n", encoding="utf-8")
    python.chmod(0o755)
    settings_from(monkeypatch, installed=True, python=str(python))
    script, marker = started_marker(tmp_path)

    message = refused_hosted_fix_step(repo, tmp_path, script, marker, "did not deny")

    assert "-I -m curamoir.hooks.pretooluse" in message and "exit code 0" in message


@pytest.mark.parametrize("value", [None, ""], ids=["unset", "empty"])
def test_a_fix_agent_with_bash_is_refused_outside_a_job(repo, tmp_path, monkeypatch, value):
    """The hook denies only when CURAMOIR_JOB is set and not empty: a fix
    step started outside run_guarded would load a denylist that allows
    everything. Refused before the agent starts."""
    committed(repo)
    if value is not None:
        monkeypatch.setenv(VARIABLE, value)
    script, marker = started_marker(tmp_path)

    refused_hosted_fix_step(repo, tmp_path, script, marker, VARIABLE)


@pytest.mark.parametrize("runner", [LOCAL_RUNNER, HOSTED], ids=["local", "hosted"])
def test_without_bash_the_fix_step_starts_as_before_whatever_the_hook_would_say(
        repo, tmp_path, monkeypatch, runner):
    """The hook's matcher is Bash, so without Bash in the toolset it can
    never fire: no CURAMOIR_JOB is required and the hook command is not run
    first (here it is a copy with no launcher, which the check would
    refuse). The local runner and the hosted run with no test command."""
    committed(repo)
    root = plugin_copy(tmp_path)
    _launcher_missing(root)
    settings_from(monkeypatch, root=root)
    script, marker = started_marker(tmp_path)

    with prepare_sandbox(repo.repo, "HEAD", parent=tmp_path) as sandbox:
        run = run_fix_step(sandbox, "p", model="m", claude=str(script), runner=runner,
                           tools=fix_tools(runner, None))

    assert run.exit_code == 0 and marker.exists()
    assert "Bash" not in run.argv[run.argv.index("--tools") + 1]


# --- what the check promises, held one property at a time (cm-60c, R5 edge cases 2) -------
#
# Stand-in hooks: a plugin root under tmp_path whose scripts/ holds small
# shell scripts, and a hooks.json naming them, both reached through the
# seams the tests above use (pretooluse_settings' `root`, and
# plugin_files.HOOKS_JSON). The repository's own hooks.json and launchers
# are never touched.

#: What cúramóir's hook prints for a refusal (hooks/pretooluse.py's _deny),
#: with a stand-in reason.
DENY = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                               "permissionDecisionReason": "stand-in"}}


def printing(answer):
    """Shell that reads the payload and prints `answer` as JSON."""
    return f"cat >/dev/null\nprintf '%s' {shlex.quote(json.dumps(answer))}\n"


def stand_in_hooks(tmp_path, monkeypatch, scripts, entries=None):
    """Make the fix step load stand-in hooks: `scripts` maps a name to the
    body of a /bin/sh script at <root>/scripts/<name>; `entries` is
    hooks.json's PreToolUse list (default: one Bash entry with a plain
    command hook per script, timeout 5). Returns the root."""
    from curamoir import plugin_files

    root = tmp_path / "stand-in plugin"
    (root / "scripts").mkdir(parents=True)
    for name, body in scripts.items():
        (root / "scripts" / name).write_text(f"#!/bin/sh\n{body}", encoding="utf-8")
        (root / "scripts" / name).chmod(0o755)
    if entries is None:
        entries = [{"matcher": "Bash", "hooks": [stand_in_hook(name) for name in scripts]}]
    registered = tmp_path / "stand-in-hooks.json"
    registered.write_text(json.dumps({"hooks": {"PreToolUse": entries}}), encoding="utf-8")
    monkeypatch.setattr(plugin_files, "HOOKS_JSON", registered)
    settings_from(monkeypatch, root=root, installed=False)
    return root


def stand_in_hook(name, timeout=5):
    """hooks.json's hook object for stand_in_hooks' script `name`."""
    return {"type": "command", "command": f'"${{CLAUDE_PLUGIN_ROOT}}"/scripts/{name}',
            "timeout": timeout}


def hosted_fix_step_in(sandbox, script, prompt="p", model="m"):
    """The hosted fix step with Bash in `sandbox`, for a test that needs
    the clone's path."""
    return run_fix_step(sandbox, prompt, model=model, claude=str(script), runner=HOSTED,
                        tools=fix_tools(HOSTED, "pytest -q"), test_command="pytest -q")


def test_the_hook_is_asked_in_the_clone_with_the_fix_steps_environment(
        repo, tmp_path, monkeypatch):
    """The user's ruling: a deny in the fix step's environment. The stand-in
    hook denies only when its working directory is the clone and it sees
    what run_fix_step built, not what the sweep's own process has:
    CLAUDE_PLUGIN_ROOT as PLUGIN_ROOT (the parent's names another plugin)
    and the hosted UV_FROZEN (absent from the parent). Asked anywhere else,
    or with the parent's environment, it is silent and the step is refused."""
    committed(repo)
    monkeypatch.setenv(VARIABLE, "sweep")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path / "some-other-plugin"))
    monkeypatch.delenv("UV_FROZEN", raising=False)
    root = stand_in_hooks(tmp_path, monkeypatch, {})
    script, marker = started_marker(tmp_path)

    with prepare_sandbox(repo.repo, "HEAD", parent=tmp_path) as sandbox:
        (root / "scripts" / "particular").write_text(
            "#!/bin/sh\ncat >/dev/null\n"
            'echo "cwd=$(pwd -P) CLAUDE_PLUGIN_ROOT=$CLAUDE_PLUGIN_ROOT UV_FROZEN=$UV_FROZEN" >&2\n'
            f'[ "$(pwd -P)" = {shlex.quote(os.path.realpath(sandbox.path))} ] || exit 0\n'
            f'[ "$CLAUDE_PLUGIN_ROOT" = {shlex.quote(str(fix_step.PLUGIN_ROOT))} ] || exit 0\n'
            '[ "$UV_FROZEN" = 1 ] || exit 0\n'
            f"printf '%s' {shlex.quote(json.dumps(DENY))}\n", encoding="utf-8")
        (root / "scripts" / "particular").chmod(0o755)
        (tmp_path / "stand-in-hooks.json").write_text(json.dumps({"hooks": {"PreToolUse": [
            {"matcher": "Bash", "hooks": [stand_in_hook("particular")]}]}}), encoding="utf-8")

        run = hosted_fix_step_in(sandbox, script)

    assert run.exit_code == 0 and marker.exists()


def test_the_hook_is_asked_with_the_payload_claude_code_sends(repo, tmp_path, monkeypatch):
    """What the hook reads on stdin is a PreToolUse payload for the Bash
    self-test command, `cwd` (the clone) included: cúramóir's hook finds
    its repository from that field."""
    committed(repo)
    monkeypatch.setenv(VARIABLE, "sweep")
    log = tmp_path / "payload.json"
    stand_in_hooks(tmp_path, monkeypatch, {"recording": (
        f"cat >{shlex.quote(str(log))}\nprintf '%s' {shlex.quote(json.dumps(DENY))}\n")})
    script, marker = started_marker(tmp_path)

    with prepare_sandbox(repo.repo, "HEAD", parent=tmp_path) as sandbox:
        run = hosted_fix_step_in(sandbox, script)
        clone = str(sandbox.path)

    assert run.exit_code == 0 and marker.exists()
    assert json.loads(log.read_text(encoding="utf-8")) == {
        "hook_event_name": "PreToolUse", "tool_name": "Bash",
        "tool_input": {"command": fix_step._SELF_TEST_COMMAND}, "cwd": clone}


def test_a_hook_slower_than_its_declared_timeout_is_refused(repo, tmp_path, monkeypatch):
    """Claude Code lets a hook that runs past its own `timeout` fail open,
    so that timeout bounds the check, not the longer fallback: a hook that
    denies after 2.5 s with a declared timeout of 1 is refused."""
    committed(repo)
    monkeypatch.setenv(VARIABLE, "sweep")
    assert fix_step._HOOK_SELF_TEST_TIMEOUT > 3
    stand_in_hooks(tmp_path, monkeypatch, {"slow": "sleep 2.5\n" + printing(DENY)}, entries=[
        {"matcher": "Bash", "hooks": [stand_in_hook("slow", timeout=1)]}])
    script, marker = started_marker(tmp_path)

    refused_hosted_fix_step(repo, tmp_path, script, marker, "did not answer within 1 seconds")


def test_a_prompt_and_a_model_named_like_the_settings_flag_do_not_hide_the_settings(
        repo, tmp_path, monkeypatch):
    """The settings the check reads back from the argv are the value of the
    LAST `--settings`: the prompt and the model come before the flag and
    may be that very word. With both, the real hook is still found, asked,
    and the agent starts."""
    committed(repo)
    monkeypatch.setenv(VARIABLE, "sweep")
    script, marker = started_marker(tmp_path)

    with prepare_sandbox(repo.repo, "HEAD", parent=tmp_path) as sandbox:
        run = hosted_fix_step_in(sandbox, script, prompt="--settings", model="--settings")

    assert run.exit_code == 0 and marker.exists()
    assert run.argv[2] == run.argv[4] == "--settings"
    assert fix_step._argv_settings(list(run.argv)) == fix_step.pretooluse_settings()


@pytest.mark.parametrize("entries", [
    [{"matcher": "Bash", "hooks": [stand_in_hook("denies"), stand_in_hook("silent")]}],
    [{"matcher": "Bash", "hooks": [stand_in_hook("denies")]},
     {"matcher": "Edit|Bash", "hooks": [stand_in_hook("silent")]}]],
    ids=["second-hook-of-the-entry", "second-entry"])
def test_every_hook_that_covers_bash_is_checked_not_only_the_first(
        repo, tmp_path, monkeypatch, entries):
    """Each hook is a command Claude Code runs for a Bash call, and the
    denylist is whichever of them cúramóir's is: one that denies beside one
    that says nothing is refused, and the refusal names the silent one."""
    committed(repo)
    monkeypatch.setenv(VARIABLE, "sweep")
    stand_in_hooks(tmp_path, monkeypatch,
                   {"denies": printing(DENY), "silent": "cat >/dev/null\n"}, entries=entries)
    script, marker = started_marker(tmp_path)

    message = refused_hosted_fix_step(repo, tmp_path, script, marker, "did not deny")

    assert "scripts/silent" in message and "exit code 0" in message


def _without(answer, key):
    return {"hookSpecificOutput": {name: value for name, value
                                   in answer["hookSpecificOutput"].items() if name != key}}


@pytest.mark.parametrize("body, exit_code", [
    (printing(DENY) + "exit 1\n", 1),
    (printing(_without(DENY, "hookEventName")), 0),
    (printing({"hookSpecificOutput": dict(DENY["hookSpecificOutput"],
                                          hookEventName="PostToolUse")}), 0),
    (printing([DENY]), 0)],
    ids=["deny-with-exit-1", "no-hookEventName", "another-hookEventName", "not-an-object"])
def test_a_deny_claude_code_would_not_honour_is_refused(
        repo, tmp_path, monkeypatch, body, exit_code):
    """Claude Code reads a hook's JSON only on exit 0, and a PreToolUse
    decision only from a `hookSpecificOutput` whose `hookEventName` is
    PreToolUse: a deny printed with exit 1, or without that name, would
    let the call through, so it does not pass the check either."""
    committed(repo)
    monkeypatch.setenv(VARIABLE, "sweep")
    stand_in_hooks(tmp_path, monkeypatch, {"almost": body})
    script, marker = started_marker(tmp_path)

    message = refused_hosted_fix_step(repo, tmp_path, script, marker, "did not deny")

    assert "scripts/almost" in message and f"exit code {exit_code}" in message


def test_the_stand_in_deny_is_what_the_real_hook_prints_for_the_self_test(repo, tmp_path):
    """DENY above stands in for cúramóir's hook: the real one, asked the
    check's own question inside a job, exits 0 with the same
    `hookEventName` and `permissionDecision`, so the stricter reading
    refuses no healthy run."""
    committed(repo)
    payload = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                          "tool_input": {"command": fix_step._SELF_TEST_COMMAND},
                          "cwd": str(repo.repo)})

    ran = subprocess.run([str(REPO_ROOT / "scripts" / "curamoir-hook-pretooluse")],
                         input=payload, capture_output=True, text=True, cwd=repo.repo,
                         env=dict(os.environ, **{VARIABLE: "sweep"}))

    answer = json.loads(ran.stdout)["hookSpecificOutput"]
    assert ran.returncode == 0, ran.stderr
    assert {key: answer[key] for key in ("hookEventName", "permissionDecision")} == {
        key: DENY["hookSpecificOutput"][key] for key in ("hookEventName", "permissionDecision")}
