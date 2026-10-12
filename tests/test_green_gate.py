"""E3-R4: the sweep runs the project's test command before opening a PR, and
never opens a red one (cm-fkd.5).

The gate runs inside the sandbox (cwd = the clone, so the fixed code is
what is tested) with the base's committed test command, reusing the source
checkout's environment read-only. On red it confirms the base is green, then
reduces scope one path at a time by restoring paths to their base content
(never `git checkout`/`reset`/`stash`/`clean`, E0-R5), and records which
strategy it used.

**Fixture provenance.** Most tests use small one-commit repositories whose
"test command" is a shell line committed in `.curamoir/curamoir.yaml`
(`! grep -rqs BROKEN src`: red exactly when a file under src holds BROKEN),
so which fix breaks the tests is decided by the test, not guessed. The
end-to-end test uses `fixtures/sample-repo` (tests/conftest.py,
committed_sample) with PLANTED.json's own `test_command` committed as the
config, the real lint, the real remedy, and the real review.
"""

import contextlib
import hashlib
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from curamoir import git_guard
from curamoir.config import ConfigError, load_config
from curamoir.fix_scope import ScopeVerdict, lint_sandbox, verify_fix
from curamoir.git_guard import prepare_sandbox, snapshot_git_state
from curamoir.green_gate import (
    ABANDONED,
    BASE_RED,
    CONFIG_INVALID,
    GREEN,
    NO_GREEN_SUBSET,
    NO_TEST_COMMAND,
    REDUCED,
    GateError,
    Trials,
    reduce_scope,
    resolve_test_command,
    run_test_gate,
    sandbox_environment,
)
from curamoir.job_run import JobRun
from curamoir.ledger import Ledger
from curamoir.remedies import apply_file_remedies
from tests.conftest import committed_sample
from tests.test_fix_scope import git_repo as files_repo

ROOT = Path(__file__).resolve().parents[1]
PLANTED = json.loads((ROOT / "fixtures" / "sample-repo" / "PLANTED.json").read_text("utf-8"))

RED_IF_BROKEN = "! grep -rqs BROKEN src"


def config(command, extra=""):
    return f"version: 1\nsweep:\n  test_command: {json.dumps(command)}\n{extra}"


def repo_of(tmp_path, files, command=None, extra=""):
    files = dict(files)
    if command is not None:
        files[".curamoir/curamoir.yaml"] = config(command, extra)
    return files_repo(tmp_path, files)


def counted(log, command=RED_IF_BROKEN):
    """`command`, appending a line to `log` each time it runs."""
    return f"echo run >> {log}; {command}"


def runs_in(log):
    return len(log.read_text().splitlines()) if log.exists() else 0


def fix(sandbox, edits):
    """Write `edits` (path -> text, or None to delete) into the sandbox and
    return the ok verdict a review of exactly those paths would give."""
    for path, text in edits.items():
        target = sandbox.path / path
        if text is None:
            target.unlink()
        else:
            target.write_text(text, encoding="utf-8")
    return ScopeVerdict(breaches=(), paths=tuple(sorted(edits)))


THREE = {"src/a.py": "a = 1\n", "src/b.py": "b = 1\n", "src/c.py": "c = 1\n"}


# --- green, reduced, abandoned ---------------------------------------------------------

def test_all_green_keeps_every_fix_after_one_run(tmp_path):
    log = tmp_path / "log"
    repo = repo_of(tmp_path, THREE, counted(log))
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        verdict = fix(sandbox, {"src/a.py": "a = 2\n", "src/b.py": "b = 2\n"})
        result = run_test_gate(sandbox, verdict)

        assert result.strategy == GREEN
        assert result.cause is None
        assert result.paths_for_pr == ("src/a.py", "src/b.py")
        assert result.dropped == ()
        assert result.command == counted(log)
        assert result.command_source == "config"
        assert runs_in(log) == 1 == len(result.runs)
        assert (sandbox.path / "src/a.py").read_text() == "a = 2\n"


def test_one_fix_that_breaks_the_tests_is_dropped_and_the_rest_kept(tmp_path):
    log = tmp_path / "log"
    repo = repo_of(tmp_path, THREE, counted(log))
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        verdict = fix(sandbox, {"src/a.py": "a = 2\n", "src/b.py": "BROKEN\n",
                                "src/c.py": "c = 2\n"})
        result = run_test_gate(sandbox, verdict)

        assert result.strategy == REDUCED
        assert result.paths_for_pr == ("src/a.py", "src/c.py")
        assert [d.path for d in result.dropped] == ["src/b.py"]
        assert "fail" in result.dropped[0].reason
        # The kept fixes stay; the dropped one is back to its base content.
        assert (sandbox.path / "src/a.py").read_text() == "a = 2\n"
        assert (sandbox.path / "src/c.py").read_text() == "c = 2\n"
        assert (sandbox.path / "src/b.py").read_text() == "b = 1\n"
        # The set handed to the PR was itself a green run.
        assert any(run.passed and run.paths == result.paths_for_pr for run in result.runs)
        # And the patch of exactly those paths applies to the base and is green.
        patch = tmp_path / "fix.patch"
        patch.write_bytes(sandbox.patch(result.paths_for_pr))
    subprocess.run(["git", "apply", str(patch)], cwd=repo, check=True)
    assert subprocess.run(["sh", "-c", RED_IF_BROKEN], cwd=repo).returncode == 0


