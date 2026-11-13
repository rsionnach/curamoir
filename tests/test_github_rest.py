"""cm-2qp.9: in a Claude Code cloud session every gh call goes through
`gh api repos/OWNER/REPO/...` (GitHub's REST API), because the session's
GitHub proxy refuses the GraphQL that `gh pr`, `gh label list` and `gh
repo view` use; everywhere else the CLI calls stay as they were.

Both paths are pinned here: the REST path's argv for each call, the
mapping of GitHub's recorded REST responses (tests/gh_api_payloads.py,
which says which are captured and which documented) to the shapes the
callers already read, LABEL_LIMIT's degradation and pagination on REST,
publish's create outcomes on REST, the default branch on REST, and two
sweeps through runner.run_job: one in a cloud session, where gh is the
proxy's (only `gh api` answers) and every call must be `gh api`, and one
on a laptop, where no call may be. The CLI path's own argv stay pinned
by tests/test_github.py and tests/test_publish.py, which this bead does
not change; the local sweep here checks them again end to end.

Every gh is tests/fake_gh.py, as in tests/test_github.py.
"""

import json
import subprocess
from datetime import datetime, timezone

import pytest

from curamoir import cli, github, green_gate, publish, runner
from curamoir.config import GithubApi, resolve_github_api
from curamoir.guards import Degraded, JobContext, KillSwitch
from curamoir.idempotence import GH_FILES_CAP, sweep_marker
from curamoir.scaffold import init_repo
from tests.fake_gh import FakeGh
from tests.gh_api_payloads import (
    CREATE_RESPONSE,
    DELETED_FORK_CLOSED,
    FORK_CLOSED,
    LABELS,
    NO_PULLS,
    NOT_FOUND_STDERR,
    NOT_FOUND_STDOUT,
    PROXY_GRAPHQL_PREFIX,
    PULL_FILES_SHAPE,
    PULLS_SHAPE,
    REPO,
    SAME_REPOSITORY_MERGED,
    SAME_REPOSITORY_OPEN,
)
from tests.gh_label_payloads import LABEL_NAMES_JQ
from tests.gh_pr_payloads import OPEN_PRS, SAME_REPOSITORY_PR, as_sweep_pr
from tests.os_sandbox_stand_in import Absent
from tests.test_brief import sweep_line, with_ledger
from tests.test_github import LABEL_ARGV, PR_ARGV, STATE_ARGV, UNPAUSED_LABELS
from tests.test_publish import GH_REPO, TITLE, World, cli_creates
from tests.test_sweep_acceptance import NOW, any_creates, sample, stand_in
from tests.test_sweep_job import (  # noqa: F401  (isolated_home is an autouse fixture)
    isolated_home,
    job_lines,
)

REST = GithubApi.REST
BASE = "a" * 40
#: Where the REST calls go for GH_REPO (github.com/acme/widgets).
HOST = ["--hostname", "github.com"]
ACME = "repos/acme/widgets"
REST_LABEL_ARGV = ["api", *HOST, f"{ACME}/labels?per_page=100&page=1"]
REST_PR_ARGV = ["api", *HOST, f"{ACME}/pulls?state=open&per_page=100&page=1"]
CLOUD = {"CLAUDE_CODE_REMOTE": "true"}


@pytest.fixture
def gh(tmp_path, monkeypatch):
    fake = FakeGh(tmp_path / "fake-gh", labels=UNPAUSED_LABELS)
    monkeypatch.setattr(github, "GH", str(fake.path))
    return fake


@pytest.fixture
def checkout(tmp_path):
    path = tmp_path / "checkout"
    path.mkdir()
    return path


def api_calls(gh):
    return [argv for argv in gh.calls() if argv[:1] == ["api"]]


# --- the decision ---------------------------------------------------------------------

@pytest.mark.parametrize("environ, expected", [
    ({}, GithubApi.CLI),
    ({"CLAUDE_CODE_REMOTE": "true"}, REST),
    # Whatever the runner kind: a cloud session running local is still
    # behind the proxy, and a GitHub Actions run (hosted) is not.
    ({"CLAUDE_CODE_REMOTE": "true", "CURAMOIR_RUNNER": "hosted"}, REST),
    ({"CLAUDE_CODE_REMOTE": "true", "CURAMOIR_RUNNER": "local"}, REST),
    ({"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted",
      "CURAMOIR_RUNNER": "hosted"}, GithubApi.CLI),
    # Exact, as the hosted markers are.
    ({"CLAUDE_CODE_REMOTE": "True"}, GithubApi.CLI),
    ({"CLAUDE_CODE_REMOTE": "1"}, GithubApi.CLI),
    ({"CLAUDE_CODE_REMOTE": " true"}, GithubApi.CLI),
    ({"CLAUDE_CODE_REMOTE": ""}, GithubApi.CLI),
])
def test_rest_is_chosen_by_claude_code_remote_exactly_true_alone(environ, expected):
    assert resolve_github_api(environ) is expected


# --- the REST argv, call by call ------------------------------------------------------------

def test_the_rest_label_query_pages_through_every_label_of_origins_repository(gh, checkout):
    names = github.repo_labels(checkout, gh_repo=GH_REPO, github_api=REST)

    assert names == [label["name"] for label in UNPAUSED_LABELS]
    assert gh.calls() == [REST_LABEL_ARGV]


def test_with_no_github_origin_the_rest_label_query_lets_gh_fill_the_repository(gh, checkout):
    """The CLI path's fallback (runner, "Which repository gh is asked
    about"), on REST: gh's own `{owner}/{repo}` placeholder, no host."""
    github.repo_labels(checkout, github_api=REST)

    assert gh.calls() == [["api", "repos/{owner}/{repo}/labels?per_page=100&page=1"]]


