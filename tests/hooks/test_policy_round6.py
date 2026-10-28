"""Round 6: broad rules rather than one spelling at a time.

The guard is defence in depth (the boundary is an OS sandbox around any job
agent's shell), so this round closes whole families rather than the spelling a
reviewer happened to try:

- **An unquoted `(` anywhere in a word.** zsh, the macOS default shell the
  Bash tool runs, reads it as a glob qualifier (`*(e:cmd:)` runs cmd) or a
  process substitution (`=(cmd)`), not only at a word's start.
- **Linear brace detection**, so a long run of commas costs no time.
- **`git push --prune`** deletes remote branches, as --mirror and --delete do.
- **Every way an allowlisted tool writes a path**: options (`sort -o`,
  `git diff --output`, pytest's `--junitxml`, `-o cache_dir=`), extra
  positionals (`uniq in out`), in-place modes (`yq -i`, `prettier --write`),
  `find -fprint`, `git init --separate-git-dir`, `git worktree add`, and `cp`'s
  every path, read by its options' arity.
- **npm's program-naming options from three letters** (`--scr`, `--nod`).
- **A symlink on the way to a write target** is a write outside the diff.
"""

import time

import pytest

from curamoir.hooks import judge_command
from curamoir.hooks.verdict import Category


def denied(command, cwd=None):
    return judge_command(command, in_job=True, cwd=cwd).deny


# --- 1: an unquoted `(` anywhere in a word -----------------------------------

@pytest.mark.parametrize("command", [
    "ls *(e:x:)",
    "ls x*(+f)",
    "cat =(id)",
    "echo a(b)",
    "git log --format=%(e:id:)",
    "git for-each-ref --format=%(oe:id:)",
    "ls --format=*%(refname)",
    "git for-each-ref --format=%(refname:sh/x)",
])
def test_an_unquoted_parenthesis_is_refused(command):
    decision = judge_command(command, in_job=True)
    assert decision.deny and decision.category == Category.UNVERIFIABLE


@pytest.mark.parametrize("command", [
    "git for-each-ref --format=%(refname:short)",
    "git for-each-ref --format=%(refname:short)%(upstream:track)",
    "git log --format=%(x)",
])
def test_an_unquoted_git_format_atom_is_refused_and_told_to_quote(command):
    decision = judge_command(command, in_job=True)
    assert decision.deny and decision.category == Category.UNVERIFIABLE
    assert "quote the format string" in decision.alternative


@pytest.mark.parametrize("command", [
    "git for-each-ref --format='%(refname:short)'",
    "git for-each-ref --format=\"%(refname:short)%(upstream:track)\"",
    "git log --format='%(x)'",
    'git log --format="%(e:x:)"',
    "grep -nE '(WIP|TODO)' x",
    "echo \\(x\\)",
])
def test_a_quoted_or_git_format_parenthesis_is_allowed(command):
    assert not denied(command)


# --- 2: brace detection is linear --------------------------------------------

@pytest.mark.parametrize("command", [
    "echo {" + "," * 15_990,
    "echo " + "{," * 7_995,
    "echo " + "{" * 15_990,
])
def test_brace_detection_is_linear(command):
    started = time.monotonic()
    judge_command(command, in_job=True)
    assert time.monotonic() - started < 0.3


# --- 3: git push --prune -----------------------------------------------------

@pytest.mark.parametrize("command", [
    "git push --prune origin", "git push --pru origin", "git push --prun",
])
def test_git_push_prune_is_denied(command):
    decision = judge_command(command, in_job=True)
    assert decision.deny and decision.category == Category.DESTRUCTIVE


def test_git_push_progress_still_passes():
    assert not denied("git push --progress origin main")


# --- 4: writes through an option or an argument -------------------------------