def test_two_offenders_are_both_dropped(tmp_path):
    files = {f"src/m{i}.py": f"m{i} = 1\n" for i in range(5)}
    repo = repo_of(tmp_path, files, RED_IF_BROKEN)
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        verdict = fix(sandbox, {"src/m0.py": "m0 = 2\n", "src/m1.py": "BROKEN\n",
                                "src/m2.py": "m2 = 2\n", "src/m3.py": "BROKEN\n",
                                "src/m4.py": "m4 = 2\n"})
        result = run_test_gate(sandbox, verdict)

    assert result.strategy == REDUCED
    assert result.paths_for_pr == ("src/m0.py", "src/m2.py", "src/m4.py")
    assert sorted(d.path for d in result.dropped) == ["src/m1.py", "src/m3.py"]


def test_two_fixes_green_alone_but_red_together_keep_the_first(tmp_path):
    """An interaction: each fix passes alone, both together fail. The first
    (in path order) is kept, the second dropped; the PR is still green."""
    command = "! { grep -qs X src/a.py && grep -qs Y src/b.py; }"
    repo = repo_of(tmp_path, THREE, command)
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        verdict = fix(sandbox, {"src/a.py": "X\n", "src/b.py": "Y\n"})
        result = run_test_gate(sandbox, verdict)

    assert result.strategy == REDUCED
    assert result.paths_for_pr == ("src/a.py",)
    assert [d.path for d in result.dropped] == ["src/b.py"]


def test_a_red_base_abandons_the_sweep_with_no_paths(tmp_path):
    files = dict(THREE, **{"src/broken.py": "BROKEN\n"})
    repo = repo_of(tmp_path, files, RED_IF_BROKEN)
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        verdict = fix(sandbox, {"src/a.py": "a = 2\n"})
        result = run_test_gate(sandbox, verdict)

        assert result.strategy == ABANDONED
        assert result.cause == BASE_RED
        assert result.paths_for_pr == ()
        assert len(result.runs) == 2
        assert result.runs[1].paths == ()  # the second run was the base
        # Nothing of the fix is left in the tree.
        assert (sandbox.path / "src/a.py").read_text() == "a = 1\n"


def test_every_fix_red_on_a_green_base_abandons_with_no_green_subset(tmp_path):
    repo = repo_of(tmp_path, THREE, RED_IF_BROKEN)
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        verdict = fix(sandbox, {"src/a.py": "BROKEN\n", "src/b.py": "BROKEN\n"})
        result = run_test_gate(sandbox, verdict)

    assert result.strategy == ABANDONED
    assert result.cause == NO_GREEN_SUBSET
    assert result.paths_for_pr == ()
    assert sorted(d.path for d in result.dropped) == ["src/a.py", "src/b.py"]


def test_no_test_command_is_not_green(tmp_path):
    """Nothing configured and nothing detectable: E3-R4 says the tests MUST
    run before a PR, so no PR."""
    repo = repo_of(tmp_path, {"notes.txt": "hello\n"})
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        verdict = fix(sandbox, {"notes.txt": "hello there\n"})
        result = run_test_gate(sandbox, verdict)

    assert result.strategy == ABANDONED
    assert result.cause == NO_TEST_COMMAND
    assert result.paths_for_pr == ()
    assert result.runs == ()


def test_an_invalid_committed_config_runs_nothing(tmp_path):
    repo = repo_of(tmp_path, dict(THREE, **{".curamoir/curamoir.yaml": "sweep: [1\n"}))
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        verdict = fix(sandbox, {"src/a.py": "a = 2\n"})
        result = run_test_gate(sandbox, verdict)

    assert result.strategy == ABANDONED
    assert result.cause == CONFIG_INVALID
    assert result.runs == ()


def test_the_command_is_the_bases_never_the_edited_trees(tmp_path):
    """A fix that rewrites the config in the sandbox to `true` does not make
    a red tree green: the command is read from the base commit."""
    repo = repo_of(tmp_path, THREE, RED_IF_BROKEN)
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        verdict = fix(sandbox, {"src/a.py": "BROKEN\n",
                                ".curamoir/curamoir.yaml": config("true")})
        result = run_test_gate(sandbox, verdict)

    assert result.command == RED_IF_BROKEN
    assert "src/a.py" not in result.paths_for_pr


def test_only_the_paths_passed_are_tested_and_the_rest_are_at_base(tmp_path):
    """The runner may trim the verdict (max_files_per_pr) before the gate;
    the paths it leaves out are at their base content for every run, so the
    tested tree is exactly the PR."""
    log = tmp_path / "log"
    repo = repo_of(tmp_path, THREE, counted(log))
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        verdict = fix(sandbox, {"src/a.py": "a = 2\n", "src/b.py": "BROKEN\n"})
        result = run_test_gate(sandbox, verdict, paths=("src/a.py",))

        assert result.strategy == GREEN
        assert result.paths_for_pr == ("src/a.py",)
        assert runs_in(log) == 1
        assert (sandbox.path / "src/b.py").read_text() == "b = 1\n"


def test_a_verdict_that_is_not_ok_or_changed_nothing_is_refused(tmp_path):
    repo = repo_of(tmp_path, THREE, RED_IF_BROKEN)
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        with pytest.raises(ValueError):
            run_test_gate(sandbox, ScopeVerdict(breaches=(), paths=()))
        bad = ScopeVerdict(breaches=(object(),), paths=("src/a.py",))
        with pytest.raises(ValueError):
            run_test_gate(sandbox, bad)
        with pytest.raises(ValueError):
            run_test_gate(sandbox, ScopeVerdict(breaches=(), paths=("src/a.py",)),
                          paths=("src/z.py",))