def test_the_rest_open_pr_query_pages_through_every_open_pr(gh, checkout):
    gh.feed(prs=OPEN_PRS)

    listed = github.open_sweep_prs(checkout, gh_repo=GH_REPO, github_api=REST)

    assert gh.calls() == [REST_PR_ARGV]  # no marker on any: no files asked for
    assert [record["branch"] for record in listed] == [pr["headRefName"] for pr in OPEN_PRS]


def test_the_rest_state_query_asks_page_by_page_and_stops_at_a_short_page(gh, checkout):
    gh.feed(prs=[{**SAME_REPOSITORY_PR, "number": n, "state": "OPEN"}
                 for n in range(250, 0, -1)])

    states = github.pr_states(checkout, gh_repo=GH_REPO, github_api=REST)

    assert gh.calls() == [["api", *HOST, f"{ACME}/pulls?state=all&per_page=100&page={page}"]
                          for page in (1, 2, 3)]
    assert sorted(states) == list(range(1, 251))


def test_the_rest_state_query_reads_no_more_than_the_newest_pr_limit(gh, checkout):
    """As `gh pr list --limit 1000` does: the newest PR_LIMIT, never every
    PR the repository ever had (a `--paginate` would read them all)."""
    gh.feed(prs=[{**SAME_REPOSITORY_PR, "number": n, "state": "OPEN"}
                 for n in range(1100, 0, -1)])

    states = github.pr_states(checkout, gh_repo=GH_REPO, github_api=REST)

    assert len(gh.calls()) == github.PR_LIMIT // github.REST_PER_PAGE == 10
    assert sorted(states) == list(range(101, 1101))


def test_the_rest_default_branch_is_the_repository_objects(tmp_path, gh):
    world = World(tmp_path)

    assert publish.origin_default_branch(world.repo, GH_REPO, github_api=REST) == "main"
    assert gh.calls() == [["api", *HOST, ACME]]


def test_a_rest_lookup_asks_for_the_branchs_open_prs_by_owner_and_branch(gh, checkout):
    branch = "curamoir/sweep-2026-10-24-aaaaaaaa"

    assert publish.find_open_pr(checkout, GH_REPO, branch, BASE, "main", github_api=REST) is None
    assert gh.calls() == [["api", *HOST, f"{ACME}/pulls?head=acme:curamoir/sweep-2026-10-24-"
                                         "aaaaaaaa&state=open&per_page=30"]]


@pytest.mark.parametrize("branch, query", [
    ("curamoir/a&b#c+d", "acme:curamoir/a%26b%23c%2Bd"),
    ("curamoir/sweep-é-ß", "acme:curamoir/sweep-%C3%A9-%C3%9F"),
    ("curamoir/a b?c=d", "acme:curamoir/a%20b%3Fc%3Dd"),
    # `%` is the escape itself, and `{}` gh's placeholder syntax.
    ("curamoir/a%26b", "acme:curamoir/a%2526b"),
    ("curamoir/{branch}", "acme:curamoir/%7Bbranch%7D"),
])
def test_a_rest_lookups_branch_is_percent_encoded_as_utf8(branch, query):
    assert github.rest_head_query("acme", branch) == query


@pytest.mark.parametrize("gh_repo, expected", [
    ("github.com/acme/widgets", (["--hostname", "github.com"], "repos/acme/widgets")),
    ("ghe.example:8443/acme/widgets", (["--hostname", "ghe.example:8443"],
                                       "repos/acme/widgets")),
    ("acme/widgets", ([], "repos/acme/widgets")),
])
def test_the_rest_target_is_origins_host_owner_and_repository(gh_repo, expected):
    assert github.rest_target(gh_repo) == expected


@pytest.mark.parametrize("gh_repo", [None, "", "widgets", "a/b/c/d", "github.com//widgets",
                                     "/acme/widgets"])
def test_a_repository_name_rest_cannot_use_is_a_gh_error_before_gh_runs(gh_repo, gh):
    with pytest.raises(github.GhError):
        github.rest_target(gh_repo)
    assert gh.calls() == []


# --- GitHub's recorded responses, mapped ---------------------------------------------------

def test_the_fake_answers_rest_in_githubs_recorded_shapes(gh, tmp_path):
    """tests/fake_gh.py's REST objects carry only keys GitHub's own
    responses carry (tests/gh_api_payloads.py), so the adapter is never
    tested against a key GitHub does not send."""
    gh.feed(prs=OPEN_PRS)

    def answer(*args):
        done = subprocess.run([str(gh.path), "api", *args], capture_output=True, text=True,
                              check=True)
        return json.loads(done.stdout)

    assert set(answer(f"{ACME}/labels")[0]) == set(LABELS[0])
    assert set(answer(ACME)) == set(REPO)
    pull = answer(f"{ACME}/pulls")[0]
    assert set(pull) <= set(SAME_REPOSITORY_OPEN)
    assert set(pull["head"]) == set(SAME_REPOSITORY_OPEN["head"])
    assert set(pull["head"]["repo"]) == set(SAME_REPOSITORY_OPEN["head"]["repo"])
    assert set(answer(f"{ACME}/pulls/103/files")[0]) == set(PULL_FILES_SHAPE[0])


def test_rest_labels_parse_as_the_cli_labels_do(gh, checkout):
    """The recorded REST labels, each label's `name`, give
    exactly the names `gh label list --jq` printed for the same repository
    (tests/gh_label_payloads.py's LABEL_NAMES_JQ)."""
    gh.feed(labels=LABELS)

    assert github.repo_labels(checkout, gh_repo=GH_REPO, github_api=REST) == (
        LABEL_NAMES_JQ.rstrip("\n").split("\n"))