@pytest.mark.parametrize("command", [
    "sort -o /tmp/x a.txt",
    "sort -o/tmp/x a.txt",
    "sort --output=.git/hooks/pre-commit a.txt",
    "sort --output ../x a.txt",
    "sort --out=/tmp/x a.txt",
    "sort -uo /tmp/x a.txt",
    "uniq a.txt /tmp/x",
    "uniq -c a.txt .git/hooks/pre-commit",
    "find . -fprint /tmp/x",
    "find . -fprint0 out.txt",
    "find . -fprintf out.txt %p",
    "find . -fls out.txt",
    "git diff --output=/tmp/x",
    "git log --output ../x",
    "git init --separate-git-dir=/tmp/g",
    "git init /tmp/elsewhere",
    "git worktree add ../wt",
    "git worktree add wt",
    "yq -i '.a = 1' /tmp/x.yaml",
    "yq --inplace '.a = 1' .git/config",
    "prettier --write /tmp/x.js",
    "prettier -w ../x.js",
    "prettier --check /tmp/x.js",
    "pytest --junitxml=/tmp/x.xml",
    "pytest --junit-xml /tmp/x.xml",
    "pytest --basetemp=../t",
    "pytest -o cache_dir=/tmp/c",
    "pytest --resultlog=.git/x",
    "python3 -m pytest --junitxml=/tmp/x.xml",
    "uv run pytest --basetemp /tmp/t",
    "ruff check --output-file=/tmp/r .",
    "eslint -o /tmp/r.txt src",
    "black ~/x.py",
    "mypy --cache-dir=/tmp/m src",
])
def test_a_write_through_an_option_or_argument_outside_the_diff_is_refused(command):
    assert denied(command), command


@pytest.mark.parametrize("command", [
    "sort -o out.txt a.txt",
    "sort -k1,2 a.txt",
    "uniq a.txt out.txt",
    "uniq -c a.txt",
    "find . -name '*.py' -print",
    "git diff --output=patch.diff",
    "git diff --stat",
    "git init",
    "git worktree list",
    "yq -i '.a = 1' config.yaml",
    "yq '.a' /etc/x.yaml",
    "prettier --write src/x.js",
    "pytest --junitxml=report.xml -p no:cacheprovider -k 'not slow'",
    "pytest tests/a.py::test_x",
    "python3 -m pytest -q",
    "ruff check --output-file=r.txt .",
])
def test_a_write_inside_the_diff_or_a_read_is_allowed(command):
    assert not denied(command), judge_command(command, in_job=True).reason


# --- 5: cp, every path, options by arity --------------------------------------

@pytest.mark.parametrize("command", [
    "cp /etc/hosts hosts",
    "cp .git/config x",
    "cp -s a b",
    "cp -l a b",
    "cp --symbolic-link a b",
    "cp --link a b",
    "cp -rs a b",
    "cp --frobnicate a b",
    "cp -Z a b",
    "cp -t .git a",
    "cp -S .bak a /tmp/b",
])
def test_cp_is_read_by_its_options_and_every_path(command):
    assert denied(command), command


@pytest.mark.parametrize("command", [
    "cp a b", "cp -r src dst", "cp -a a b", "cp -pR a b", "cp -t dst a b",
    "cp --target-directory=dst a", "cp -S .bak a b", "cp --backup=numbered a b",
    "cp -- -a b",
])
def test_ordinary_cp_passes(command):
    assert not denied(command), judge_command(command, in_job=True).reason


# --- 6: npm's program-naming options from three letters -----------------------

@pytest.mark.parametrize("command", [
    "npm run build --scr=x", "npm run build --script=x", "npm run build --node=x",
    "npm run build --nodeo=x", "npm test --SCRI_PT=x", "npm run build --nod x",
])
def test_npm_program_naming_options_count_from_three_letters(command):
    assert denied(command)


@pytest.mark.parametrize("command", [
    "npm run build --silent", "npm exec --no -- tsc", "npm run build -s",
    "npm test --if-present",
])
def test_npm_options_not_naming_a_program_pass(command):
    assert not denied(command)


# --- 7: a symlink on the way to a write target --------------------------------

@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "build").mkdir()
    (root / ".git" / "hooks").mkdir(parents=True)
    (tmp_path / "outside").mkdir()
    (root / "a").symlink_to(root / ".git")
    (root / "src" / "deep").symlink_to(root / ".git" / "hooks")
    (root / "linkdir").symlink_to(tmp_path / "outside")
    (root / "linkfile").symlink_to(tmp_path / "outside" / "f")
    (root / "inner").symlink_to(root / "src")
    return root


