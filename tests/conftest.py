import subprocess

import pytest

from tests.harness import Harness


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