def test_a_checkout_is_not_a_sandbox(tmp_path):
    repo = repo_of(tmp_path, THREE, RED_IF_BROKEN)
    with pytest.raises(GateError):
        run_test_gate(repo, ScopeVerdict(breaches=(), paths=("src/a.py",)))


def test_a_deleted_file_is_a_fix_that_can_be_dropped(tmp_path):
    """A remedy's deletion (handlers_v2.py) is restored like any edit."""
    files = dict(THREE, **{"src/v2.py": "v = 1\n"})
    # Red while src/v2.py is missing.
    repo = repo_of(tmp_path, files, "test -f src/v2.py")
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        verdict = fix(sandbox, {"src/a.py": "a = 2\n", "src/v2.py": None})
        result = run_test_gate(sandbox, verdict)

        assert result.paths_for_pr == ("src/a.py",)
        assert [d.path for d in result.dropped] == ["src/v2.py"]
        assert (sandbox.path / "src/v2.py").read_text() == "v = 1\n"


def test_an_executable_bit_is_restored_with_the_base_content(tmp_path):
    repo = repo_of(tmp_path, THREE, RED_IF_BROKEN)
    (repo / "src/a.py").chmod(0o755)
    subprocess.run(["git", "commit", "-qam", "exec"], cwd=repo, check=True)
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        target = sandbox.path / "src/a.py"
        verdict = fix(sandbox, {"src/a.py": "BROKEN\n", "src/b.py": "b = 2\n"})
        target.chmod(0o644)
        run_test_gate(sandbox, verdict)

        assert target.read_text() == "a = 1\n"
        assert os.stat(target).st_mode & stat.S_IXUSR


def test_a_command_that_cannot_run_is_not_a_red_base(tmp_path):
    repo = repo_of(tmp_path, THREE, "no-such-test-runner-xyz -q")
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        result = run_test_gate(sandbox, fix(sandbox, {"src/a.py": "a = 2\n"}))

    assert result.strategy == ABANDONED and result.cause == "command_not_runnable"
    assert "no-such-test-runner-xyz" in result.why


def test_reverting_an_added_file_leaves_no_empty_directory(tmp_path):
    repo = repo_of(tmp_path, THREE, RED_IF_BROKEN)
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        (sandbox.path / "src/new/deep").mkdir(parents=True)
        verdict = fix(sandbox, {"src/new/deep/m.py": "BROKEN\n", "src/a.py": "a = 2\n"})
        result = run_test_gate(sandbox, verdict)

        assert result.kept == ("src/a.py",)
        assert not (sandbox.path / "src/new").exists()
        assert (sandbox.path / "src").is_dir()


@pytest.mark.parametrize("message", ["/usr/bin/python3: No module named pytest",
                                     "sh: jest: command not found"])
def test_a_missing_test_runner_that_exits_1_is_not_a_red_base(tmp_path, message):
    repo = repo_of(tmp_path, THREE, f"echo '{message}' >&2; exit 1")
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        result = run_test_gate(sandbox, fix(sandbox, {"src/a.py": "a = 2\n"}))
    assert result.strategy == ABANDONED and result.cause == "command_not_runnable"
    assert message in result.why


# --- hardening ------------------------------------------------------------------------

def test_a_timeout_is_red_and_kills_the_process_group(tmp_path):
    pids = tmp_path / "pids"
    command = f"sleep 30 & echo $! >> {pids}; wait"
    repo = repo_of(tmp_path, THREE, command, extra="  test_timeout_seconds: 1\n")
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        verdict = fix(sandbox, {"src/a.py": "a = 2\n"})
        started = time.monotonic()
        result = run_test_gate(sandbox, verdict)

    assert time.monotonic() - started < 20
    # Timing out at the base and with the fix says the budget is too small,
    # not that the base is red.
    assert result.strategy == ABANDONED and result.cause == "test_timeout"
    assert [run.outcome for run in result.runs] == ["timed_out", "timed_out"]
    for pid in map(int, pids.read_text().split()):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            pytest.fail(f"the test command's child {pid} outlived its timeout")


