"""Round 13, from the R5 fixture-provenance review of the guard.

1. `git push -f --force-with-lease` was allowed: the lease exempted an
   explicit -f, which `git help push` says "disables these checks".
2. Every destructive rule's ADVICE pinned by its text, as a literal string
   here and not imported from the code, so a rule that keeps refusing but
   loses or swaps its advice fails a test (`git branch -d -f` must get the
   deletion advice, not the branch-move one).
3. The package.json size bound observable: valid JSON padded past the bound
   is unknown scripts, not read.
"""

import io
import json

import pytest

from curamoir.hooks import judge_command, policy, pretooluse
from curamoir.hooks.verdict import Category

D, N = Category.DESTRUCTIVE, Category.NOT_ALLOWLISTED

STASH = "git stash"
LEASE = "git push --force-with-lease"
PUSH_BRANCH = "git push <remote> <branch>"
DELETE_LOCALLY = "delete the branch locally and let the PR close it"
CHECKOUT = "git switch <branch> to change branch, git stash push <path> to set work aside"
STASH_PATH = "git stash push <path>"
NEW_BRANCH = "create a new branch (git switch -c <new>) instead of moving an existing one"
DELETION = ("a job does not delete or truncate files: put the deletion in the PR's "
            "description, or `git rm --cached` to untrack")
EMPTIED = "write to a new file, then mv it over, as two separate commands"
SETTINGS = "say in the PR what should change; a job does not change the repository's settings"
BRANCH = "leave the branch; say in the PR what should be deleted"
API = "a job reads GitHub with gh api; it does not change it"


# --- 1: an explicit force is not excused by a lease ------------------------------

@pytest.mark.parametrize("command", [
    "git push -f --force-with-lease", "git push --force --force-with-lease origin main",
    "git push --force-if-includes -f", "git push --force-with-lease -f origin main",
])
def test_an_explicit_force_beside_a_lease_is_denied(command):
    decision = judge_command(command, in_job=True)
    assert decision.deny and decision.category == D, command
    assert LEASE in decision.alternative


@pytest.mark.parametrize("command", [
    "git push --force-with-lease origin main", "git push --force-with-lease",
    "git push --force-if-includes --force-with-lease origin main",
])
def test_a_lease_alone_is_allowed(command):
    assert not judge_command(command, in_job=True).deny


# --- 2: every rule's advice, by its text -------------------------------------------