@pytest.mark.parametrize("pull, fields", [
    (SAME_REPOSITORY_OPEN, {"state": "OPEN", "isCrossRepository": False, "mergedAt": None}),
    (SAME_REPOSITORY_MERGED, {"state": "MERGED", "isCrossRepository": False,
                              "mergedAt": SAME_REPOSITORY_MERGED["merged_at"]}),
    (FORK_CLOSED, {"state": "CLOSED", "isCrossRepository": True, "mergedAt": None}),
    # `head.repo` null: gh calls a deleted fork's PR cross-repository.
    (DELETED_FORK_CLOSED, {"state": "CLOSED", "isCrossRepository": True, "mergedAt": None}),
], ids=["open", "merged", "fork", "deleted-fork"])
def test_a_recorded_rest_pull_maps_to_the_cli_fields(pull, fields):
    mapped = github.rest_pull_fields(pull)

    assert mapped == {"number": pull["number"], "url": pull["html_url"], "body": pull["body"],
                      "title": pull["title"], "headRefName": pull["head"]["ref"],
                      "headRefOid": pull["head"]["sha"], "baseRefName": pull["base"]["ref"],
                      **fields}


@pytest.mark.parametrize("head, base, expected", [
    ({"repo": {"id": 1}}, {"repo": {"id": 1}}, False),
    ({"repo": {"id": 2}}, {"repo": {"id": 1}}, True),
    ({"repo": None}, {"repo": {"id": 1}}, True),
    ({}, {"repo": {"id": 1}}, None),
    ({"repo": {"id": "1"}}, {"repo": {"id": 1}}, None),
    ({"repo": {"id": True}}, {"repo": {"id": True}}, None),
    ({"repo": {"id": 1}}, {}, None),
    ({"repo": "acme/widgets"}, {"repo": {"id": 1}}, None),
])
def test_cross_repository_is_unknown_unless_github_ids_say(head, base, expected):
    """guards.pr_cap's trust boundary: only GitHub's own answer is True or
    False; anything else is unknown provenance (None), never trusted."""
    assert github.rest_cross_repository(head, base) is expected


@pytest.mark.parametrize("pull", [{}, {"head": "x", "base": 3, "state": "merged"},
                                  {"number": "7", "state": None}])
def test_a_malformed_rest_pull_maps_to_nones_and_never_raises(pull):
    mapped = github.rest_pull_fields(pull)

    assert mapped["state"] is None and mapped["headRefName"] is None
    assert mapped["isCrossRepository"] is None


def test_recorded_rest_pulls_give_the_brief_the_cli_paths_states(gh, checkout):
    gh.feed(rest_pulls=PULLS_SHAPE)

    states = github.pr_states(checkout, gh_repo="github.com/cli/cli", github_api=REST)

    assert {number: state["state"] for number, state in states.items()} == {
        14846: "OPEN", 14879: "MERGED", 14885: "CLOSED", 14823: "CLOSED"}
    assert states[14879]["merged_at"] == datetime(2026, 10, 30, 18, 24, 32,
                                                  tzinfo=timezone.utc)
    assert states[14846]["branch"] == SAME_REPOSITORY_OPEN["head"]["ref"]


def test_recorded_rest_pulls_give_the_listing_contract_with_files_for_sweep_prs(gh, checkout):
    """The recorded open same-repository pull, its body edited to carry a
    sweep marker (the one edit; tests/gh_pr_payloads.as_sweep_pr does the
    same on the CLI side), is listed with its recorded files' names; the
    fork's record is listed (and later dropped by the trust boundary)
    without a files call."""
    sweep_pull = {**SAME_REPOSITORY_OPEN, "body": f"Sweep.\n\n{sweep_marker(BASE)}\n"}
    fork_open = {**FORK_CLOSED, "state": "open"}
    gh.feed(rest_pulls=[sweep_pull, fork_open], rest_files={"14846": PULL_FILES_SHAPE})

    listed = github.open_sweep_prs(checkout, gh_repo="github.com/cli/cli", github_api=REST)

    assert listed == [
        {"branch": sweep_pull["head"]["ref"], "number": 14846, "url": sweep_pull["html_url"],
         "base": BASE, "paths": sorted(entry["filename"] for entry in PULL_FILES_SHAPE),
         "cross_repository": False},
        {"branch": fork_open["head"]["ref"], "number": 14885, "url": fork_open["html_url"],
         "base": None, "paths": None, "cross_repository": True},
    ]
    assert api_calls(gh)[1:] == [["api", "--hostname", "github.com",
                                  f"repos/cli/cli/pulls/14846/files?per_page={GH_FILES_CAP}"]]


def test_a_sweep_pr_with_the_files_cap_or_more_has_unknown_paths_on_rest(gh, checkout):
    """GraphQL's `files` is the first GH_FILES_CAP; REST's one page is the
    same, and as long a list may be cut short (sweep_pr_listing)."""
    pr = as_sweep_pr(SAME_REPOSITORY_PR, "curamoir/sweep-x", BASE)
    pr["files"] = [{"path": f"f{i}.py"} for i in range(GH_FILES_CAP + 5)]
    gh.feed(prs=[pr])

    [record] = github.open_sweep_prs(checkout, gh_repo=GH_REPO, github_api=REST)

    assert record["base"] == BASE and record["paths"] is None


@pytest.mark.parametrize("files", [{"message": "API rate limit exceeded"}, 3],
                         ids=["error-object", "scalar"])
def test_a_files_answer_that_is_not_a_list_at_exit_0_is_a_gh_error(gh, checkout, files):
    """An error object or a scalar where a sweep PR's `/files` page should
    be is refused, never iterated as its keys or a TypeError."""
    sweep_pull = {**SAME_REPOSITORY_OPEN, "body": f"Sweep.\n\n{sweep_marker(BASE)}\n"}
    gh.feed(rest_pulls=[sweep_pull], rest_files={"14846": files})

    with pytest.raises(github.GhError, match="not a list of files"):
        github.open_sweep_prs(checkout, gh_repo="github.com/cli/cli", github_api=REST)