def tree_digest(root):
    found = {}
    for directory, _, files in os.walk(root):
        for name in files:
            path = Path(directory) / name
            found[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return found


def test_the_command_runs_in_the_sandbox_with_the_sources_environment(tmp_path, monkeypatch):
    out = tmp_path / "seen"
    command = ('printf "%s\\n" "$VIRTUAL_ENV" "$(pwd -P)" "$(command -v python3)" '
               '"$NODE_PATH" "$PYTHONDONTWRITEBYTECODE" "${GIT_DIR-unset}" '
               f'"${{PYTHONHOME-unset}}" "$(command -v jest)" > {out}')
    repo = repo_of(tmp_path, THREE, command)
    venv = repo / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\n")
    for tool in (venv / "bin" / "python3", repo / "node_modules" / ".bin" / "jest"):
        tool.parent.mkdir(parents=True, exist_ok=True)
        tool.write_text("#!/bin/sh\nexit 0\n")
        tool.chmod(0o755)
    before = tree_digest(repo)
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        monkeypatch.setenv("GIT_DIR", "/nonexistent")
        monkeypatch.setenv("PYTHONHOME", "/bogus")
        verdict = fix(sandbox, {"src/a.py": "a = 2\n"})
        run_test_gate(sandbox, verdict)
        cwd = os.path.realpath(sandbox.path)

    (virtual_env, seen_cwd, python, node_path, no_bytecode, git_dir, python_home,
     jest) = out.read_text().splitlines()
    assert virtual_env == str(venv)
    assert seen_cwd == cwd
    assert python == str(venv / "bin" / "python3")
    assert node_path.split(os.pathsep)[0] == str(repo / "node_modules")
    assert no_bytecode == "1"
    assert git_dir == "unset"
    assert python_home == "unset"
    assert jest == str(repo / "node_modules" / ".bin" / "jest")
    assert tree_digest(repo) == before, "the user's checkout was written"


def test_without_a_source_venv_the_allowlist_keeps_the_inherited_virtual_env_and_path(tmp_path, monkeypatch):
    monkeypatch.setenv("VIRTUAL_ENV", "/runner/venv")
    env = sandbox_environment(tmp_path)
    assert env["VIRTUAL_ENV"] == "/runner/venv"
    assert env["PATH"] == os.environ["PATH"]
    assert "NODE_PATH" not in env or env["NODE_PATH"] == os.environ.get("NODE_PATH")


def test_scope_reduction_never_uses_a_denylisted_git_command(tmp_path, monkeypatch):
    repo = repo_of(tmp_path, THREE, RED_IF_BROKEN)
    calls = []
    real = git_guard._run_git

    def spy(repo_path, args, timeout, extra_env=None):
        calls.append(list(args))
        return real(repo_path, args, timeout, extra_env)

    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        monkeypatch.setattr(git_guard, "_run_git", spy)
        verdict = fix(sandbox, {"src/a.py": "a = 2\n", "src/b.py": "BROKEN\n",
                                "src/c.py": None})
        result = run_test_gate(sandbox, verdict)
        sandbox.patch(result.paths_for_pr)

    assert result.strategy == REDUCED
    assert calls, "the spy saw no git at all"
    denied = {"checkout", "reset", "stash", "clean", "restore", "rm"}
    assert not [args for args in calls if denied & set(args)]


def test_a_test_command_that_writes_git_files_is_refused(tmp_path):
    command = "mkdir -p .git/hooks && touch .git/hooks/pre-commit"
    repo = repo_of(tmp_path, THREE, command)
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        verdict = fix(sandbox, {"src/a.py": "a = 2\n"})
        with pytest.raises(GateError, match="hooks"):
            run_test_gate(sandbox, verdict)


def test_a_test_command_that_edits_a_fixed_file_does_not_reach_the_pr(tmp_path):
    repo = repo_of(tmp_path, THREE, "echo tampered >> src/a.py")
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        verdict = fix(sandbox, {"src/a.py": "a = 2\n"})
        result = run_test_gate(sandbox, verdict)

        assert result.strategy == GREEN
        assert (sandbox.path / "src/a.py").read_text() == "a = 2\n"
        assert b"tampered" not in sandbox.patch(result.paths_for_pr)


def venv_with_pth(repo, pth_text):
    """A real venv in the SOURCE checkout `repo` whose site-packages holds a
    `.pth` with `pth_text`, which is what an editable install leaves."""
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(repo / ".venv")],
                   check=True, capture_output=True)
    site = next((repo / ".venv" / "lib").glob("python*/site-packages"))
    (site / "__editable__pkg.pth").write_text(pth_text, encoding="utf-8")


PKG_TEST = "python3 -c 'import pkg, sys; sys.exit(0 if pkg.x == 1 else 1)'"


PKG = {"src/pkg/__init__.py": "x = 1\n", "src/pkg/other.py": "y = 1\n", "pyproject.toml": ""}


def test_an_editable_install_of_the_checkout_does_not_test_the_unfixed_code(tmp_path):
    """A plain path `.pth` (pip -e, hatch's `_editable_impl_*`) puts the
    CHECKOUT's src on sys.path; the sandbox's src on PYTHONPATH comes first."""
    repo = repo_of(tmp_path, PKG, PKG_TEST)
    venv_with_pth(repo, str(repo / "src") + "\n")
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        verdict = fix(sandbox, {"src/pkg/__init__.py": "x = 2\n", "src/pkg/other.py": "y = 2\n"})
        result = run_test_gate(sandbox, verdict)

    assert result.strategy == REDUCED, [run.tail for run in result.runs]
    assert result.kept == ("src/pkg/other.py",)


def test_an_import_that_still_resolves_to_the_checkout_abandons_the_sweep(tmp_path):
    """An editable hook that puts the checkout FIRST on sys.path (old
    setuptools' easy-install.pth does) beats PYTHONPATH: the canary sees it."""
    repo = repo_of(tmp_path, PKG, PKG_TEST)
    venv_with_pth(repo, f"import sys; sys.path.insert(0, {str(repo / 'src')!r})\n")
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        result = run_test_gate(sandbox, fix(sandbox, {"src/pkg/__init__.py": "x = 2\n"}))

    assert result.strategy == ABANDONED
    assert result.cause == "imports_the_checkout"
    assert "pkg" in result.why and str(repo) in result.why
    assert result.runs == () and result.paths_for_pr == ()


ALLOWED = {"PATH", "HOME", "TMPDIR", "USER", "LOGNAME", "LANG", "TZ", "VIRTUAL_ENV",
           "NODE_PATH", "PYTHONPATH", "PYTHONDONTWRITEBYTECODE", "CI", "PYTHONUSERBASE",
           # version managers' roots: directories a shim on PATH needs
           "PYENV_ROOT", "ASDF_DATA_DIR", "ASDF_DIR", "VOLTA_HOME", "NVM_DIR",
           # set by /bin/sh itself
           "PWD", "OLDPWD", "SHLVL", "_"}


