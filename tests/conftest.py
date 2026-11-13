import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.fake_gh import FakeGh
from tests.harness import Harness


@pytest.fixture(scope="session")
def _quiet_gh(tmp_path_factory):
    """A fake gh (tests/fake_gh.py) that finds no labels and no open PRs."""
    return FakeGh(tmp_path_factory.mktemp("quiet-gh"))


@pytest.fixture(autouse=True)
def no_real_gh(_quiet_gh, monkeypatch):
    """No test reaches the real `gh`, in this process or in any it starts.

    The runner lists labels for every job and open PRs for the sweep
    (curamoir.github), so without this a test of `curamoir run` would call
    GitHub with the developer's credentials. Two seams, because two kinds
    of process run the runner: this one, through `github.GH`, and the
    child interpreters tests/test_lock_slot.py starts, which import
    curamoir afresh (their `github.GH` is "gh" again) and inherit only the
    environment. For those, the fake's directory goes first on PATH: the
    fake is an executable named `gh`, so "gh" resolves to it.

    The stand-in answers as an unpaused repository with no open PRs does,
    so the kill switch and the cap pass without a guard line, as they did
    before the runner had gh defaults. It is shared by the session and
    reset before every test, so its call log is this test's alone: a test
    that takes this fixture gets the FakeGh and can assert on its calls. A
    test that needs gh to say something else points `github.GH` at a
    FakeGh of its own."""
    from curamoir import github

    _quiet_gh.reset()
    monkeypatch.setattr(github, "GH", str(_quiet_gh.path))
    monkeypatch.setenv("PATH", os.pathsep.join(
        [str(_quiet_gh.directory), *filter(None, [os.environ.get("PATH")])]))
    return _quiet_gh


