"""cm-fkd.10: the `gh` adapter (curamoir.github) and the runner's wiring of it.

Every `gh` here is tests/fake_gh.py, a real script run through the
adapter's real subprocess path and fed gh's recorded output
(tests/gh_label_payloads.py, tests/gh_pr_payloads.py), pointed at by the
adapter's seam, `github.GH`, and, for child processes, by PATH
(tests/conftest.py, `no_real_gh`). Nothing here calls the real gh.

The runner tests drive `runner.run_job` and `cli.main(["run", ...])` over
the harness's real git repo initialised by `scaffold.init_repo`, and read
the lines back from the repo's own ledger, as tests/test_runner.py does.
"""

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from curamoir import cli, github, runner
from curamoir.guards import Degraded, JobContext, KillSwitch, OpenPrCap, Refusal
from curamoir.guards.pr_cap import DEFAULT_PREFIX
from curamoir.idempotence import (
    DUPLICATE_OPEN_PR,
    SWEEP_BRANCH_PREFIX,
    UNKNOWN,
    WORK,
    sweep_branch_prefix,
    sweep_decision,
)
from curamoir.runner import EXIT_FAILURE, EXIT_REFUSED, EXIT_SUCCESS, JOBS, run_job
from curamoir.scaffold import init_repo
from tests.fake_gh import GH_DEFAULT_LIMIT, FakeGh
from tests.gh_label_payloads import LABEL_LIST, LABEL_NAMES_JQ
from tests.gh_pr_payloads import (
    FORK_PRS,
    NO_OPEN_PRS,
    OPEN_PRS,
    SAME_REPOSITORY_PR,
    as_sweep_pr,
)
from tests.test_idempotence import violation, work_of
from tests.test_job_guard import october

BASE = "a" * 40
NEWER = "b" * 40

#: The recorded labels without the pause label: "curamoir:paused" back to
#: the "bug" it was recorded as (tests/gh_label_payloads.py).
UNPAUSED_LABELS = [{"name": "bug"} if label["name"] == "curamoir:paused" else label
                   for label in LABEL_LIST]

LABEL_ARGV = ["label", "list", "--limit", "1000", "--json", "name", "--jq", ".[].name"]
PR_ARGV = ["pr", "list", "--state", "open", "--limit", "1000",
           "--json", "headRefName,number,url,body,files,isCrossRepository"]


def sweep_prs(*branches, prefix=SWEEP_BRANCH_PREFIX):
    """Same-repository sweep PRs on `prefix` + each branch, from the
    recorded same-repository item."""
    return [as_sweep_pr(SAME_REPOSITORY_PR, prefix + branch, BASE, number=41 + i)
            for i, branch in enumerate(branches)]


@pytest.fixture
def gh(tmp_path, monkeypatch):
    """A fake gh with the recorded, unpaused labels and no open PRs, set as
    the adapter's executable."""
    fake = FakeGh(tmp_path / "fake-gh", labels=UNPAUSED_LABELS, prs=NO_OPEN_PRS)
    monkeypatch.setattr(github, "GH", str(fake.path))
    return fake


@pytest.fixture
def repo(harness):
    """The harness repo, initialised as `/curamoir:init` leaves it."""
    init_repo(harness.repo)
    return harness


def lines(harness, kind):
    return [entry for entry in harness.ledger().entries() if entry["kind"] == kind]


# --- the two commands, exactly ------------------------------------------------------

def ran_in(record):
    """The directory a fake gh call ran in, resolved (macOS's /var is a link)."""
    return Path(record["cwd"]).resolve()