def test_the_test_command_sees_only_an_allowlisted_environment(tmp_path, monkeypatch):
    seen = tmp_path / "env"
    command = f'env > {seen}; echo "$HOME" > {seen}.home; echo "$TMPDIR" > {seen}.tmp; ' \
              'touch "$HOME/x" "$TMPDIR/y"'
    repo = repo_of(tmp_path, THREE, command)
    for name, value in {"CURAMOIR_PROBE_TOKEN": "s3cret-a", "AWS_SECRET_ACCESS_KEY": "s3cret-b",
                        "SSH_AUTH_SOCK": "/s3cret-c", "GH_TOKEN": "s3cret-d",
                        "ANTHROPIC_API_KEY": "s3cret-e", "LC_ALL": "C"}.items():
        monkeypatch.setenv(name, value)
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        result = run_test_gate(sandbox, fix(sandbox, {"src/a.py": "a = 2\n"}))
        root = os.path.realpath(sandbox.path)

    assert result.strategy == GREEN
    text = seen.read_text()
    keys = {line.split("=", 1)[0] for line in text.splitlines() if "=" in line}
    assert keys - ALLOWED - {k for k in keys if k.startswith("LC_")} == set()
    assert "s3cret" not in text
    assert "CI=true" in text.splitlines()
    for dump in (f"{seen}.home", f"{seen}.tmp"):
        made = Path(Path(dump).read_text().strip())
        assert made.is_absolute() and not made.exists(), f"{made} was left behind"
        assert not str(os.path.realpath(made)).startswith(root)


def test_a_test_command_that_locks_a_directory_is_a_refusal_not_an_oserror(tmp_path):
    repo = repo_of(tmp_path, THREE, "chmod 000 src; true")
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        verdict = fix(sandbox, {"src/a.py": "a = 2\n"})
        with pytest.raises(GateError):
            run_test_gate(sandbox, verdict)


@pytest.mark.parametrize("command", ["rm -rf src; true", "echo x >> src/c.py; true"])
def test_a_test_that_changes_a_file_outside_the_fix_is_refused(tmp_path, command):
    repo = repo_of(tmp_path, THREE, command)
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        verdict = fix(sandbox, {"src/a.py": "a = 2\n"})
        with pytest.raises(GateError, match=r"src/c\.py"):
            run_test_gate(sandbox, verdict)


def test_files_a_test_leaves_behind_do_not_reach_the_next_run(tmp_path):
    command = f"test ! -e leftover && mkdir -p cache/d && touch leftover cache/d/f && {RED_IF_BROKEN}"
    repo = repo_of(tmp_path, THREE, command)
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        result = run_test_gate(sandbox, fix(sandbox, {"src/a.py": "a = 2\n",
                                                      "src/b.py": "BROKEN\n"}))
        assert not (sandbox.path / "leftover").exists()
        assert not (sandbox.path / "cache").exists()

    assert result.strategy == REDUCED and result.kept == ("src/a.py",)


def test_a_large_output_keeps_separate_bounded_tails(tmp_path):
    import resource
    command = ("head -c 50000000 /dev/zero | tr '\\0' a; echo; echo OUT-END; "
               "head -c 5000000 /dev/zero | tr '\\0' b >&2; echo ERR-END >&2; true")
    repo = repo_of(tmp_path, THREE, command)
    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        result = run_test_gate(sandbox, fix(sandbox, {"src/a.py": "a = 2\n"}))
    grown = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before
    grown_bytes = grown if sys.platform == "darwin" else grown * 1024

    (run,) = result.runs
    assert run.stdout_tail.endswith("OUT-END\n") and "b" not in run.stdout_tail
    assert run.stderr_tail.endswith("ERR-END\n") and "a" not in run.stderr_tail
    assert len(run.stdout_tail) <= 4000 and len(run.stderr_tail) <= 4000
    assert grown_bytes < 30_000_000, f"the gate's memory grew by {grown_bytes} bytes"


def test_a_daemon_that_keeps_the_output_open_does_not_hold_the_run(tmp_path):
    pid_file = tmp_path / "daemon.pid"
    daemon = (f"perl -e 'use POSIX; my $p = fork; if ($p) {{ open F, \">{pid_file}\"; "
              "print F $p; exit } POSIX::setsid(); sleep 30'; true")
    repo = repo_of(tmp_path, THREE, daemon)
    try:
        with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
            started = time.monotonic()
            result = run_test_gate(sandbox, fix(sandbox, {"src/a.py": "a = 2\n"}))
        assert time.monotonic() - started < 15
        assert result.strategy == GREEN
    finally:
        if pid_file.exists():
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(pid_file.read_text()), 9)


USER_BASE = "import site, sys; sys.stdout.write(site.getuserbase())"


@pytest.mark.parametrize("inherited", [None, "set"])
def test_python3s_user_base_is_kept_despite_the_fresh_home(tmp_path, monkeypatch, inherited):
    """`pip install --user` puts pytest under the user base, which python
    derives from HOME; the fresh HOME would hide it, so the gate passes the
    user base python3 reports in the sweep's own environment."""
    if inherited:
        monkeypatch.setenv("PYTHONUSERBASE", str(tmp_path / "userbase"))
    else:
        monkeypatch.delenv("PYTHONUSERBASE", raising=False)
    expected = subprocess.run(["python3", "-c", USER_BASE], capture_output=True, text=True,
                              check=True).stdout
    out = tmp_path / "seen"
    repo = repo_of(tmp_path, THREE, f'printf "%s" "$PYTHONUSERBASE" > {out}')
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        run_test_gate(sandbox, fix(sandbox, {"src/a.py": "a = 2\n"}))
    assert out.read_text() == expected
    if inherited:
        assert expected == str(tmp_path / "userbase")