def test_the_recorded_empty_repository_lists_no_open_prs_on_rest(gh, checkout):
    gh.feed(rest_pulls=NO_PULLS)

    assert github.open_sweep_prs(checkout, gh_repo=GH_REPO, github_api=REST) == []


@pytest.mark.parametrize("call", [
    lambda repo: github.repo_labels(repo, gh_repo=GH_REPO, github_api=REST),
    lambda repo: github.open_sweep_prs(repo, gh_repo=GH_REPO, github_api=REST),
    lambda repo: github.pr_states(repo, gh_repo=GH_REPO, github_api=REST),
], ids=["labels", "open-prs", "states"])
def test_a_rest_error_is_a_gh_error_quoting_githubs_message(gh, checkout, call):
    """As recorded: `gh api` exits 1 with `gh: Not Found (HTTP 404)` on
    stderr and GitHub's error body on stdout. The error quotes the stderr;
    the stdout's JSON is never read as an answer."""
    gh.feed(api_fail_on={ACME: NOT_FOUND_STDERR.strip()})

    with pytest.raises(github.GhError, match=r"exited 1: gh: Not Found \(HTTP 404\)") as failed:
        call(checkout)
    assert "documentation_url" not in str(failed.value)
    assert subprocess.run([str(gh.path), "api", ACME], capture_output=True,
                          text=True).stdout == NOT_FOUND_STDOUT


@pytest.mark.parametrize("printed", ["not json", "{}", "[{}, 3]", "[[{}], 3]"])
def test_a_rest_listing_that_is_not_pages_of_pulls_is_a_gh_error(gh, checkout, printed):
    gh.feed(raw_stdout=printed.encode().hex())

    with pytest.raises(github.GhError):
        github.open_sweep_prs(checkout, gh_repo=GH_REPO, github_api=REST)


@pytest.mark.parametrize("printed", ['[{"name": 7}]', "[{}]"])
def test_a_rest_label_without_a_text_name_is_the_adapters_own_error(gh, checkout, printed):
    gh.feed(raw_stdout=printed.encode().hex())

    with pytest.raises(github.GhError, match="without a name"):
        github.repo_labels(checkout, gh_repo=GH_REPO, github_api=REST)


@pytest.mark.parametrize("provenance", [{}, {"isCrossRepository": None}],
                         ids=["missing", "unknown"])
def test_a_sweep_pr_of_unknown_provenance_gets_no_files_call(gh, checkout, provenance):
    """Its paths are never used (unknown provenance), so they are never
    asked for: no `/files` call, `paths` None."""
    pr = {key: value for key, value in as_sweep_pr(SAME_REPOSITORY_PR, "curamoir/sweep-x",
                                                   BASE).items()
          if key != "isCrossRepository"}
    gh.feed(prs=[{**pr, **provenance}])

    [record] = github.open_sweep_prs(checkout, gh_repo=GH_REPO, github_api=REST)

    assert record["cross_repository"] is None and record["paths"] is None
    assert not any("/files" in argv[-1] for argv in gh.calls())


# --- LABEL_LIMIT and pagination on REST -----------------------------------------------------

def test_rest_labels_are_read_across_pages(gh, checkout):
    labels = [{"name": f"label-{i:03}"} for i in range(250)]
    gh.feed(labels=[*labels, {"name": "curamoir:paused"}])

    names = github.repo_labels(checkout, gh_repo=GH_REPO, github_api=REST)

    assert names == [label["name"] for label in labels] + ["curamoir:paused"]
    verdict = KillSwitch(lambda: names)(JobContext(job="sweep", repo=checkout))
    assert verdict is not None and not isinstance(verdict, Degraded)


@pytest.mark.parametrize("count", [github.LABEL_LIMIT, github.LABEL_LIMIT + 1])
def test_rest_labels_reaching_label_limit_leave_the_pause_label_never_checked(gh, checkout,
                                                                              count):
    """The CLI path's degradation, kept on REST: a listing as long as
    LABEL_LIMIT may not hold the pause label, so the kill switch says it
    never checked (Degraded), never "not paused"."""
    gh.feed(labels=[{"name": f"label-{i:04}"} for i in range(count)])

    # Read page by page up to LABEL_LIMIT, so at most LABEL_LIMIT are read.
    with pytest.raises(github.GhError, match="listed 1000 labels, as many as LABEL_LIMIT"):
        github.repo_labels(checkout, gh_repo=GH_REPO, github_api=REST)
    assert len(gh.calls()) == github.LABEL_LIMIT // 100
    verdict = KillSwitch(lambda: github.repo_labels(checkout, gh_repo=GH_REPO, github_api=REST))(
        JobContext(job="sweep", repo=checkout))
    assert isinstance(verdict, Degraded)


@pytest.mark.parametrize("printed", [
    "<!DOCTYPE html><html><body>curamoir:paused</body></html>",
    '{"message": "API rate limit exceeded", "status": "403"}',
    '[{"name": "bug"}, "curamoir:paused"]',
    '[{"name": 7}]',
    "",
])
def test_a_rest_label_answer_that_is_not_labels_at_exit_0_is_degraded_not_unpaused(
        gh, checkout, printed):
    """HTML, an error object or anything else at exit 0 is a GhError, so
    the kill switch says it never checked (Degraded), never "not paused"."""
    gh.feed(raw_stdout=printed.encode().hex())

    verdict = KillSwitch(lambda: github.repo_labels(checkout, gh_repo=GH_REPO,
                                                    github_api=REST))(
        JobContext(job="sweep", repo=checkout))

    assert isinstance(verdict, Degraded)