def test_the_label_query_is_exactly_the_one_that_cannot_fail_open(gh, tmp_path):
    """A high --limit (gh's default is 30), and names through --json/--jq,
    never gh's tab-separated text; run in the repository, which is where
    real gh finds the repository it asks about."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()

    assert github.repo_labels(checkout) == [label["name"] for label in UNPAUSED_LABELS]

    assert gh.calls() == [LABEL_ARGV]
    assert ran_in(gh.call_records()[0]) == checkout.resolve()


def test_the_pr_query_is_exactly_the_listing_contracts(gh, tmp_path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    gh.feed(prs=OPEN_PRS)

    listed = github.open_sweep_prs(checkout)

    assert gh.calls() == [PR_ARGV]
    assert ran_in(gh.call_records()[0]) == checkout.resolve()
    assert [record["branch"] for record in listed] == [
        item["headRefName"] for item in OPEN_PRS]


def test_no_label_listing_is_cached_either(gh, tmp_path):
    """The kill switch reads the labels as they are now, not as some earlier
    call in this process saw them."""
    first = github.repo_labels(tmp_path)
    gh.feed(labels=LABEL_LIST)
    second = github.repo_labels(tmp_path)

    assert gh.calls() == [LABEL_ARGV, LABEL_ARGV]
    assert "curamoir:paused" not in first and "curamoir:paused" in second


@pytest.mark.parametrize("query", [github.repo_labels, github.open_sweep_prs])
def test_output_that_is_not_utf8_raises(gh, tmp_path, query):
    gh.feed(raw_stdout=b"[\xff\xfe]\n".hex())

    with pytest.raises(github.GhError, match="not UTF-8"):
        query(tmp_path)


def test_a_long_stderr_is_quoted_cut_to_the_clip(gh, tmp_path):
    gh.feed(fail="x" * 1000)

    with pytest.raises(github.GhError) as raised:
        github.repo_labels(tmp_path)

    assert str(raised.value).split("exited 1: ", 1)[1] == "x" * 300 + "...(clipped)"


def test_line_and_paragraph_separators_in_stderr_are_shown_escaped(gh, tmp_path):
    gh.feed(fail="one\u2028two\u2029three")

    with pytest.raises(github.GhError) as raised:
        github.repo_labels(tmp_path)

    message = str(raised.value)
    assert "one\\u2028two\\u2029three" in message
    assert "\u2028" not in message and "\u2029" not in message


def test_each_listed_item_is_mapped_through_sweep_pr_listing(gh, tmp_path):
    """gh's files ({path, additions, deletions}) become paths, the marker
    the base, isCrossRepository cross_repository."""
    gh.feed(prs=[as_sweep_pr(SAME_REPOSITORY_PR, "curamoir/sweep-x", BASE)])

    record, = github.open_sweep_prs(tmp_path)

    assert record == {
        "branch": "curamoir/sweep-x", "number": 103,
        "url": "https://github.com/example/repo/pull/103", "base": BASE,
        "paths": ["docs/development.md", "docs/flags.md"],
        "cross_repository": False}


def test_gh_runs_without_git_variables_or_gh_repo_and_without_prompting(
        gh, tmp_path, monkeypatch):
    """A GIT_DIR inherited from a hook, or a GH_REPO, would point gh at
    another repository."""
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("GH_REPO", "someone/else")

    github.repo_labels(tmp_path)
    github.open_sweep_prs(tmp_path)

    for record in gh.call_records():
        assert (record["GIT_DIR"], record["GH_REPO"]) == (None, None)
        assert record["GH_PROMPT_DISABLED"] == "1"


def test_forced_colour_in_the_environment_does_not_reach_gh(gh, repo, monkeypatch):
    """gh colours --json output into a pipe under CLICOLOR_FORCE=1 or
    GH_FORCE_TTY, and NO_COLOR does not stop it: unscrubbed, every listing
    would fail to parse and every sweep refuse."""
    monkeypatch.setenv("CLICOLOR_FORCE", "1")
    monkeypatch.setenv("GH_FORCE_TTY", "1")
    gh.feed(labels=LABEL_LIST, prs=sweep_prs("2026-10-05"))
    # The fake colours as gh does, so this test can fail:
    assert run_fake(gh, *PR_ARGV).stdout.startswith("\033[")

    assert "curamoir:paused" in github.repo_labels(repo.repo)
    assert [r["branch"] for r in github.open_sweep_prs(repo.repo)] == [
        "curamoir/sweep-2026-10-05"]
    for record in gh.call_records()[1:]:
        assert (record["GH_FORCE_TTY"], record["CLICOLOR_FORCE"], record["NO_COLOR"]) == (
            None, "0", "1")


def test_the_fake_colours_json_but_not_jq_output_as_gh_does(tmp_path, monkeypatch):
    fake = FakeGh(tmp_path / "gh", labels=[{"name": "bug"}])
    for variables in ({"CLICOLOR_FORCE": "1"}, {"GH_FORCE_TTY": "1", "CLICOLOR_FORCE": "0"}):
        for name, value in variables.items():
            monkeypatch.setenv(name, value)
        assert run_fake(fake, "label", "list", "--json", "name").stdout.startswith("\033[")
        assert run_fake(fake, *LABEL_ARGV).stdout == "bug\n"
        for name in variables:
            monkeypatch.delenv(name)
    monkeypatch.setenv("CLICOLOR_FORCE", "0")
    assert run_fake(fake, "label", "list", "--json", "name").stdout == '[{"name": "bug"}]\n'


def test_no_listing_is_cached_two_calls_run_gh_twice_and_see_the_change(gh, tmp_path):
    first = github.open_sweep_prs(tmp_path)
    gh.feed(prs=sweep_prs("2026-10-05"))
    second = github.open_sweep_prs(tmp_path)

    assert gh.calls() == [PR_ARGV, PR_ARGV]
    assert first == []
    assert [record["branch"] for record in second] == ["curamoir/sweep-2026-10-05"]


# --- the fake answers and fails as gh does --------------------------------------------

def run_fake(fake, *args):
    return subprocess.run([str(fake.path), *args], capture_output=True, text=True,
                          check=False)


def test_the_fakes_jq_output_is_gh_recorded_bytes(tmp_path):
    names = LABEL_NAMES_JQ.splitlines()
    fake = FakeGh(tmp_path / "gh", labels=[{"name": name} for name in names])

    done = run_fake(fake, *LABEL_ARGV)

    assert (done.returncode, done.stdout) == (0, LABEL_NAMES_JQ)


def test_the_fake_returns_only_the_json_fields_asked_for(tmp_path):
    """The projection the isCrossRepository test relies on: gh's --json
    prints only the fields named."""
    fake = FakeGh(tmp_path / "gh", prs=[SAME_REPOSITORY_PR])

    done = run_fake(fake, "pr", "list", "--state", "open", "--json", "number")

    assert json.loads(done.stdout) == [{"number": SAME_REPOSITORY_PR["number"]}]


@pytest.mark.parametrize("command", [["label", "list", "--json", "name"],
                                     ["pr", "list", "--state", "open", "--json", "number"]])
def test_without_a_limit_the_fake_lists_gh_default_thirty(tmp_path, command):
    fake = FakeGh(tmp_path / "gh", labels=[{"name": f"l{i}"} for i in range(40)],
                  prs=[{**SAME_REPOSITORY_PR, "number": i} for i in range(40)])

    done = run_fake(fake, *command)

    assert done.returncode == 0
    assert len(json.loads(done.stdout)) == GH_DEFAULT_LIMIT == 30


@pytest.mark.parametrize("command", [["label", "list", "--json", "name,nosuch"],
                                     ["pr", "list", "--state", "open", "--json",
                                      "headRefName,nosuch"]])
def test_an_unknown_json_field_fails_in_the_fake_as_in_gh(tmp_path, command):
    done = run_fake(FakeGh(tmp_path / "gh"), *command)

    assert done.returncode == 1
    assert done.stderr.startswith('Unknown JSON field: "nosuch"\nAvailable fields:\n')
    assert done.stdout == ""


def test_a_child_process_resolves_gh_to_the_shared_fake(no_real_gh, repo):
    """tests/test_lock_slot.py runs the runner in child interpreters, where
    `github.GH` is "gh" again: PATH must lead it to the fake, never the
    developer's gh."""
    child = subprocess.run(
        [sys.executable, "-c",
         "import sys\n"
         "from curamoir import github\n"
         "from curamoir.cli import main\n"
         "assert github.GH == 'gh'\n"
         "main(['run', 'brief', '--repo', sys.argv[1]])\n",
         str(repo.repo)],
        capture_output=True, text=True, check=False)

    assert child.returncode == 0, child.stderr
    assert no_real_gh.calls() == [LABEL_ARGV]
    assert lines(repo, "guard") == []