def test_a_user_site_module_imports_in_the_test_run(tmp_path, monkeypatch):
    enabled = subprocess.run(["python3", "-c", "import site, sys; sys.exit(not site.ENABLE_USER_SITE)"])
    if enabled.returncode:
        pytest.skip("the python3 on PATH has the user site disabled (a venv's)")
    monkeypatch.setenv("PYTHONUSERBASE", str(tmp_path / "userbase"))
    site_dir = Path(subprocess.run(["python3", "-c", "import site, sys; "
                                    "sys.stdout.write(site.getusersitepackages())"],
                                   capture_output=True, text=True, check=True).stdout)
    site_dir.mkdir(parents=True)
    (site_dir / "curamoir_user_probe.py").write_text("ok = 1\n")
    repo = repo_of(tmp_path, THREE, "python3 -c 'import curamoir_user_probe'")
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        result = run_test_gate(sandbox, fix(sandbox, {"src/a.py": "a = 2\n"}))
    assert result.strategy == GREEN, [run.tail for run in result.runs]


MOD = {"src/mod.py": "x = 1\n", "pyproject.toml": ""}


MOD_TEST = "python3 -c 'import mod, sys; sys.exit(0 if mod.x == 1 else 1)'"


NS = {"src/pkg/core.py": "x = 1\n", "pyproject.toml": ""}


NS_TEST = "python3 -c 'import pkg.core as m, sys; sys.exit(0 if m.x == 1 else 1)'"


@pytest.mark.parametrize("files, command, edits", [
    (MOD, MOD_TEST, {"src/mod.py": "x = 2\n"}),
    (NS, NS_TEST, {"src/pkg/core.py": "x = 2\n"}),
], ids=["single-module", "namespace-package"])
def test_a_checkout_first_on_sys_path_is_refused_with_no_package_to_name(
        tmp_path, files, command, edits):
    """No `__init__.py` to name, so the import check has nothing to import:
    the sys.path check still sees the checkout's src ahead of the sandbox's."""
    repo = repo_of(tmp_path, files, command)
    venv_with_pth(repo, f"import sys; sys.path.insert(0, {str(repo / 'src')!r})\n")
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        result = run_test_gate(sandbox, fix(sandbox, edits))
    assert result.strategy == ABANDONED and result.cause == "imports_the_checkout", result.canary
    assert str(repo / "src") in result.why
    assert result.runs == ()


def test_a_checkout_appended_to_sys_path_passes_the_canary(tmp_path):
    repo = repo_of(tmp_path, MOD, MOD_TEST)
    venv_with_pth(repo, str(repo / "src") + "\n")
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        result = run_test_gate(sandbox, fix(sandbox, {"src/mod.py": "x = 2\n"}))
    # The sandbox's mod (x = 2) was tested, and is red.
    assert result.cause == NO_GREEN_SUBSET, result.canary
    assert result.canary.startswith("checked: sys.path")


def test_a_kept_file_rewritten_after_the_gate_is_never_patched(tmp_path):
    """A setsid() daemon a test left behind rewrites a kept file after the
    green run: the patch must carry what was tested or nothing."""
    daemon = ("perl -e 'use POSIX; if (!fork) { POSIX::setsid(); sleep 1; "
              "open F, \">src/a.py\"; print F \"BROKEN\\n\"; exit }'; " + RED_IF_BROKEN)
    repo = repo_of(tmp_path, THREE, daemon)
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        result = run_test_gate(sandbox, fix(sandbox, {"src/a.py": "a = 2\n"}))
        assert result.strategy == GREEN
        deadline = time.monotonic() + 10
        while "BROKEN" not in (sandbox.path / "src/a.py").read_text():
            assert time.monotonic() < deadline, "the daemon never wrote"
            time.sleep(0.05)
        with pytest.raises(GateError, match=r"src/a\.py"):
            result.patch(sandbox)


def test_the_gates_patch_is_the_kept_paths_patch(tmp_path):
    repo = repo_of(tmp_path, THREE, RED_IF_BROKEN)
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        result = run_test_gate(sandbox, fix(sandbox, {"src/a.py": "a = 2\n",
                                                      "src/b.py": "BROKEN\n", "src/c.py": None}))
        patch = result.patch(sandbox)
        assert patch == sandbox.patch(("src/a.py", "src/c.py"))
    assert b"a = 2" in patch and b"BROKEN" not in patch


def test_output_past_the_disk_cap_is_killed_and_red(tmp_path, monkeypatch):
    from curamoir import green_gate
    monkeypatch.setattr(green_gate, "_OUTPUT_CAP", 1_000_000)
    repo = repo_of(tmp_path, THREE, "head -c 20000000 /dev/zero | tr '\\0' a; sleep 5; true")
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        started = time.monotonic()
        result = run_test_gate(sandbox, fix(sandbox, {"src/a.py": "a = 2\n"}))
    assert time.monotonic() - started < 8
    assert result.strategy == ABANDONED
    assert result.runs[0].outcome == "failed" and result.runs[0].code is None
    assert "output" in result.runs[0].stderr_tail


