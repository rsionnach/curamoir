"""Round 12 (re-scoped lens): advice that fits what was refused.

1. gh deletions and changes get per-noun advice: close an issue rather than
   delete it; leave settings-like things (labels, releases, secrets, repo
   archive/rename) to the PR's reader; the branch wording only where a branch
   is what would go.
2. `gh release delete` gets that advice, not "file a bead" (release is not on
   gh's allowlist; the destructive rule's advice is preferred over the
   allowlist's generic refusal for gh, as for a wrapper).
3. Any `gh api` that is not a GET or HEAD is a state change: an explicit
   method, or fields without one (gh then POSTs). graphql is refused for a
   mutation or a body from a file.
4. `delete` among the first three gh positionals (`gh repo deploy-key delete`).
5. The self-emptying refusal says to run the write and the mv separately.
"""

import pytest

from curamoir.hooks import judge_command

API = "a job reads GitHub with gh api; it does not change it"
SETTINGS = "say in the PR what should change; a job does not change the repository's settings"
BRANCH = "leave the branch; say in the PR what should be deleted"


def verdict(command):
    return judge_command(command, in_job=True)


@pytest.mark.parametrize("command,alternative", [
    ("gh issue delete 1", "close it instead (gh issue close)"),
    ("gh label delete bug", SETTINGS),
    ("gh run delete 5", SETTINGS),
    ("gh release delete v1", SETTINGS),
    ("gh repo archive a/b", SETTINGS),
    ("gh repo rename x", SETTINGS),
    ("gh repo deploy-key delete 1", SETTINGS),
    ("gh pr close 1 --delete-branch", BRANCH),
    ("gh api -X DELETE repos/o/r/git/refs/heads/x", BRANCH),
    ("gh api -X DELETE repos/o/r", API),
    ("gh api -X PATCH repos/o/r", API),
    ("gh api repos/o/r/pulls/1/merge -X PUT", API),
    ("gh api repos/o/r/issues -f title=x", API),
    ("gh api --method=post repos/o/r/issues", API),
    ("gh api -iXPOST repos/o/r/issues", API),
    ("gh api repos/o/r/issues --input body.json", API),
    ("gh api -H Accept:x graphql -f query='mutation{x}'", API),
    ("gh api graphql -F query=@q.graphql", API),
    ("gh api graphql --input q.json", API),
])
def test_gh_changes_get_advice_that_fits(command, alternative):
    decision = verdict(command)
    assert decision.deny, command
    assert decision.alternative == alternative, decision.alternative


@pytest.mark.parametrize("command", [
    "gh api repos/o/r", "gh api -X GET search/issues -f q=x",
    "gh api graphql -f query='{viewer{login}}'", "gh api --method HEAD repos/o/r",
    "gh api -H Accept:x repos/o/r/pulls", "gh api -q .name repos/o/r",
])
def test_gh_reads_pass(command):
    assert not verdict(command).deny, verdict(command).reason


@pytest.mark.parametrize("command", ["sort a > a", "cat a | sort > a"])
def test_the_self_emptying_advice_says_two_commands(command):
    assert verdict(command).alternative == (
        "write to a new file, then mv it over, as two separate commands")