def test_the_shared_fake_starts_every_test_with_an_empty_log(no_real_gh):
    assert no_real_gh.calls() == []


# --- every failure raises ------------------------------------------------------------

@pytest.mark.parametrize("query", [github.repo_labels, github.open_sweep_prs])
def test_a_gh_that_hangs_is_killed_at_the_timeout_and_raises(gh, tmp_path, monkeypatch,
                                                              query):
    gh.feed(hang=True)
    monkeypatch.setattr(github, "GH_TIMEOUT", 1)

    started = time.monotonic()
    with pytest.raises(github.GhError, match="did not finish within 1 second"):
        query(tmp_path)

    assert time.monotonic() - started < 30


@pytest.mark.parametrize("query", [github.repo_labels, github.open_sweep_prs])
def test_a_gh_that_exits_non_zero_raises_with_its_stderr(gh, tmp_path, query):
    gh.feed(fail="HTTP 401: Bad credentials (https://api.github.com/graphql)")

    with pytest.raises(github.GhError, match=r"exited 1: HTTP 401: Bad credentials"):
        query(tmp_path)


@pytest.mark.parametrize("query", [github.repo_labels, github.open_sweep_prs])
def test_a_missing_gh_raises(tmp_path, monkeypatch, query):
    monkeypatch.setattr(github, "GH", str(tmp_path / "no-such-gh"))

    with pytest.raises(github.GhError, match="could not run"):
        query(tmp_path)


