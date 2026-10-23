import os
import subprocess

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