def test_rest_labels_one_short_of_label_limit_are_read(gh, checkout):
    gh.feed(labels=[{"name": f"label-{i:04}"} for i in range(github.LABEL_LIMIT - 1)])

    assert len(github.repo_labels(checkout, gh_repo=GH_REPO, github_api=REST)) == (
        github.LABEL_LIMIT - 1)


def test_a_label_page_that_fails_part_way_is_never_read_as_unpaused(gh, checkout):
    """gh prints the pages it read and then exits 1 when a later page
    fails: the listing is cut, so it is an error (Degraded), not the
    names before the cut."""
    gh.feed(labels=[{"name": f"label-{i:03}"} for i in range(250)], api_fail_at_page=2)

    with pytest.raises(github.GhError, match="HTTP 502"):
        github.repo_labels(checkout, gh_repo=GH_REPO, github_api=REST)
    verdict = KillSwitch(lambda: github.repo_labels(checkout, gh_repo=GH_REPO, github_api=REST))(
        JobContext(job="sweep", repo=checkout))
    assert isinstance(verdict, Degraded)


def test_rest_open_prs_are_read_across_pages(gh, checkout):
    gh.feed(prs=[{**SAME_REPOSITORY_PR, "number": n} for n in range(1, 151)])

    listed = github.open_sweep_prs(checkout, gh_repo=GH_REPO, github_api=REST)

    assert [record["number"] for record in listed] == list(range(1, 151))