def test_a_rewrite_that_restores_size_and_mtime_is_still_drift(tmp_path):
    """The drift walk hashes only entries whose stat changed; ctime, which
    no process can set back, is part of that stat."""
    rewrite = (f"{sys.executable} -c \"import os; s = os.stat('src/c.py'); "
               "open('src/c.py', 'w').write('c = 9\\n'); "
               "os.utime('src/c.py', ns=(s.st_atime_ns, s.st_mtime_ns))\"; true")
    repo = repo_of(tmp_path, THREE, rewrite)
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        verdict = fix(sandbox, {"src/a.py": "a = 2\n"})
        with pytest.raises(GateError, match=r"src/c\.py"):
            run_test_gate(sandbox, verdict)


# --- the reduction, bounded -----------------------------------------------------------

@pytest.mark.parametrize("offender", range(20))
def test_one_offender_among_max_files_per_pr_is_found_within_the_default_cap(offender):
    """The default cap (12) is chosen so a single offender among
    sweep.max_files_per_pr (20) fixes is always isolated."""
    candidates = tuple(f"f{i:02}" for i in range(20))
    bad = candidates[offender]
    trials = Trials(lambda subset: bad not in subset,
                    max_runs=load_config({})["sweep"]["max_test_runs"])
    assert trials(candidates) is False  # the full run
    assert trials(()) is True  # the base

    kept, dropped = reduce_scope(candidates, trials)

    assert kept == tuple(c for c in candidates if c != bad)
    assert [d.path for d in dropped] == [bad]
    assert trials.used <= 12


def test_reaching_the_cap_drops_what_was_not_tested():
    candidates = tuple(f"f{i}" for i in range(8))
    trials = Trials(lambda subset: "f0" not in subset and "f5" not in subset, max_runs=4)
    trials(candidates)
    trials(())

    kept, dropped = reduce_scope(candidates, trials)

    assert trials.used == 4
    assert "f0" not in kept and "f5" not in kept
    assert {d.path for d in dropped} | set(kept) == set(candidates)
    assert any("max_test_runs" in d.reason for d in dropped)


@pytest.mark.parametrize("max_runs", [2, 3])
def test_the_cap_reached_during_the_reduction_is_run_cap(tmp_path, max_runs):
    repo = repo_of(tmp_path, THREE, RED_IF_BROKEN, extra=f"  max_test_runs: {max_runs}\n")
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        result = run_test_gate(sandbox, fix(sandbox, {"src/a.py": "BROKEN\n",
                                                      "src/b.py": "b = 2\n"}))

    assert result.strategy == ABANDONED and result.cause == "run_cap"
    assert len(result.runs) == max_runs


# --- configuration and detection ------------------------------------------------------

def test_the_test_keys_are_validated_by_name():
    with pytest.raises(ConfigError) as caught:
        load_config({"sweep": {"test_command": 3}})
    assert "sweep.test_command" in str(caught.value)
    defaults = load_config({})["sweep"]
    assert defaults["test_command"] == ""
    assert defaults["max_test_runs"] == 12
    assert defaults["test_timeout_seconds"] == 600


@pytest.mark.parametrize("files, command", [
    ({"pyproject.toml": "[project]\n", "tests/test_x.py": "def test(): pass\n"},
     "python3 -m pytest -q"),
    ({"setup.py": "", "pkg/thing_test.py": "def test(): pass\n"}, "python3 -m pytest -q"),
    ({"package.json": json.dumps({"scripts": {"test": "jest"}})}, "npm test"),
])
def test_a_command_is_detected_from_the_base(tmp_path, files, command):
    repo = repo_of(tmp_path, files)
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        chosen = resolve_test_command(sandbox)
    assert chosen.command == command
    assert chosen.source == "detected"


@pytest.mark.parametrize("files", [
    {"pyproject.toml": "[project]\n"},  # Python, but no test file
    {"tests/test_x.py": "def test(): pass\n"},  # tests, but no Python project marker
    {"package.json": json.dumps({"scripts": {
        "test": 'echo "Error: no test specified" && exit 1'}})},  # npm's placeholder
    {"package.json": "{not json"},
    {"pyproject.toml": "", "tests/test_x.py": "", "package.json": json.dumps(
        {"scripts": {"test": "jest"}})},  # both: ambiguous
])
def test_nothing_is_guessed_when_detection_is_not_clear(tmp_path, files):
    repo = repo_of(tmp_path, files)
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        chosen = resolve_test_command(sandbox)
    assert chosen.command is None
    assert chosen.cause == NO_TEST_COMMAND
    assert chosen.why


def test_a_configured_command_wins_over_detection(tmp_path):
    repo = repo_of(tmp_path, {"pyproject.toml": "", "tests/test_x.py": ""}, "make check")
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        chosen = resolve_test_command(sandbox)
    assert (chosen.command, chosen.source) == ("make check", "config")


@pytest.mark.parametrize("seconds", [0, 86401, 10 ** 30])
def test_the_timeout_is_bounded_by_the_schema(seconds):
    with pytest.raises(ConfigError, match=r"sweep\.test_timeout_seconds"):
        load_config({"sweep": {"test_timeout_seconds": seconds}})


# --- the ledger -----------------------------------------------------------------------