@pytest.mark.parametrize("printed", ['{"headRefName": "x"}', "not json", '["x"]', ""])
def test_a_pr_listing_that_is_not_a_list_of_objects_raises(tmp_path, monkeypatch, printed):
    monkeypatch.setattr(github, "run_gh", lambda *_args, **_kwargs: printed + "\n")

    with pytest.raises(github.GhError):
        github.open_sweep_prs(tmp_path)


def test_a_label_listing_at_the_limit_may_be_cut_short_and_raises(gh, tmp_path, monkeypatch):
    """At --limit, curamoir:paused may be the label left off: unknown, not
    unpaused."""
    monkeypatch.setattr(github, "LABEL_LIMIT", 3)

    with pytest.raises(github.GhError, match="may be cut short"):
        github.repo_labels(tmp_path)


def test_a_pr_listing_at_the_limit_may_be_cut_short_and_raises(gh, tmp_path, monkeypatch):
    gh.feed(prs=OPEN_PRS)
    monkeypatch.setattr(github, "PR_LIMIT", 3)

    with pytest.raises(github.GhError, match="may be cut short"):
        github.open_sweep_prs(tmp_path)


# --- what gh prints is read narrowly, and quoted safely ---------------------------------

def paused_by(repo):
    """KillSwitch's verdict on `repo` with the real adapter's labels."""
    return KillSwitch(lambda: github.repo_labels(repo.repo))(
        JobContext(job="sweep", repo=repo.repo))


@pytest.mark.parametrize("name", ["\ufeffcuramoir:paused", "curamoir:paused\r",
                                  "curamoir:paused "])
def test_a_bom_crlf_or_padded_pause_label_still_pauses(gh, repo, name):
    """A BOM before the first name, CRLF line ends and a trailing space all
    reach KillSwitch as the pause label."""
    gh.feed(labels=[{"name": name}, *UNPAUSED_LABELS])

    assert isinstance(paused_by(repo), Refusal)


