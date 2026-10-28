"""Round 11 (the re-scoped lens: an agent's honest mistakes, and never failing
open):

1. a write that empties its own input through a PIPELINE or a `<` source
   (`cat a | sort > a`), not only within one command;
2. `uniq a a`, whose output path is its input;
3. gh: any `<noun> delete`, `repo archive`/`rename`, `pr merge`, `api -X
   DELETE` inside a cluster, and GraphQL mutations;
4. `git rm -r --cached <dir>` gets advice that fits it;
5. an environment assignment in front of a destructive git keeps the rule's
   advice, as a wrapper does.
"""

import pytest

from curamoir.hooks import judge_command
from curamoir.hooks.verdict import Category


def verdict(command):
    return judge_command(command, in_job=True)


# --- 1 and 2: self-emptying through a pipeline, `<`, or uniq -------------------------

@pytest.mark.parametrize("command", [
    "cat a | sort > a", "cut -f1 a | sort > a", "cat a.json | jq . > a.json",
    "sort < a > a", "jq . < a.json > a.json", "cat a | sort >| a", "uniq a a",
    "uniq -c ./a a",
])
def test_a_write_over_its_own_pipeline_input_is_refused(command):
    decision = verdict(command)
    assert decision.deny and decision.category == Category.DESTRUCTIVE, decision
    assert "mv it over" in decision.alternative


@pytest.mark.parametrize("command", ["sort a > b && mv b a", "sort a > b; mv b a"])
def test_a_sequential_write_is_not_read_as_self_emptying(command):
    """Not self-emptying: `sort` has finished before `mv` runs. Refused all
    the same, by round 8's rule that a write beside a non-reader (`mv`) runs
    on its own, so the message must be that one, not this round's."""
    decision = verdict(command)
    assert "mv it over" not in (decision.alternative or "")


@pytest.mark.parametrize("command", [
    "sort -o a a", "cat a | sort > b",
    "sort < a > b", "uniq a b", "cat a | sort >> a",
])
def test_a_sequential_or_distinct_write_passes(command):
    assert not verdict(command).deny, verdict(command).reason


# --- 3: gh state changes -------------------------------------------------------------

@pytest.mark.parametrize("command,alternative", [
    ("gh issue delete 1", "say in the PR"),
    ("gh label delete bug", "say in the PR"),
    ("gh repo delete a/b", "say in the PR"),
    ("gh run delete 5", "say in the PR"),
    ("gh repo archive a/b", "say in the PR"),
    ("gh repo rename x", "say in the PR"),
    ("gh pr merge 1", "never merges"),
    ("gh pr merge 1 --squash --auto", "never merges"),
    ("gh api -iX DELETE repos/a/b", "say in the PR"),
    ("gh api -iXDELETE repos/a/b", "say in the PR"),
    ("gh api graphql -f query='mutation { closeIssue }'", "through the API"),
    ("gh api graphql --raw-field query='Mutation{x}'", "through the API"),
    ("gh api graphql --input body.json", "through the API"),
])
def test_a_gh_state_change_is_refused(command, alternative):
    decision = verdict(command)
    assert decision.deny and decision.category == Category.DESTRUCTIVE, decision
    assert alternative in decision.alternative


@pytest.mark.parametrize("command", [
    "gh issue view 1", "gh pr view 1", "gh pr create --title x --body y",
    "gh api graphql -f query='query { viewer { login } }'", "gh api -X GET repos/a/b",
    "gh label list",
])
def test_reading_gh_passes(command):
    assert not verdict(command).deny, verdict(command).reason


# --- 4: git rm -r --cached --------------------------------------------------------

@pytest.mark.parametrize("command", [
    "git rm -r --cached .", "git rm --cached -r src", "git rm -rq --cached build",
])
def test_git_rm_recursive_cached_gets_per_file_advice(command):
    decision = verdict(command)
    assert decision.deny
    assert decision.alternative == "git rm --cached <path> for each file", decision


def test_git_rm_cached_of_a_file_passes():
    assert not verdict("git rm --cached a.txt").deny


# --- 5: an assignment in front of destructive git -----------------------------------

@pytest.mark.parametrize("command", [
    "GIT_DIR=x git reset --hard", "GIT_WORK_TREE=. git reset --hard HEAD~1",
    "FOO=1 BAR=2 git reset --hard",
])
def test_an_assignment_before_destructive_git_keeps_the_advice(command):
    decision = verdict(command)
    assert decision.deny and decision.alternative == "git stash", decision


def test_an_unlisted_assignment_before_a_harmless_command_keeps_its_refusal():
    decision = verdict("GIT_DIR=x git status")
    assert decision.deny and decision.category == Category.UNVERIFIABLE
