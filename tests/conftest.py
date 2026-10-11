import subprocess

import pytest

from tests.harness import Harness


@pytest.fixture
def harness(tmp_path):
    """A throwaway git repo with a ledger, for running one guardrail at a time."""
    return Harness(tmp_path / "repo")


@pytest.fixture
def swept(tmp_path):
    """`fixtures/sample-repo` as a git repository at one commit, with
    tests/test_lint.py's regex rules installed, and the work list the real
    `scripts/curamoir-lint` gives for that commit: (repo, WorkList). For the
    sweep's scope check (E3-R2), which compares a fix with this commit."""
    from curamoir.sweep import work_list
    from tests.test_lint import ALL_REGEX_RULES, sample_repo

    repo = sample_repo(tmp_path, *ALL_REGEX_RULES)
    for command in (["init", "--initial-branch=main"],
                    ["config", "user.email", "test@example.invalid"],
                    ["config", "user.name", "test"],
                    ["add", "-A"],
                    ["commit", "-m", "base"]):
        subprocess.run(["git", *command], cwd=repo, check=True, capture_output=True)
    return repo, work_list(repo)
