"""Round 7: the R5 edge-case review's second failure, and the security review
of 3ecd16d. Broad rules again:

- **zsh's clobber redirections** (`>!`, `>>!`, `&>!`, `>>|`) lex as an
  operator and a `!`, so the real target hid behind it (E1).
- **Every allowlisted program that is not a pure reader is path-strict** (E2):
  `go build -o`, `cargo --target-dir`, `pip --target`, `gh run download -D`,
  `npm --prefix`, `make -C` and the rest write where an option says, and a
  per-tool list of those options is the denylist problem.
- **A tree-changing git command earlier in the command** can create the
  symlink a later write goes through, after the guard looked (E3).
- **git's leading options read by their arity** (S1), and **paths relative to
  another directory** refused rather than resolved (S2).
- **zsh's `=cmd` expansion** (S3), and **every `=`/`:`-separated part** of
  every value (S5).
"""

import pytest

from curamoir.hooks import judge_command
from curamoir.hooks.verdict import Category


def denied(command, cwd=None):
    return judge_command(command, in_job=True, cwd=cwd).deny


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / ".git" / "hooks").mkdir(parents=True)
    (root / "hk").symlink_to(root / ".git" / "hooks")
    return root


# --- E1: zsh clobber redirections --------------------------------------------

@pytest.mark.parametrize("command", [
    "echo x >! .git/config",
    "echo x >>! .git/config",
    "echo x &>! .git/config",
    "echo x &>>! .git/config",
    "echo x >>| .git/config",
    "echo x >! out.txt",
    "echo x >!out.txt",
    "echo x > | out.txt",
])
def test_a_zsh_clobber_redirection_is_refused(command):
    decision = judge_command(command, in_job=True)
    assert decision.deny and decision.category == Category.UNVERIFIABLE, decision


def test_a_plain_redirection_still_passes():
    assert not denied("echo x > out.txt")
    assert not denied("echo x >| out.txt")


# --- E2: every non-reader is path-strict --------------------------------------

@pytest.mark.parametrize("command", [
    "go build -o .git/hooks/pre-commit",
    "go build -o=/tmp/x",
    "go build -o/tmp/x ./...",
    "cargo build --artifact-dir=.git/x",
    "cargo build --target-dir /tmp/t",
    "pip install --target .git/hooks x",
    "pip download -d /tmp",
    "docker build --iidfile .git/x .",
    "gh run download -D .git/hooks",
    "gh repo clone a/b /tmp/x",
    "gh api --output /tmp/x",
    "npm install --prefix /tmp",
    "npm pack --pack-destination /tmp",
    "uv venv /tmp/v",
    "uv run --directory /tmp pytest",
    "make -C /tmp",
    "bd export -o /tmp/x",
    "curamoir sweep --out=/tmp/x",
])
def test_a_non_reader_writing_outside_the_diff_is_refused(command):
    assert denied(command), command


def test_go_test_into_a_symlink_is_refused(repo):
    assert denied("go test -c -o hk/pre-commit", cwd=str(repo))
    assert not denied("go test -c -o src/x.test", cwd=str(repo))


@pytest.mark.parametrize("command", [
    "go build ./...", "cargo test", "pip install -r requirements.txt", "npm test",
    "gh pr view 1", "bd ready", "docker build -t name:tag .",
    "pip install git+https://github.com/a/b", "go test -run 'TestX/sub' ./...",
    "cat /etc/hosts", "grep -r x /usr/include", "ls /tmp", "diff /tmp/a b",
])
def test_ordinary_work_and_pure_reads_pass(command):
    assert not denied(command), judge_command(command, in_job=True).reason


# --- E3: a write after a tree-changing git command ----------------------------

@pytest.mark.parametrize("command", [
    "git apply p.patch && echo x > newlink/pre-commit",
    "git checkout -b x; touch a/b",
    "git pull && cp a b",
    "git stash && echo x >> notes.txt",
    "git -C sub merge main && tee out.log",
])
def test_a_write_after_the_tree_may_change_is_refused(command):
    decision = judge_command(command, in_job=True)
    assert decision.deny and decision.category == Category.UNVERIFIABLE, decision
    assert "run them separately" in decision.reason


@pytest.mark.parametrize("command", [
    "git status && echo x > out.txt",
    "echo x > out.txt && git apply p.patch",
    "git pull && git log -1",
    "git add -A && git commit -m x",
])
def test_a_write_without_a_tree_change_before_it_passes(command):
    assert not denied(command), judge_command(command, in_job=True).reason


# --- S1: git's leading options by arity ---------------------------------------

@pytest.mark.parametrize("command", [
    "git --namespace x -C /tmp/y status",
    "git --namespace=x --git-dir /tmp/g log",
    "git -C sub -C /tmp status",
    "git --no-pager --work-tree=/tmp/w status",
])
def test_every_git_directory_value_is_judged(command):
    assert denied(command), command


def test_git_namespace_value_is_not_a_directory():
    assert not denied("git --namespace /tmp status")


# --- S2: paths relative to another directory -----------------------------------

@pytest.mark.parametrize("command", [
    "git -C sub diff --output=patch.diff",
    "git --work-tree=sub diff --output patch.diff",
    "git -C sub init x",
])
def test_a_git_output_path_under_another_directory_is_refused(command):
    assert denied(command), command


def test_a_git_output_path_without_a_directory_option_passes():
    assert not denied("git diff --output=patch.diff")


# --- S3: zsh `=cmd` expansion ------------------------------------------------

@pytest.mark.parametrize("command", ["cat =ls", "echo x > =x", "ls =(id)"])
def test_a_word_beginning_with_equals_is_refused(command):
    decision = judge_command(command, in_job=True)
    assert decision.deny and decision.category == Category.UNVERIFIABLE


@pytest.mark.parametrize("command", [
    "git log --format=x", "test a = b", "echo '=x'", 'echo "=x"',
])
def test_an_equals_that_zsh_does_not_expand_passes(command):
    assert not denied(command)


# --- S5: every `=`- and `:`-separated part of every value ----------------------

@pytest.mark.parametrize("command", [
    "pytest -o cache_dir=/tmp/x",
    "pytest --override-ini=cache_dir=/tmp/x",
    "ruff --config=a=b=/tmp/x check",
    "mypy --cache-dir=x:/tmp/y src",
    "pytest -o=/tmp/x",
])
def test_every_separated_part_of_a_value_is_judged(command):
    assert denied(command), command