@pytest.mark.parametrize("count", [github.PR_LIMIT, github.PR_LIMIT + 150])
def test_rest_open_prs_reaching_pr_limit_ask_bounded_pages_and_are_cut_short(gh, checkout,
                                                                              count):
    """Page by hand, never `--paginate`: at most PR_LIMIT / 100 pages are
    asked however many PRs are open, and a listing that reaches PR_LIMIT
    is refused as possibly cut short, as on the CLI path. The refusal
    names PR_LIMIT, not the `--limit` this path never passes."""
    gh.feed(prs=[{**SAME_REPOSITORY_PR, "number": n} for n in range(1, count + 1)])

    with pytest.raises(github.GhError, match=rf"open pull requests, as many as PR_LIMIT "
                                             rf"\({github.PR_LIMIT}\), so the list may be "
                                             "cut short"):
        github.open_sweep_prs(checkout, gh_repo=GH_REPO, github_api=REST)
    assert gh.calls() == [["api", *HOST, f"{ACME}/pulls?state=open&per_page=100&page={page}"]
                          for page in range(1, github.PR_LIMIT // 100 + 1)]


def test_an_open_pr_page_that_fails_part_way_is_a_gh_error(gh, checkout):
    gh.feed(prs=[{**SAME_REPOSITORY_PR, "number": n} for n in range(1, 151)],
            api_fail_at_page=2)

    with pytest.raises(github.GhError, match="HTTP 502"):
        github.open_sweep_prs(checkout, gh_repo=GH_REPO, github_api=REST)


def test_a_rest_label_listing_at_a_limit_below_one_page_raises(gh, checkout, monkeypatch):
    """tests/test_github.py's LABEL_LIMIT=3 case, on REST: a limit below one
    page still reads a page, and four labels reach it. Never [] unasked."""
    monkeypatch.setattr(github, "LABEL_LIMIT", 3)
    gh.feed(labels=[{"name": f"label-{i}"} for i in range(4)])

    with pytest.raises(github.GhError, match="may be cut short"):
        github.repo_labels(checkout, gh_repo=GH_REPO, github_api=REST)
    assert gh.calls() == [REST_LABEL_ARGV]


def test_a_rest_pr_listing_at_a_limit_below_one_page_raises(gh, checkout, monkeypatch):
    monkeypatch.setattr(github, "PR_LIMIT", 2)
    gh.feed(prs=[{**SAME_REPOSITORY_PR, "number": n} for n in (1, 2, 3)])

    with pytest.raises(github.GhError, match="may be cut short"):
        github.open_sweep_prs(checkout, gh_repo=GH_REPO, github_api=REST)
    assert gh.calls() == [REST_PR_ARGV]


def test_a_limit_that_is_not_a_whole_page_is_still_reached(gh, checkout, monkeypatch):
    """150 with 160 open: page 2 is read, 160 >= 150, refused."""
    monkeypatch.setattr(github, "PR_LIMIT", 150)
    gh.feed(prs=[{**SAME_REPOSITORY_PR, "number": n} for n in range(1, 161)])

    with pytest.raises(github.GhError, match="may be cut short"):
        github.open_sweep_prs(checkout, gh_repo=GH_REPO, github_api=REST)
    assert len(gh.calls()) == 2


def test_a_listing_under_a_limit_that_is_not_a_whole_page_is_read_whole(gh, checkout,
                                                                        monkeypatch):
    monkeypatch.setattr(github, "PR_LIMIT", 150)
    gh.feed(prs=[{**SAME_REPOSITORY_PR, "number": n} for n in range(1, 121)])

    listed = github.open_sweep_prs(checkout, gh_repo=GH_REPO, github_api=REST)

    assert len(listed) == 120 and len(gh.calls()) == 2


def test_a_limit_above_whole_pages_reads_past_them(gh, checkout, monkeypatch):
    """1050 with 1100 labels: 11 pages, not 10, so the limit is reached."""
    monkeypatch.setattr(github, "LABEL_LIMIT", 1050)
    gh.feed(labels=[{"name": f"label-{i:04}"} for i in range(1100)])

    with pytest.raises(github.GhError, match="listed 1100 labels"):
        github.repo_labels(checkout, gh_repo=GH_REPO, github_api=REST)
    assert len(gh.calls()) == 11


def test_a_limit_just_past_one_page_reads_a_second_page_and_raises(gh, checkout,
                                                                   monkeypatch):
    """PR_LIMIT=101 with 150 open: page 1 (100) is short of it, page 2 is
    read, 150 >= 101, refused."""
    monkeypatch.setattr(github, "PR_LIMIT", 101)
    gh.feed(prs=[{**SAME_REPOSITORY_PR, "number": n} for n in range(1, 151)])

    with pytest.raises(github.GhError, match="may be cut short"):
        github.open_sweep_prs(checkout, gh_repo=GH_REPO, github_api=REST)
    assert len(gh.calls()) == 2


# --- publish on REST -----------------------------------------------------------------------

@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(publish, "LOOKUP_DELAY", 0)
    return World(tmp_path)


def rest_creates(gh):
    return [record for record in gh.call_records()
            if record["argv"][:1] == ["api"] and "POST" in record["argv"]]


def rest_lookups(gh):
    return [argv for argv in api_calls(gh) if "/pulls?head=" in argv[-1]]


def test_a_rest_publish_opens_the_pr_with_the_body_on_stdin_never_in_argv(world, no_real_gh):
    published = world.publish(github_api=REST)

    assert (published.number, published.url) == (1, "https://github.com/acme/widgets/pull/1")
    [create] = rest_creates(no_real_gh)
    assert create["argv"] == ["api", *HOST, "-X", "POST", f"{ACME}/pulls", "--input", "-"]
    assert json.loads(create["input_content"]) == {
        "title": TITLE,
        "head": world.branch, "base": "main", "body": world.body}
    assert world.body not in " ".join(create["argv"])
    # Only `gh api` was asked: the default branch, then the create.
    assert no_real_gh.calls() == [["api", *HOST, ACME], create["argv"]]
    assert cli_creates(no_real_gh) == []


def test_a_rest_create_read_from_githubs_documented_response(world, no_real_gh):
    """GitHub's documented 201 response (tests/gh_api_payloads.py,
    documented, not captured), with the example's repository and number
    swapped for origin's and the PR's: its html_url and number are the PR."""
    response = {**CREATE_RESPONSE, "number": 1,
                "html_url": "https://github.com/acme/widgets/pull/1"}
    no_real_gh.feed(create_stdout=json.dumps(response))

    published = world.publish(github_api=REST)

    assert (published.number, published.url) == (1, "https://github.com/acme/widgets/pull/1")
    assert rest_lookups(no_real_gh) == []


def test_a_rest_create_github_refused_deletes_the_branch(world, no_real_gh):
    """A definite failure (GitHub's 422, nothing made): one lookup finds
    no PR, the branch is deleted, PrCreateFailed: the CLI path's row."""
    no_real_gh.feed(api_fail_on={"POST repos/acme/widgets/pulls":
                                 "gh: Validation Failed (HTTP 422)"})

    with pytest.raises(publish.PrCreateFailed, match="Validation Failed") as failed:
        world.publish(github_api=REST)

    assert failed.value.branch_deleted is True and failed.value.timed_out is False
    assert world.origin_branches() == ["refs/heads/main"]
    assert len(rest_lookups(no_real_gh)) == 1


def test_a_rest_create_that_errored_after_opening_the_pr_finds_it(world, no_real_gh):
    no_real_gh.feed(create_fails_with="gh: Bad Gateway (HTTP 502)")

    published = world.publish(github_api=REST)

    assert published.number == 1
    assert world.origin_branches() == ["refs/heads/main", f"refs/heads/{world.branch}"]


def test_a_rest_create_that_timed_out_after_opening_the_pr_finds_it(world, no_real_gh,
                                                                    monkeypatch):
    no_real_gh.feed(create_hang="after")
    monkeypatch.setattr(publish, "CREATE_TIMEOUT", 2)

    published = world.publish(github_api=REST)

    assert (published.number, published.url) == (1, "https://github.com/acme/widgets/pull/1")
    assert rest_lookups(no_real_gh)


def test_a_rest_create_that_timed_out_without_a_pr_is_looked_for_then_deleted(
        world, no_real_gh, monkeypatch):
    no_real_gh.feed(create_hang="before")
    monkeypatch.setattr(publish, "CREATE_TIMEOUT", 2)

    with pytest.raises(publish.PrCreateFailed, match="did not finish within 2") as failed:
        world.publish(github_api=REST)

    assert failed.value.timed_out is True and failed.value.branch_deleted is True
    assert len(rest_lookups(no_real_gh)) == publish.LOOKUP_ATTEMPTS


@pytest.mark.parametrize("printed", [
    "not json",
    json.dumps({"html_url": "https://github.com/someone/else/pull/1", "number": 1}),
    json.dumps({"html_url": "https://github.com/acme/widgets/pull/1", "number": 2}),
    json.dumps(["https://github.com/acme/widgets/pull/1"]),
])
def test_a_rest_create_whose_answer_is_not_the_pr_is_unknown_and_the_lookup_decides(
        world, no_real_gh, printed):
    """Exit 0 with an answer that is not a PR on origin's repository: the
    outcome is unknown (CreateOutputUnreadable), so the lookup decides,
    and finds the PR GitHub made."""
    no_real_gh.feed(create_stdout=printed)

    published = world.publish(github_api=REST)

    assert (published.number, published.url) == (1, "https://github.com/acme/widgets/pull/1")
    assert rest_lookups(no_real_gh)


def test_a_rest_create_and_lookup_that_both_fail_leave_the_branch(world, no_real_gh):
    no_real_gh.feed(api_fail_on={"POST repos/acme/widgets/pulls": "gh: HTTP 502",
                                 "GET repos/acme/widgets/pulls?head": "gh: HTTP 503"})

    with pytest.raises(publish.PrOutcomeUnknown, match="HTTP 503"):
        world.publish(github_api=REST)

    assert world.origin_branches() == ["refs/heads/main", f"refs/heads/{world.branch}"]


def test_a_rest_rate_limit_on_the_create_with_the_lookup_failing_is_unknown(world,
                                                                           no_real_gh):
    """A 403 secondary rate limit is any other GhError (no retry): one
    lookup, and when that fails too the outcome is unknown and the branch
    is kept, since deleting it would close a PR that may exist."""
    no_real_gh.feed(api_fail_on={
        "POST repos/acme/widgets/pulls": "gh: You have exceeded a secondary rate limit "
                                         "(HTTP 403)",
        "GET repos/acme/widgets/pulls?head": "gh: You have exceeded a secondary rate limit "
                                             "(HTTP 403)"})

    with pytest.raises(publish.PrOutcomeUnknown, match="secondary rate limit"):
        world.publish(github_api=REST)

    assert len(rest_lookups(no_real_gh)) == 1
    assert world.origin_branches() == ["refs/heads/main", f"refs/heads/{world.branch}"]


def test_a_rest_create_whose_input_cannot_be_made_never_runs_gh_and_deletes_the_branch(
        world, no_real_gh, monkeypatch):
    """The stdin file is not gh's: an OSError making it is an OSError (gh
    never ran), never "is the GitHub CLI installed", and publish's
    OSError row deletes the branch with no lookup."""
    def no_space(*_args, **_kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(github.tempfile, "TemporaryFile", no_space)

    with pytest.raises(publish.PrCreateFailed, match="No space left") as failed:
        world.publish(github_api=REST)

    assert "GitHub CLI installed" not in str(failed.value)
    assert failed.value.branch_deleted is True
    assert rest_creates(no_real_gh) == [] and rest_lookups(no_real_gh) == []


def test_a_rest_create_body_that_cannot_be_encoded_raises_before_gh_runs(gh, checkout):
    """A lone surrogate cannot be UTF-8: ValueError (UnicodeEncodeError),
    and gh is never asked, as _rest_create_pr's docstring says."""
    with pytest.raises(ValueError):
        publish.create_pr(checkout, gh_repo=GH_REPO, branch="curamoir/sweep-x",
                          base_branch="main", title=TITLE, body="\ud800", github_api=REST)
    assert gh.calls() == []


def test_a_rest_lookup_ignores_a_fork_and_another_commit(world, no_real_gh):
    """The lookup's filter is the CLI path's, on the mapped fields: the
    same branch name from a fork, or at another commit, is not the PR."""
    branch, sha = world.branch, "c" * 40
    no_real_gh.feed(prs=[
        {"headRefName": branch, "number": 5, "url": "https://github.com/acme/widgets/pull/5",
         "isCrossRepository": True, "headRefOid": sha, "baseRefName": "main"},
        {"headRefName": branch, "number": 6, "url": "https://github.com/acme/widgets/pull/6",
         "isCrossRepository": False, "headRefOid": "d" * 40, "baseRefName": "main"},
        {"headRefName": branch, "number": 7, "url": "https://github.com/acme/widgets/pull/7",
         "isCrossRepository": False, "headRefOid": sha, "baseRefName": "main"},
    ])

    assert publish.find_open_pr(world.repo, GH_REPO, branch, sha, "main", github_api=REST) == (
        7, "https://github.com/acme/widgets/pull/7")


def test_a_rest_default_branch_github_cannot_name_is_refused_before_the_push(world,
                                                                            no_real_gh):
    no_real_gh.feed(api_fail_on={f"GET {ACME}": NOT_FOUND_STDERR.strip()})

    with pytest.raises(publish.PublishRefused, match=r"Not Found \(HTTP 404\)"):
        world.publish(github_api=REST)

    assert world.origin_branches() == ["refs/heads/main"]
    assert rest_creates(no_real_gh) == []


def test_a_rest_repository_object_without_a_default_branch_is_refused(world, no_real_gh):
    no_real_gh.feed(rest_repo={key: value for key, value in REPO.items()
                               if key != "default_branch"}, default_branch="")

    with pytest.raises(publish.PublishRefused, match=r"not github\.com/acme/widgets.s default"):
        world.publish(github_api=REST)


# --- whole runs ----------------------------------------------------------------------------

def test_a_cloud_sweep_asks_github_only_through_gh_api(tmp_path, no_real_gh, monkeypatch):
    """CURAMOIR_RUNNER=hosted in a cloud session, through runner.run_job,
    with gh answering as the proxy does (every non-`gh api` call gets the
    GraphQL 403): every call is `gh api`, none is `pr`, `label` or `repo
    view`, and the sweep opens its one PR."""
    monkeypatch.setattr(publish, "LOOKUP_DELAY", 0)
    monkeypatch.setattr(green_gate, "OS_SANDBOX", Absent())
    repo = sample(tmp_path)
    stand_in(tmp_path, monkeypatch)
    no_real_gh.feed(proxy=True)

    ran = runner.run_job(repo, "sweep", now=lambda: NOW,
                         environ={"CURAMOIR_RUNNER": "hosted", **CLOUD})

    assert ran.exit_code == runner.EXIT_SUCCESS, ran.sentence
    calls = no_real_gh.calls()
    assert calls and all(argv[0] == "api" for argv in calls), calls
    assert not any(argv[:1] in (["pr"], ["label"], ["issue"]) or argv[:2] == ["repo", "view"]
                   for argv in calls)
    [create] = any_creates(no_real_gh)
    assert create["argv"] == ["api", *HOST, "-X", "POST", f"{ACME}/pulls", "--input", "-"]
    assert PROXY_GRAPHQL_PREFIX not in ran.sentence
    assert job_lines(repo)[-1]["github_api"] == "rest"


def test_a_local_sweep_asks_github_only_through_the_cli_calls(tmp_path, no_real_gh,
                                                              monkeypatch):
    """CLAUDE_CODE_REMOTE unset: the 3f50a3f calls, exactly, and no `gh api`."""
    monkeypatch.setattr(publish, "LOOKUP_DELAY", 0)
    repo = sample(tmp_path)
    stand_in(tmp_path, monkeypatch)

    ran = runner.run_job(repo, "sweep", now=lambda: NOW, environ={})

    assert ran.exit_code == runner.EXIT_SUCCESS, ran.sentence
    calls = no_real_gh.calls()
    assert api_calls(no_real_gh) == []
    repo_option = ["--repo", "github.com/acme/widgets"]
    assert calls[:4] == [
        [*LABEL_ARGV[:2], *repo_option, *LABEL_ARGV[2:]],
        [*PR_ARGV[:2], *repo_option, *PR_ARGV[2:]],
        ["repo", "view", "github.com/acme/widgets", "--json", "defaultBranchRef"],
        [*PR_ARGV[:2], *repo_option, *PR_ARGV[2:]],
    ]
    assert [argv[:2] for argv in calls[4:]] == [["repo", "view"], ["pr", "create"]]
    assert "github_api" not in job_lines(repo)[-1]


def github_origin_repo(harness):
    """harness.repo initialised, with a GitHub origin (acme/widgets)."""
    init_repo(harness.repo)
    subprocess.run(["git", "remote", "add", "origin", "https://github.com/acme/widgets.git"],
                   cwd=harness.repo, check=True, capture_output=True)
    return harness.repo


def brief_repo(harness):
    """github_origin_repo with one sweep PR on its ledger, so the brief
    asks gh what became of it."""
    return with_ledger(github_origin_repo(harness), sweep_line(12, 3))


def test_a_cloud_brief_asks_for_pr_states_through_gh_api(no_real_gh, harness, monkeypatch,
                                                         capsys):
    """cli's `brief` runs outside run_guarded, so it decides at its own
    entry point, from CLAUDE_CODE_REMOTE, as run_job does."""
    repo = brief_repo(harness)
    no_real_gh.feed(proxy=True)
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")

    assert cli.main(["brief", "--repo", str(repo)]) == 0

    assert no_real_gh.calls() == [["api", *HOST, f"{ACME}/pulls?state=all&per_page=100&page=1"]]


def test_a_scheduled_cloud_brief_asks_for_pr_states_through_gh_api(no_real_gh, harness):
    """The brief job through runner.run_job (a Routine's scheduled brief):
    run.github_api reaches its state query, so behind the proxy it is the
    REST listing, after the kill switch's label check."""
    repo = brief_repo(harness)
    no_real_gh.feed(proxy=True)

    ran = runner.run_job(repo, "brief", environ=CLOUD)

    assert ran.exit_code == runner.EXIT_SUCCESS, ran.sentence
    assert no_real_gh.calls() == [REST_LABEL_ARGV,
                                  ["api", *HOST, f"{ACME}/pulls?state=all&per_page=100&page=1"]]


def test_a_local_brief_asks_for_pr_states_through_gh_pr_list(no_real_gh, harness, capsys):
    repo = brief_repo(harness)

    assert cli.main(["brief", "--repo", str(repo)]) == 0

    assert no_real_gh.calls() == [[*STATE_ARGV[:2], "--repo", "github.com/acme/widgets",
                                   *STATE_ARGV[2:]]]


def test_the_run_job_decision_reaches_the_label_check_the_cap_and_the_body(
        no_real_gh, harness, monkeypatch):
    """A body standing in for the sweep reads `run.github_api`: the one
    decision run_job made from `environ`, and the guards' calls used it."""
    github_origin_repo(harness)
    seen = []
    monkeypatch.setitem(runner.JOBS, "sweep", runner.Job(
        body=lambda _root, run: (seen.append(run.github_api), run.succeeded("ok")),
        opens_prs=True, open_pr_listing_for=runner.JOBS["sweep"].open_pr_listing_for))

    runner.run_job(harness.repo, "sweep", environ=CLOUD)

    assert seen == [REST]
    assert no_real_gh.calls() == [REST_LABEL_ARGV, REST_PR_ARGV]


@pytest.mark.parametrize("environ, field", [
    (CLOUD, {"github_api": "rest"}),
    ({"CLAUDE_CODE_REMOTE": "true", "CURAMOIR_RUNNER": "hosted"}, {"github_api": "rest"}),
    ({}, {}),
    ({"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted",
      "CURAMOIR_RUNNER": "hosted"}, {}),
], ids=["cloud-local", "cloud-hosted", "laptop", "actions"])
def test_the_job_line_says_rest_only_when_github_was_asked_through_rest(
        no_real_gh, harness, monkeypatch, environ, field):
    """`github_api: rest` on a cloud run's line; nothing on a laptop's or
    an Actions run's, whose lines stay as before cm-2qp.9."""
    init_repo(harness.repo)
    monkeypatch.setitem(runner.JOBS, "brief", runner.Job(
        body=lambda _root, run: run.succeeded("ok"), opens_prs=False))

    runner.run_job(harness.repo, "brief", environ=environ)

    [line] = [entry for entry in harness.ledger().entries() if entry["kind"] == "job"]
    assert {key: line[key] for key in ("github_api",) if key in line} == field


def test_the_github_api_field_is_reserved():
    from curamoir.job_run import RESERVED_FIELDS

    assert "github_api" in RESERVED_FIELDS
    assert GithubApi.REST.ledger_fields() == {"github_api": "rest"}
    assert GithubApi.CLI.ledger_fields() == {}