def test_label_lines_are_split_on_newline_only(gh, repo):
    """splitlines() would also break at \\x0b, \\x0c or U+2028, turning one
    label into a pause label that nobody set."""
    gh.feed(labels=[{"name": "a\x0bcuramoir:paused"}, {"name": "b\u2028curamoir:paused"}])

    assert github.repo_labels(repo.repo) == ["a\x0bcuramoir:paused",
                                             "b\u2028curamoir:paused"]
    assert paused_by(repo) is None


@pytest.mark.parametrize("items", [[{}], [{**SAME_REPOSITORY_PR, "headRefName": None}]])
def test_a_pr_item_without_a_branch_degrades_the_cap_and_refuses_the_decision(
        gh, repo, items):
    gh.feed(prs=items)

    def listing():
        return github.open_sweep_prs(repo.repo)

    verdict = OpenPrCap(listing)(JobContext(job="sweep", repo=repo.repo))
    decision = sweep_decision(work_of(violation("f1")), BASE, [], listing,
                              branch_prefix=DEFAULT_PREFIX)

    assert isinstance(verdict, Degraded)
    assert decision.kind == UNKNOWN


TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"


def test_a_token_in_gh_stderr_is_redacted_in_the_error(gh, tmp_path):
    gh.feed(fail=f"\x1b[31mHTTP 401\x1b[0m: bad credentials {TOKEN} for this request")

    with pytest.raises(github.GhError) as raised:
        github.repo_labels(tmp_path)

    message = str(raised.value)
    assert TOKEN not in message and "[REDACTED]" in message
    assert "\x1b" not in message and "HTTP 401" in message


def test_a_token_in_unparseable_pr_output_is_redacted_in_the_error(tmp_path, monkeypatch):
    printed = f"\x1b[1;38m[{{\"body\": \"{TOKEN}\"}}]\x1b[m"
    monkeypatch.setattr(github, "run_gh", lambda *_args, **_kwargs: printed)

    with pytest.raises(github.GhError) as raised:
        github.open_sweep_prs(tmp_path)

    message = str(raised.value)
    assert TOKEN not in message and "[REDACTED]" in message
    assert "\x1b" not in message


# --- the guards, fed by the real adapter ----------------------------------------------

def test_the_kill_switch_refuses_on_the_recorded_pause_label_through_the_adapter(
        gh, repo):
    gh.feed(labels=LABEL_LIST)

    verdict = KillSwitch(lambda: github.repo_labels(repo.repo))(
        JobContext(job="sweep", repo=repo.repo))

    assert isinstance(verdict, Refusal)
    assert "curamoir:paused" in verdict.message


def test_the_cap_counts_the_records_of_the_real_listing(gh, repo):
    gh.feed(prs=[*sweep_prs("2026-09-14", "2026-09-21", "2026-09-28"), SAME_REPOSITORY_PR])

    verdict = OpenPrCap(lambda: github.open_sweep_prs(repo.repo))(
        JobContext(job="sweep", repo=repo.repo))

    assert isinstance(verdict, Refusal)
    assert verdict.message.startswith("3 open curamoir/* pull request(s)")


def test_a_cross_repository_pr_from_the_real_listing_is_dropped(gh, repo):
    """Two of ours and a fork's on a sweep-looking branch: under the cap of
    three, because the fork's is not counted. An adapter that did not ask for
    isCrossRepository would count it (unknown provenance) and refuse."""
    fork = as_sweep_pr(FORK_PRS[0], SWEEP_BRANCH_PREFIX + "2026-09-28", BASE)
    gh.feed(prs=[*sweep_prs("2026-09-14", "2026-09-21"), fork])

    listed = github.open_sweep_prs(repo.repo)
    verdict = OpenPrCap(lambda: listed)(JobContext(job="sweep", repo=repo.repo))

    assert [record["cross_repository"] for record in listed] == [False, False, True]
    assert verdict is None


# --- the runner's defaults -----------------------------------------------------------
#
# The runner names origin's repository with --repo (runner, "Which repository
# gh is asked about"), so these repos have a GitHub origin (`hub`). Nothing is
# fetched or pushed: the URL is only read. The sweep's body is `stub_body`,
# a failure line, as the placeholder was: what is tested here is the guards'
# gh calls, not the sweep (tests/test_sweep_publish.py runs the real body).