@pytest.mark.parametrize("command", [
    "echo x > linkdir/f",
    "echo x > linkfile",
    "echo x > a/hooks/x",
    "echo x > sub/../a/hooks/x",
    "echo x > src/deep/pre-commit",
    "tee a/config",
    "cp src/a linkdir/b",
    "mkdir -p linkdir/x",
    "sort -o linkfile a",
    "echo x > .gi[^x]/hooks/pre-commit",
    "echo x > ./*/hooks/x",
    "tee .g*/config",
    "touch link*/x",
])
def test_a_write_through_a_symlink_is_refused(repo, command):
    assert denied(command, cwd=str(repo)), command


@pytest.mark.parametrize("command", [
    "echo x > src/f",
    "echo x > out.txt",
    "echo x > build/out.txt",
    "echo x > new/dir/f",
    "echo x > src/../out.txt",
    "echo x > inner/f",
    "cp src/a src/b",
    "touch src/x",
    "echo x > 'a*.txt'",
])
def test_a_write_without_a_symlink_is_allowed(repo, command):
    assert not denied(command, cwd=str(repo))


def test_an_absolute_path_inside_the_repository_is_allowed_when_cwd_is_known(repo):
    assert not denied(f"echo x > {repo}/src/f", cwd=str(repo))
    assert denied(f"echo x > {repo}/linkdir/f", cwd=str(repo))
    assert denied(f"echo x > {repo}/.git/hooks/x", cwd=str(repo))


def test_without_a_cwd_the_symlink_check_is_skipped(repo, monkeypatch):
    monkeypatch.chdir(repo)
    assert not denied("echo x > linkdir/f")


def test_the_hook_passes_its_cwd(repo, monkeypatch):
    import io
    import json

    from curamoir.hooks import pretooluse

    monkeypatch.setenv("CURAMOIR_JOB", "sweep")
    monkeypatch.setenv("CURAMOIR_USE_DCG", "0")
    stdout = io.StringIO()
    payload = {"tool_name": "Bash", "tool_input": {"command": "echo x > linkdir/f"},
               "cwd": str(repo)}
    pretooluse.main(stdin=io.StringIO(json.dumps(payload)), stdout=stdout)
    assert json.loads(stdout.getvalue())["hookSpecificOutput"]["permissionDecision"] == "deny"


# --- round 6b: git's directory options are write targets ----------------------

@pytest.mark.parametrize("command", [
    "git -C /tmp/x status",
    "git -C .. status",
    "git -C ~/elsewhere log",
    "git --git-dir=/tmp/g log",
    "git --git-dir .git/modules/x log",
    "git --work-tree ../w status",
    "git --work-tree=src/../../w status",
    "git -C .git status",
])
def test_git_directory_options_outside_the_diff_are_refused(command):
    assert denied(command), command


@pytest.mark.parametrize("command", [
    "git -C sub status", "git -C src/x log", "git --namespace=x log",
    "git --work-tree=. status",
])
def test_git_directory_options_inside_the_repository_pass(command):
    assert not denied(command), judge_command(command, in_job=True).reason


def test_git_directory_options_are_resolved_against_cwd(repo):
    assert denied("git -C linkdir status", cwd=str(repo))
    assert denied("git -C a status", cwd=str(repo))
    assert not denied(f"git -C {repo}/src status", cwd=str(repo))
    assert not denied("git -C src status", cwd=str(repo))


# --- round 6c: `.git` compared without case, after the real path -------------

def test_a_symlink_to_an_upper_case_git_directory_is_refused(tmp_path):
    """On macOS `.GIT` is `.git`; realpath keeps the case it was given, so
    the rule must compare without case. A directory literally named `.GIT`
    makes this meaningful on a case-sensitive filesystem too."""
    root = tmp_path / "repo"
    (root / ".GIT" / "hooks").mkdir(parents=True)
    (root / "a").symlink_to(root / ".GIT")
    assert denied("echo x > a/hooks/x", cwd=str(root))


def test_a_nested_git_directory_in_any_case_is_refused(tmp_path):
    root = tmp_path / "repo"
    (root / "x" / ".Git").mkdir(parents=True)
    (root / "x" / "b").symlink_to(root / "x" / ".Git")
    assert denied("echo x > x/.Git/y", cwd=str(root))
    assert denied("echo x > x/b/y", cwd=str(root))
    assert not denied("echo x > x/y", cwd=str(root))