ADVICE = [
    ("git reset --hard", D, STASH),
    ("git reset --ha HEAD~1", D, STASH),
    ("git reset --merge", D, STASH),
    ("git push --force", D, LEASE),
    ("git push origin +main", D, LEASE),
    ("git push --delete origin x", D, DELETE_LOCALLY),
    ("git push origin :x", D, DELETE_LOCALLY),
    ("git push --prune origin", D, PUSH_BRANCH),
    ("git push --mirror", D, PUSH_BRANCH),
    ("git checkout a.py", D, CHECKOUT),
    ("git checkout -- a.py", D, CHECKOUT),
    ("git checkout -f main", D, CHECKOUT),
    ("git switch --discard-changes main", D, STASH_PATH),
    ("git switch -f main", D, STASH_PATH),
    ("git clean -f", D, "git clean -n"),
    ("git clean -fd", D, "git clean -n"),
    ("git branch -D x", D, "git branch -d"),
    ("git branch -d -f x", D, "git branch -d"),
    ("git branch --delete --force x", D, "git branch -d"),
    ("git checkout -B x", D, NEW_BRANCH),
    ("git switch -C x", D, NEW_BRANCH),
    ("git switch --force-create x", D, NEW_BRANCH),
    ("git branch -f x HEAD~1", D, NEW_BRANCH),
    ("git branch -M x", D, NEW_BRANCH),
    ("git branch -C a b", D, NEW_BRANCH),
    ("git stash drop", D, "git stash list"),
    ("git stash clear", D, "git stash list"),
    ("git tag -d v1", D, "leave it"),
    ("git rm a", D, DELETION),
    ("git rm -r --cached src", D, "git rm --cached <path> for each file"),
    ("rm a", N, DELETION),
    ("rm -rf build", N, DELETION),
    ("rmdir a", N, DELETION),
    ("unlink a", N, DELETION),
    ("truncate -s0 a", N, DELETION),
    ("shred a", N, DELETION),
    ("sed -i s/a/b/ x", N, DELETION),
    ("sed --in-place s/a/b/ x", N, DELETION),
    ("sed --in-place=.bak s/a/b/ x", N, DELETION),
    ("sed -Ei s/a/b/ x", N, DELETION),
    ("sort a > a", D, EMPTIED),
    ("cat a >| a", D, EMPTIED),
    ("cat a | sort > a", D, EMPTIED),
    ("sort < a > a", D, EMPTIED),
    ("uniq a a", D, EMPTIED),
    ("bd delete x-1", D, "bd close"),
    ("gh issue delete 1", D, "close it instead (gh issue close)"),
    ("gh label delete x", D, SETTINGS),
    ("gh repo archive a/b", D, SETTINGS),
    ("gh repo deploy-key delete 1", D, SETTINGS),
    ("gh release delete v1", D, SETTINGS),
    ("gh pr merge 1", D, "leave the PR for review; a job never merges"),
    ("gh pr close 1 -d", D, BRANCH),
    ("gh pr close 1 --delete-branch", D, BRANCH),
    ("gh api -X DELETE repos/o/r/git/refs/heads/x", D, BRANCH),
    ("gh api -X PATCH repos/o/r", D, API),
    ("gh api repos/o/r/issues -f title=x", D, API),
    ("gh api graphql -f query='mutation{x}'", D, API),
    ("timeout 5 git reset --hard", D, STASH),
    ("nice git reset --hard", D, STASH),
    ("command git push --force", D, LEASE),
    ("GIT_DIR=x git reset --hard", D, STASH),
    ("FOO=1 git branch -D x", D, "git branch -d"),
]


@pytest.mark.parametrize("command,category,advice", ADVICE, ids=[row[0] for row in ADVICE])
def test_every_rule_names_its_advice(command, category, advice):
    decision = judge_command(command, in_job=True)
    assert decision.deny, command
    assert decision.category == category, decision
    assert advice in decision.alternative, decision.alternative


def _main(raw):
    stdout = io.StringIO()
    pretooluse.main(stdin=io.StringIO(raw), stdout=stdout)
    return json.loads(stdout.getvalue())["hookSpecificOutput"]["permissionDecisionReason"]


def test_a_malformed_payload_in_a_job_says_retry(monkeypatch):
    monkeypatch.setenv("CURAMOIR_JOB", "sweep")
    assert "retry the command" in _main("[1]")


def test_a_judging_exception_in_a_job_says_simplify(monkeypatch):
    monkeypatch.setenv("CURAMOIR_JOB", "sweep")
    monkeypatch.setenv("CURAMOIR_USE_DCG", "0")

    def broken(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setitem(policy._PROGRAM_CHECKS, "make", (broken,))
    payload = {"tool_name": "Bash", "tool_input": {"command": "make test"}, "cwd": "."}
    assert "simplify the command" in _main(json.dumps(payload))


# --- 3: the package.json size bound ------------------------------------------------

def test_valid_package_json_padded_past_the_bound_is_not_read(tmp_path):
    body = json.dumps({"scripts": {"build": "tsc"}}).encode()
    padding = b" " * (pretooluse.MAX_PACKAGE_JSON_BYTES + 1 - len(body) + 10)
    (tmp_path / "package.json").write_bytes(body + padding)
    assert pretooluse._package_scripts(tmp_path, "yarn run build") is None


def test_valid_package_json_within_the_bound_is_read(tmp_path):
    body = json.dumps({"scripts": {"build": "tsc"}}).encode()
    (tmp_path / "package.json").write_bytes(body + b" " * 100)
    assert pretooluse._package_scripts(tmp_path, "yarn run build") == frozenset({"build"})