GH_REPO = "github.com/acme/widgets"
#: The runner's two gh calls for a repository whose origin is GH_REPO.
RUNNER_LABEL_ARGV = [*LABEL_ARGV[:2], "--repo", GH_REPO, *LABEL_ARGV[2:]]
RUNNER_PR_ARGV = [*PR_ARGV[:2], "--repo", GH_REPO, *PR_ARGV[2:]]
STUB = "the stub sweep body ran"


@pytest.fixture
def hub(repo):
    """The harness repo with origin naming GH_REPO."""
    subprocess.run(["git", "remote", "add", "origin", f"https://{GH_REPO}.git"],
                   cwd=repo.repo, check=True, capture_output=True)
    return repo


@pytest.fixture
def stub_body(monkeypatch):
    monkeypatch.setitem(JOBS, "sweep", runner.Job(
        body=lambda _root, run: run.failed(STUB), opens_prs=True,
        open_pr_listing_for=JOBS["sweep"].open_pr_listing_for))


def test_run_sweep_with_no_arguments_uses_both_gh_defaults(gh, hub, stub_body, capsys):
    code = cli.main(["run", "sweep", "--repo", str(hub.repo)])

    assert gh.calls() == [RUNNER_LABEL_ARGV, RUNNER_PR_ARGV]
    # Both checks ran and passed: no guard line, and the body ran.
    assert lines(hub, "guard") == []
    assert code == EXIT_FAILURE
    assert STUB in capsys.readouterr().out


def test_the_registered_sweep_runs_the_real_body_after_both_defaults(gh, hub):
    """No stub: run_sweep resolves the base (`gh repo view` on origin's
    repository) and, with no refs/remotes/origin/main fetched, refuses
    before any sandbox."""
    ran = run_job(hub.repo, "sweep", now=october)

    assert gh.calls() == [RUNNER_LABEL_ARGV, RUNNER_PR_ARGV,
                          ["repo", "view", GH_REPO, "--json", "defaultBranchRef"]]
    assert ran.exit_code == EXIT_REFUSED
    assert lines(hub, "job")[-1]["reason"] == "sweep_base"