def test_the_runner_records_the_strategy_on_the_sweeps_ledger_line(tmp_path):
    repo = repo_of(tmp_path, THREE, RED_IF_BROKEN)
    ledger = Ledger(tmp_path / "ledger.jsonl")
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        verdict = fix(sandbox, {"src/a.py": "a = 2\n", "src/b.py": "BROKEN\n"})
        result = run_test_gate(sandbox, verdict)
    with JobRun(ledger, "sweep") as run:
        run.succeeded("opened one PR", **result.ledger_fields())

    line = ledger.entries()[-1]
    assert line["outcome"] == "success"
    gate = line["test_gate"]
    assert gate["strategy"] == REDUCED
    assert gate["kept"] == ["src/a.py"]
    assert [d["path"] for d in gate["dropped"]] == ["src/b.py"]
    assert gate["command_source"] == "config"
    assert len(gate["runs"]) == len(result.runs)


def test_an_abandoned_sweep_records_why(tmp_path):
    repo = repo_of(tmp_path, {"notes.txt": "x\n"})
    ledger = Ledger(tmp_path / "ledger.jsonl")
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        result = run_test_gate(sandbox, fix(sandbox, {"notes.txt": "y\n"}))
    with JobRun(ledger, "sweep") as run:
        run.failed(result.render(), **result.ledger_fields())

    line = ledger.entries()[-1]
    assert line["test_gate"]["strategy"] == ABANDONED
    assert line["test_gate"]["cause"] == NO_TEST_COMMAND
    assert "test command" in line["message"]


def test_the_ledger_names_the_count_of_fixed_files_per_run(tmp_path):
    repo = repo_of(tmp_path, THREE, RED_IF_BROKEN)
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        result = run_test_gate(sandbox, fix(sandbox, {"src/a.py": "a = 2\n"}))
    (run,) = result.ledger_fields()["test_gate"]["runs"]
    assert run["fixed_count"] == 1 and "fixed" not in run


# --- the acceptance criterion, end to end ---------------------------------------------

BILLING = "src/sample/core/billing.py"
HANDLERS = "src/sample/web/handlers.py"
VARIANT = "src/sample/web/handlers_v2.py"


def edit(root, path, old, new):
    target = root / path
    text = target.read_text(encoding="utf-8")
    assert text.count(old) == 1
    target.write_text(text.replace(old, new), encoding="utf-8")


def test_a_fixture_sweep_whose_one_fix_breaks_the_tests_opens_a_pr_without_it(tmp_path, monkeypatch):
    """E3-R4's acceptance: the fixture's five planted fixes, one of which
    (the billing print fix) also zeroes the quantity and so breaks
    `test_line_total_applies_tax` under PLANTED.json's own test command. The
    PR excludes it and the ledger says `reduced`. The billing file's other,
    good fix goes with it: the gate drops whole paths, as the review and the
    patch work per path.

    The command's `python3` is pinned to the interpreter running these
    tests (its directory first on PATH), which has pytest: an ambient
    `python3` (macOS's /usr/bin/python3, with pytest from `pip install
    --user` or none) must not decide this test."""
    monkeypatch.setenv("PATH", os.pathsep.join([os.path.dirname(sys.executable),
                                                os.environ.get("PATH", os.defpath)]))
    repo = committed_sample(tmp_path)
    (repo / ".curamoir" / "curamoir.yaml").write_text(config(PLANTED["test_command"]))
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "test command"], cwd=repo, check=True)
    ledger = Ledger(tmp_path / "ledger.jsonl")
    with prepare_sandbox(repo, "HEAD", parent=tmp_path) as sandbox:
        lint_sandbox(sandbox)
        assert apply_file_remedies(sandbox).deleted == (VARIANT,)
        state = snapshot_git_state(sandbox)
        root = sandbox.path
        edit(root, BILLING, '    print("calculating line total")',
             '    logger.info("calculating line total")\n    quantity = 0')
        edit(root, BILLING,
             "    except OSError:\n        raise RuntimeError(\"could not load rates\")",
             "    except OSError as error:\n"
             "        raise RuntimeError(f\"could not load rates from {path}\") from error")
        edit(root, HANDLERS, 'print(f"request failed: {exception}")',
             'logger.error("request failed: %s", exception)')
        edit(root, HANDLERS, '    except IndexError:\n        raise ValueError("bad token")',
             '    except IndexError as error:\n'
             '        raise ValueError(f"bad token: {raw!r}") from error')
        verdict = verify_fix(sandbox, git_state=state)
        assert verdict.ok, verdict.render()
        assert verdict.paths == (BILLING, HANDLERS, VARIANT)

        result = run_test_gate(sandbox, verdict)
        patch = sandbox.patch(result.paths_for_pr)
    with JobRun(ledger, "sweep") as run:
        run.succeeded("opened one PR", **result.ledger_fields())

    assert result.strategy == REDUCED
    assert result.paths_for_pr == (HANDLERS, VARIANT)
    assert [d.path for d in result.dropped] == [BILLING]
    assert BILLING.encode() not in patch
    assert ledger.entries()[-1]["test_gate"]["strategy"] == REDUCED
    # The PR's tree is green under the same command.
    (tmp_path / "fix.patch").write_bytes(patch)
    subprocess.run(["git", "apply", str(tmp_path / "fix.patch")], cwd=repo, check=True)
    green = subprocess.run(["sh", "-c", PLANTED["test_command"]], cwd=repo,
                           capture_output=True, env={**os.environ,
                                                     "PYTHONDONTWRITEBYTECODE": "1"})
    assert green.returncode == 0, green.stdout.decode() + green.stderr.decode()