@pytest.fixture(autouse=True)
def no_declared_runner(monkeypatch):
    """Every test starts local, with no cloud session, whatever the shell
    that runs the suite declares (E14.1). The runner reads CURAMOIR_RUNNER
    and CLAUDE_CODE_REMOTE_SESSION_ID from os.environ when no `environ` is
    passed (job_guard.run_guarded), so a suite run inside a hosted workflow
    step or a Claude Code cloud session would otherwise run hosted, or put
    a transcript link on lines and PR bodies the tests pin. The hosted
    markers are left as they are, but for CLAUDE_CODE_REMOTE: on their own
    they resolve local, which tests/test_runner_kind.py proves, but
    CLAUDE_CODE_REMOTE=true alone also moves every gh call to `gh api`
    (config.resolve_github_api, cm-2qp.9), which the CLI path's tests pin
    against. A test that means another runner passes `environ`, or sets
    the variables itself."""
    monkeypatch.delenv("CURAMOIR_RUNNER", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_REMOTE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_REMOTE", raising=False)


#: What the refusing stand-in `claude` prints on stderr (no_real_claude).
NO_REAL_CLAUDE = "the real claude is not reachable from the tests: pass a stand-in"


@pytest.fixture(scope="session")
def _refusing_claude(tmp_path_factory):
    """A `claude` that makes no call: it prints NO_REAL_CLAUDE on stderr,
    nothing on stdout, and exits 97."""
    script = tmp_path_factory.mktemp("no-claude") / "claude"
    script.write_text(f"#!{sys.executable}\nimport sys\n"
                      f"sys.stderr.write({NO_REAL_CLAUDE!r} + '\\n')\nsys.exit(97)\n",
                      encoding="utf-8")
    script.chmod(0o755)
    return script


#: The tests that run the real `claude` CLI on purpose (no_real_claude).
_REAL_CLAUDE_CLI_TESTS = Path(__file__).parent / "plugin"


@pytest.fixture(autouse=True)
def no_real_claude(_refusing_claude, monkeypatch, request):
    """No test reaches the real `claude` (a model call, on the developer's
    subscription), in this process or in any it starts. Two seams, as for
    gh (no_real_gh): sweep_job.CLAUDE, the executable the registered sweep
    body (runner.JOBS["sweep"], which makes its own SweepTools) runs, and
    PATH, where the refusing stand-in is first under the name `claude`, for
    every other caller that runs "claude" by name (fix_step's and tier1's
    defaults, a child interpreter). A test that means a model call to
    happen sets sweep_job.CLAUDE, or passes `claude=`, to a stand-in of its
    own; one that forgets gets this refusal, never a model.

    tests/plugin/ (_REAL_CLAUDE_CLI_TESTS) keeps PATH's `claude`: those
    tests run the real CLI on purpose, for commands that make no model call
    (`claude plugin validate`, `claude plugin details`), and skip when it
    is not installed. sweep_job.CLAUDE is the stand-in there too."""
    from curamoir import sweep_job

    monkeypatch.setattr(sweep_job, "CLAUDE", str(_refusing_claude))
    if _REAL_CLAUDE_CLI_TESTS in Path(request.node.path).parents:
        return _refusing_claude
    monkeypatch.setenv("PATH", os.pathsep.join(
        [str(_refusing_claude.parent), *filter(None, [os.environ.get("PATH")])]))
    return _refusing_claude


@pytest.fixture
def harness(tmp_path):
    """A throwaway git repo with a ledger, for running one guardrail at a time."""
    return Harness(tmp_path / "repo")


def committed_sample(tmp_path, *rules):
    """`fixtures/sample-repo` as a git repository at one commit, with `rules`
    installed: by default tests/test_lint.py's regex rules."""
    from tests.test_lint import ALL_REGEX_RULES, sample_repo

    repo = sample_repo(tmp_path, *(rules or ALL_REGEX_RULES))
    for command in (["init", "--initial-branch=main"],
                    ["config", "user.email", "test@example.invalid"],
                    ["config", "user.name", "test"],
                    ["add", "-A"],
                    ["commit", "-m", "base"]):
        subprocess.run(["git", *command], cwd=repo, check=True, capture_output=True)
    return repo


@pytest.fixture
def swept(tmp_path):
    """The committed sample (committed_sample) and the work list the real
    `scripts/curamoir-lint` gives for that commit: (repo, WorkList). For the
    prompt and the CLI, which do not touch git."""
    from curamoir.sweep import work_list

    repo = committed_sample(tmp_path)
    return repo, work_list(repo)


@pytest.fixture
def sandboxed(tmp_path):
    """A sandbox (git_guard.prepare_sandbox) of the committed sample at its
    one commit, linted there by the real lint (fix_scope.lint_sandbox): what
    the sweep's fix step edits and its review step compares. For E3-R2's
    scope check. Removed after the test."""
    from curamoir.fix_scope import lint_sandbox
    from curamoir.git_guard import prepare_sandbox

    with prepare_sandbox(committed_sample(tmp_path), "HEAD", parent=tmp_path) as sandbox:
        lint_sandbox(sandbox)
        yield sandbox


#: Where grant_tmp makes its directories on Linux: inside the checkout,
#: ignored by git (.gitignore), and emptied after each test.
GRANT_TMP_ROOT = Path(__file__).resolve().parents[1] / ".pytest-grant-tmp"


@pytest.fixture
def grant_tmp(tmp_path):
    """A temporary directory os_sandbox.granted() can grant, for the tests
    that build a home, a toolchain or a venv and ask whether (or check
    that) it is granted. On macOS that is tmp_path (under
    /private/var/folders). On Linux tmp_path is under /tmp, a socket tree
    granted() refuses whatever is in it (os_sandbox._SOCKET_TREES), so those
    tests would only ever see a refusal there (cm-4st): there it is a fresh
    directory under GRANT_TMP_ROOT, removed after the test."""
    if not sys.platform.startswith("linux"):
        yield tmp_path
        return
    import tempfile

    from curamoir import os_sandbox

    for tree in (*os_sandbox._SOCKET_TREES, *os_sandbox._KERNEL_TREES):
        assert not os_sandbox._inside(str(GRANT_TMP_ROOT), tree), (
            f"the checkout is under {tree}, which granted() refuses: grant_tmp cannot "
            "give a grantable directory here")
    GRANT_TMP_ROOT.mkdir(exist_ok=True)
    path = Path(tempfile.mkdtemp(dir=GRANT_TMP_ROOT))
    try:
        yield path
    finally:
        remove_grant_tmp(path)


def remove_grant_tmp(root):
    """Remove grant_tmp's directory `root` whole, whatever modes a test left
    in it: every directory is made u+rwx first, top down (a directory must
    be listable before what is in it can be reached), and only then is the
    tree removed. A link is never followed: what it names keeps its mode.
    (An error handler cannot do this for rmtree: on an unlistable directory
    the call it would retry is os.open's, cm-4st R5 edge cases.)"""
    import shutil
    import stat

    pending = [str(root)]
    while pending:
        directory = pending.pop()
        os.chmod(directory, stat.S_IRWXU)  # never a link: only real directories are queued
        with os.scandir(directory) as listing:
            pending += [entry.path for entry in listing
                        if entry.is_dir(follow_symlinks=False)]
    shutil.rmtree(root)