def test_gh_runs_in_the_repo_root_not_the_process_cwd(gh, hub, stub_body, tmp_path,
                                                       monkeypatch):
    """`--repo` names the repository; a scheduler's cwd may be anywhere, and
    gh run there would read another repository's labels and PRs."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    cli.main(["run", "sweep", "--repo", str(hub.repo)])

    assert gh.calls() == [RUNNER_LABEL_ARGV, RUNNER_PR_ARGV]
    assert [ran_in(record) for record in gh.call_records()] == [hub.repo.resolve()] * 2


def test_run_sweep_with_no_arguments_is_paused_by_the_label(gh, hub):
    gh.feed(labels=LABEL_LIST)

    code = cli.main(["run", "sweep", "--repo", str(hub.repo)])

    assert code == EXIT_REFUSED
    assert lines(hub, "job")[-1]["reason"] == "paused"
    # Refused before the cap: the open PRs were never listed.
    assert gh.calls() == [RUNNER_LABEL_ARGV]


def test_run_sweep_with_no_arguments_is_capped_by_the_listing(gh, hub):
    gh.feed(prs=sweep_prs("2026-09-14", "2026-09-21", "2026-09-28"))

    code = cli.main(["run", "sweep", "--repo", str(hub.repo)])

    assert code == EXIT_REFUSED
    assert lines(hub, "job")[-1]["reason"] == "pr_cap"


def test_every_job_gets_the_label_check_not_only_the_sweep(gh, hub):
    """The kill switch is per run: the brief, which opens no PRs, is paused
    by the label too (origin's), and never lists PRs."""
    gh.feed(labels=LABEL_LIST)

    ran = run_job(hub.repo, "brief", now=october)

    assert ran.exit_code == EXIT_REFUSED
    assert lines(hub, "job")[-1]["reason"] == "paused"
    assert gh.calls() == [RUNNER_LABEL_ARGV]


def test_with_no_github_origin_the_labels_are_gh_resolved_and_the_cap_degrades(gh, repo):
    """Origin names no GitHub repository (here there is no origin): the
    label check falls back to gh's own resolution, as before cm-fkd.14, so
    the brief is checked as it was; the sweep's cap cannot name the
    repository its PR would open on, so it degrades without asking gh, and
    the sweep refuses for want of a base."""
    brief = run_job(repo.repo, "brief", now=october)
    assert brief.exit_code == EXIT_SUCCESS
    assert gh.calls() == [LABEL_ARGV]

    ran = run_job(repo.repo, "sweep", now=october)

    assert gh.calls() == [LABEL_ARGV, LABEL_ARGV]
    [cap] = [line for line in lines(repo, "guard") if line["reason"] == "pr_cap"]
    assert cap["severity"] == "degraded" and "no origin remote" in cap["message"]
    assert ran.exit_code == EXIT_REFUSED
    assert lines(repo, "job")[-1]["reason"] == "sweep_base"


def test_a_gh_outage_degrades_both_checks_and_the_job_still_runs(gh, hub, stub_body):
    gh.feed(fail="error connecting to api.github.com")

    ran = run_job(hub.repo, "sweep", now=october)

    guards = lines(hub, "guard")
    assert [(line["reason"], line["severity"]) for line in guards] == [
        ("paused", "degraded"), ("pr_cap", "degraded")]
    assert all("error connecting to api.github.com" in line["message"] for line in guards)
    # The body ran: degraded checks allow.
    assert ran.exit_code == EXIT_FAILURE
    assert lines(hub, "job")[-1]["message"] == STUB


def test_the_overrides_still_replace_the_defaults(gh, hub, stub_body):
    ran = run_job(hub.repo, "sweep", now=october, repo_labels=lambda: [],
                  open_pr_listing=lambda: [])

    assert gh.calls() == []
    assert ran.exit_code == EXIT_FAILURE


def test_two_listings_in_one_sweep_run_both_run_gh(gh, hub, monkeypatch):
    """The cap's listing and the body's (step 2b's decision) in ONE run:
    two gh calls, and the second sees a PR opened in between."""
    seen = []

    def body(root, run):
        gh.feed(prs=sweep_prs("2026-10-05"))
        seen.extend(record["branch"] for record in github.open_sweep_prs(root))
        run.succeeded("listed")

    monkeypatch.setitem(JOBS, "sweep", runner.Job(
        body=body, opens_prs=True, open_pr_listing_for=JOBS["sweep"].open_pr_listing_for))

    ran = run_job(hub.repo, "sweep", now=october)

    assert ran.exit_code == EXIT_SUCCESS
    assert gh.calls() == [RUNNER_LABEL_ARGV, RUNNER_PR_ARGV, PR_ARGV]
    assert seen == ["curamoir/sweep-2026-10-05"]


# --- a configured guardrails.branch_prefix ---------------------------------------------

BOT_YAML = "version: 1\nguardrails:\n  branch_prefix: bot/\n"


def test_the_sweep_prefix_follows_the_configured_branch_prefix():
    assert sweep_branch_prefix("bot/") == "bot/sweep-"
    assert sweep_branch_prefix(DEFAULT_PREFIX) == SWEEP_BRANCH_PREFIX == "curamoir/sweep-"
    for bad in ("", "  ", None, 3, " bot/", "bot/ ", " bot/ "):
        with pytest.raises(ValueError):
            sweep_branch_prefix(bad)


def test_sweep_prs_under_a_configured_prefix_are_counted_by_the_cap(gh, hub):
    hub.write(".curamoir/curamoir.yaml", BOT_YAML)
    gh.feed(prs=sweep_prs("1", "2", "3", prefix="bot/sweep-"))

    ran = run_job(hub.repo, "sweep", now=october)

    assert ran.exit_code == EXIT_REFUSED
    assert lines(hub, "job")[-1]["reason"] == "pr_cap"


def test_sweep_prs_under_the_default_prefix_are_not_counted_once_another_is_configured(
        gh, hub, stub_body):
    hub.write(".curamoir/curamoir.yaml", BOT_YAML)
    gh.feed(prs=sweep_prs("1", "2", "3"))

    run_job(hub.repo, "sweep", now=october)

    assert lines(hub, "job")[-1]["reason"] is None
    assert lines(hub, "job")[-1]["message"] == STUB


def test_sweep_decision_sees_sweep_prs_under_the_configured_prefix(gh, tmp_path):
    """A bot/sweep- PR from this base is the duplicate under branch_prefix
    bot/, and is not a sweep PR at all under the default."""
    gh.feed(prs=sweep_prs("2026-10-05", prefix="bot/sweep-"))
    work = work_of(violation("f1", path="docs/flags.md"))

    def listing():
        return github.open_sweep_prs(tmp_path)

    under_bot = sweep_decision(work, BASE, [], listing, branch_prefix="bot/")
    under_default = sweep_decision(work, BASE, [], listing, branch_prefix=DEFAULT_PREFIX)

    assert under_bot.kind == DUPLICATE_OPEN_PR
    assert [pr["branch"] for pr in under_bot.open_prs] == ["bot/sweep-2026-10-05"]
    assert under_default.kind == WORK and under_default.open_prs == ()


@pytest.mark.parametrize("prefix", ["", "  ", None, " curamoir/ "])
def test_sweep_decision_refuses_an_empty_prefix_before_listing(prefix):
    def listing():
        raise AssertionError("listed with no prefix to judge the PRs by")

    with pytest.raises(ValueError, match=r"guardrails\.branch_prefix"):
        sweep_decision(work_of(violation("f1")), NEWER, [], listing, branch_prefix=prefix)


def test_sweep_decision_cannot_be_called_without_the_configured_prefix():
    """No default to fall back to: a caller that forgot the config would
    otherwise judge sweep PRs by a prefix nobody configured."""
    with pytest.raises(TypeError):
        sweep_decision(work_of(violation("f1")), NEWER, [], lambda: [])


# --- which repository: --repo (cm-fkd.12) ------------------------------------------
#
# gh resolves its repository by remote priority (upstream > github > origin)
# while the sweep pushes to origin. publish.origin_gh_repo names origin's
# repository once; both listings take it as `gh_repo` and pass it as
# `--repo`, after the subcommand (gh accepts it there), so the cap, the
# decision and the PR all read the repository the branch is pushed to.


def test_the_label_query_names_the_repository_when_given_one(gh, tmp_path):
    assert github.repo_labels(tmp_path, gh_repo=GH_REPO) == [
        label["name"] for label in UNPAUSED_LABELS]

    assert gh.calls() == [[*LABEL_ARGV[:2], "--repo", GH_REPO, *LABEL_ARGV[2:]]]


def test_the_pr_query_names_the_repository_when_given_one(gh, tmp_path):
    gh.feed(prs=OPEN_PRS)

    listed = github.open_sweep_prs(tmp_path, gh_repo=GH_REPO)

    assert gh.calls() == [[*PR_ARGV[:2], "--repo", GH_REPO, *PR_ARGV[2:]]]
    assert len(listed) == len(OPEN_PRS)


def test_a_timeout_is_its_own_gh_error(gh, tmp_path, monkeypatch):
    """A write that timed out has an UNKNOWN outcome (the PR may exist), so
    the caller must tell a timeout from a refusal: GhTimeout, a GhError."""
    gh.feed(hang=True)
    monkeypatch.setattr(github, "GH_TIMEOUT", 1)

    with pytest.raises(github.GhTimeout):
        github.repo_labels(tmp_path)

    gh.feed(hang=False, fail="HTTP 401")
    with pytest.raises(github.GhError) as refused:
        github.repo_labels(tmp_path)
    assert not isinstance(refused.value, github.GhTimeout)
