"""cm-fkd.12: publishing the sweep's accepted change (curamoir.publish).

Every test runs real git: a user checkout whose `origin` names a GitHub
repository (`https://github.com/acme/widgets.git`) and is redirected, by
the checkout's own `url.<path>.insteadOf` and `url.<path>.pushInsteadOf`,
to a local bare repository, so `git push origin` really pushes and the
bare repository shows what landed.

**Nothing may reach GitHub.** publish pushes with the user's git and
config on purpose, so in a test the developer's own global config would
apply: a global `url.git@github.com:.pushInsteadOf https://github.com/`
beats a local fetch `insteadOf` for pushes and would send the push to the
real github.com, and a global `core.hooksPath` would replace the tests'
hooks. GIT_CONFIG_GLOBAL cannot stop it (publish drops every GIT_*), so
`isolated_home` (autouse) points HOME and XDG_CONFIG_HOME at an empty
directory for every test here, holding only a marker the isolation test
reads back; and World's local `pushInsteadOf` is the longest match for
origin's URL, so even a global rewrite of `https://github.com/` loses to
it (tested with one planted). Do not remove isolated_home or World's
pushInsteadOf: without them a developer's global config can send a test
push to the real github.com; the two tests under "nothing reaches GitHub"
fail if either goes.
gh is tests/fake_gh.py (the autouse `no_real_gh` of tests/conftest.py),
which records each call's argv and the text of its `--body-file`. Nothing
here reaches GitHub or the real gh.

The patch is made the way the runner makes it: in a sandbox
(git_guard.prepare_sandbox) of the checkout's base commit, by
Sandbox.patch of the accepted paths.
"""

import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import types
import unicodedata
from datetime import date
from pathlib import Path

import pytest

from curamoir import publish
from curamoir.git_guard import prepare_sandbox
from curamoir.idempotence import parse_sweep_marker, sweep_marker
from curamoir.sweep_pr import sweep_branch
from curamoir.termination import Terminated
from tests.gh_pr_payloads import FORK_PRS, as_sweep_pr

GH_REPO = "github.com/acme/widgets"
GITHUB_URL = "https://github.com/acme/widgets.git"
PREFIX = "curamoir/"
BLOB = bytes(range(256)) * 4
ACCEPTED = ("a.py", "b.py", "c.bin")
TITLE = "curamoir sweep 2026-10-24: 3 fixes of 1 principle"


def git(cwd, *args, env=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          env=env).stdout.decode("utf-8", "surrogateescape")


#: A key only the isolated global config holds.
ISOLATION_MARKER = "curamoir-test.isolated-home"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """HOME and XDG_CONFIG_HOME for this test alone (module docstring,
    "Nothing may reach GitHub"): a global git config holding only
    ISOLATION_MARKER. Returns the directory."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text(
        "[curamoir-test]\n\tisolated-home = yes\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    return home


class World:
    """The checkout, its bare origin, the base, and a patch of ACCEPTED."""

    def __init__(self, tmp_path):
        self.origin = tmp_path / "origin.git"
        subprocess.run(["git", "init", "--quiet", "--bare", "--initial-branch=main",
                        str(self.origin)], check=True)
        self.repo = tmp_path / "checkout"
        files = {"a.py": b"a = 1\n", "b.py": b"b = 1\n", "c.bin": BLOB,
                 "d.py": b"d = 1\n", ".gitignore": b".curamoir/state/\n"}
        for path, content in files.items():
            (self.repo / path).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / path).write_bytes(content)
        for command in (["init", "--quiet", "--initial-branch=main"],
                        ["config", "user.name", "Ada Lovelace"],
                        ["config", "user.email", "ada@example.invalid"],
                        ["add", "-A"], ["commit", "--quiet", "-m", "base"],
                        ["remote", "add", "origin", GITHUB_URL],
                        ["config", f"url.{self.origin}.insteadOf", GITHUB_URL],
                        ["config", f"url.{self.origin}.pushInsteadOf", GITHUB_URL],
                        ["push", "--quiet", "origin", "main"],
                        ["remote", "set-head", "origin", "main"]):
            git(self.repo, *command)
        self.base = git(self.repo, "rev-parse", "HEAD").strip()
        self.branch = sweep_branch(PREFIX, date(2026, 10, 24), self.base)
        self.body = (f"{sweep_marker(self.base)}\n## curamoir sweep\n\nThree fixes, "
                     "with a `code span` and a line that says token=[REDACTED].\n")
        self.message = f"{TITLE}\n\nBase: {self.base}\n\nclarity: 3\n"
        sandboxes = tmp_path / "sandboxes"
        sandboxes.mkdir()
        with prepare_sandbox(self.repo, self.base, parent=sandboxes) as sandbox:
            (sandbox.path / "a.py").write_bytes(b"a = 2\n")
            (sandbox.path / "b.py").unlink()
            (sandbox.path / "c.bin").write_bytes(BLOB[::-1])
            (sandbox.path / "d.py").write_bytes(b"d = 'not accepted'\n")
            self.patch = sandbox.patch(ACCEPTED)

    def publish(self, **changes):
        arguments = {"base": self.base, "patch": self.patch, "paths": ACCEPTED,
                     "branch": self.branch, "branch_prefix": PREFIX, "gh_repo": GH_REPO,
                     "message": self.message, "title": TITLE, "body": self.body}
        arguments.update(changes)
        return publish.publish(self.repo, **arguments)

    def origin_branches(self):
        """origin's branches, the default branch first."""
        found = git(self.origin, "for-each-ref", "--format=%(refname)", "refs/heads").split()
        return sorted(found, key=lambda ref: (ref != "refs/heads/main", ref))

    def hook(self, script):
        hook = self.repo / ".git" / "hooks" / "pre-push"
        hook.parent.mkdir(exist_ok=True)
        hook.write_text("#!/bin/sh\n" + script, encoding="utf-8")
        hook.chmod(0o755)


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def creates(gh):
    return [record for record in gh.call_records() if record["argv"][:2] == ["pr", "create"]]


def no_sweep_branch_and_no_pr(world, gh):
    """For tests that add branches of their own to origin."""
    assert f"refs/heads/{world.branch}" not in world.origin_branches()
    assert creates(gh) == []


def nothing_left_the_machine(world, gh):
    assert world.origin_branches() == ["refs/heads/main"]
    assert creates(gh) == []


# --- what lands ------------------------------------------------------------------------

def test_the_pushed_branch_differs_from_the_base_in_exactly_the_patchs_paths(world,
                                                                           no_real_gh):
    published = world.publish()

    head = f"refs/heads/{world.branch}"
    assert world.origin_branches() == ["refs/heads/main", head]
    changed = git(world.origin, "diff-tree", "-r", "--name-only", "--no-renames",
                  world.base, head).split()
    assert sorted(changed) == sorted(ACCEPTED)
    assert git(world.origin, "rev-parse", f"{head}^").strip() == world.base
    assert git(world.origin, "show", f"{head}:a.py") == "a = 2\n"
    shown = subprocess.run(["git", "show", f"{head}:c.bin"], cwd=world.origin,
                           capture_output=True, check=True).stdout
    assert shown == BLOB[::-1]
    assert git(world.origin, "show", f"{head}:d.py") == "d = 1\n"
    assert published.sha == git(world.origin, "rev-parse", head).strip()
    assert (published.branch, published.base_branch, published.gh_repo) == (
        world.branch, "main", GH_REPO)
    assert (published.number, published.url) == (
        1, "https://github.com/acme/widgets/pull/1")


def test_the_pr_is_opened_on_origins_repository_from_the_branch_into_the_default(
        world, no_real_gh):
    world.publish()

    [create] = creates(no_real_gh)
    argv = create["argv"]
    assert argv[:2] == ["pr", "create"]
    assert dict(zip(argv[2::2], argv[3::2], strict=True)) == {
        "--repo": GH_REPO, "--head": world.branch, "--base": "main", "--title": TITLE,
        "--body-file": argv[-1]}
    assert Path(create["cwd"]).resolve() == world.repo.resolve()


def test_the_body_gh_received_carries_the_marker_and_never_rode_in_argv(world,
                                                                        no_real_gh):
    """gh read the body from a file (run_gh feeds no stdin, so `--body-file
    -` would have published an empty body without the marker), the file is
    gone afterwards, and no argument carried the body's text."""
    world.publish()

    [create] = creates(no_real_gh)
    assert create["body_file_content"] == world.body
    assert parse_sweep_marker(create["body_file_content"]) == world.base
    body_file = create["argv"][create["argv"].index("--body-file") + 1]
    assert body_file != "-"
    assert not Path(body_file).exists()
    assert not any("Three fixes" in argument for argument in create["argv"])
    [held] = no_real_gh.prs()
    assert parse_sweep_marker(held["body"]) == world.base


def test_the_commit_is_the_users_with_the_sweeps_message(world, no_real_gh):
    published = world.publish()

    shown = git(world.origin, "log", "-1", "--format=%an <%ae>|%cn <%ce>", published.sha)
    assert shown.strip() == ("Ada Lovelace <ada@example.invalid>|"
                             "Ada Lovelace <ada@example.invalid>")
    assert git(world.origin, "log", "-1", "--format=%B", published.sha).rstrip("\n") == (
        world.message.rstrip("\n"))


# --- the user's checkout ---------------------------------------------------------------

def checkout_state(repo):
    """Everything of the checkout publish must not touch: the index's
    bytes (read first: `git status` may refresh it, so it runs with
    optional locks off), HEAD, the local branches, and the status of the
    work tree, untracked and ignored files included."""
    index = hashlib.sha256((repo / ".git" / "index").read_bytes()).hexdigest()
    quiet = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    return {
        "index": index,
        "HEAD": (repo / ".git" / "HEAD").read_text(encoding="utf-8"),
        "head": git(repo, "rev-parse", "HEAD", env=quiet),
        "branches": git(repo, "for-each-ref", "refs/heads", env=quiet),
        "status": git(repo, "status", "--porcelain=v2", "--untracked-files=all",
                      "--ignored", env=quiet),
    }


def dirty(world):
    """A checkout mid-work: an edit, a staged file, cúramóir's own state
    (ignored), and one state file forced into the user's index."""
    (world.repo / "a.py").write_text("a = 'mine, uncommitted'\n", encoding="utf-8")
    (world.repo / "staged.py").write_text("staged = 1\n", encoding="utf-8")
    git(world.repo, "add", "staged.py")
    state = world.repo / ".curamoir" / "state"
    state.mkdir(parents=True)
    (state / "ledger.jsonl").write_text('{"kind": "job"}\n', encoding="utf-8")
    (state / "forced.json").write_text("{}\n", encoding="utf-8")
    git(world.repo, "add", "--force", ".curamoir/state/forced.json")


def test_the_users_status_index_and_head_are_unchanged(world, no_real_gh):
    dirty(world)
    before = checkout_state(world.repo)

    world.publish()

    assert checkout_state(world.repo) == before
    assert (world.repo / "a.py").read_text(encoding="utf-8") == "a = 'mine, uncommitted'\n"


def test_curamoirs_state_in_the_checkout_is_not_in_the_commit(world, no_real_gh):
    dirty(world)

    published = world.publish()

    tree = git(world.origin, "ls-tree", "-r", "--name-only", published.sha).split()
    assert sorted(tree) == [".gitignore", "a.py", "c.bin", "d.py"]
    assert "staged.py" not in tree


# --- refused before any push -----------------------------------------------------------

@pytest.mark.parametrize("branch, why", [
    ("main", "default branch 'main'"),
    ("feature/sweep-2026-10-24-x", "outside the 'curamoir/' prefix"),
    ("", "names no branch"),
])
def test_a_target_of_the_default_branch_or_outside_the_prefix_is_refused(
        world, no_real_gh, branch, why):
    with pytest.raises(publish.PublishRefused, match=why):
        world.publish(branch=branch)

    nothing_left_the_machine(world, no_real_gh)


def test_a_default_branch_inside_the_prefix_is_still_refused(world, no_real_gh):
    """An empty or permissive prefix must not open the default branch: a
    repository whose default is itself `curamoir/main`."""
    git(world.origin, "branch", "curamoir/main", "main")
    git(world.origin, "symbolic-ref", "HEAD", "refs/heads/curamoir/main")
    git(world.repo, "fetch", "--quiet", "origin")
    git(world.repo, "remote", "set-head", "origin", "curamoir/main")

    with pytest.raises(publish.PublishRefused, match="default branch 'curamoir/main'"):
        world.publish(branch="curamoir/main")

    assert creates(no_real_gh) == []
    assert git(world.origin, "rev-parse", "curamoir/main").strip() == world.base


def test_a_repository_gh_would_name_other_than_origins_is_refused(world, no_real_gh):
    with pytest.raises(publish.PublishRefused, match=re.escape(GH_REPO)):
        world.publish(gh_repo="github.com/upstream/widgets")

    nothing_left_the_machine(world, no_real_gh)


def test_a_branch_already_on_origin_is_refused_and_left_alone(world, no_real_gh):
    git(world.origin, "branch", world.branch, "main")

    with pytest.raises(publish.PublishRefused, match="already exists on origin"):
        world.publish()

    assert git(world.origin, "rev-parse", world.branch).strip() == world.base
    assert creates(no_real_gh) == []


def test_a_checkout_with_no_identity_is_refused(world, no_real_gh):
    git(world.repo, "config", "--unset", "user.email")

    with pytest.raises(publish.PublishRefused, match=r"user\.email"):
        world.publish()

    nothing_left_the_machine(world, no_real_gh)


@pytest.mark.parametrize("paths", [("a.py",), (*ACCEPTED, "d.py")])
def test_a_commit_that_is_not_exactly_the_prs_paths_is_refused(world, no_real_gh, paths):
    with pytest.raises(publish.PublishRefused, match="not exactly"):
        world.publish(paths=paths)

    nothing_left_the_machine(world, no_real_gh)


# --- the push --------------------------------------------------------------------------

def test_a_failing_pre_push_hook_is_a_typed_failure_with_no_pr(world, no_real_gh):
    world.hook("echo 'hook says no' >&2\nexit 1\n")

    with pytest.raises(publish.PushFailed, match="hook says no") as failed:
        world.publish()

    assert failed.value.timed_out is False
    nothing_left_the_machine(world, no_real_gh)


def test_the_pre_push_hook_runs_and_sees_the_sweep_branch(world, no_real_gh, tmp_path):
    seen = tmp_path / "pushed"
    world.hook(f"cat > {seen}\nexit 0\n")

    published = world.publish()

    assert seen.read_text(encoding="utf-8").split() == [
        published.sha, published.sha, f"refs/heads/{world.branch}", "0" * 40]


def test_a_push_that_hangs_is_stopped_and_raises(world, no_real_gh, monkeypatch):
    world.hook("sleep 60\n")
    monkeypatch.setattr(publish, "PUSH_TIMEOUT", 2)

    started = time.monotonic()
    with pytest.raises(publish.PushFailed, match="did not finish within 2") as failed:
        world.publish()

    assert time.monotonic() - started < 30
    assert failed.value.timed_out is True
    assert creates(no_real_gh) == []


# --- the PR ----------------------------------------------------------------------------

def test_push_ok_and_create_failed_deletes_the_remote_branch(world, no_real_gh):
    no_real_gh.feed(fail_on={"pr create": "GraphQL: Head sha can't be blank"})

    with pytest.raises(publish.PrCreateFailed) as failed:
        world.publish()

    assert world.origin_branches() == ["refs/heads/main"]
    error = failed.value
    assert (error.branch, error.branch_deleted, error.delete_error) == (
        world.branch, True, None)
    assert error.timed_out is False
    assert "Head sha can't be blank" in str(error)
    assert "deleted" in str(error)


def test_a_deletion_that_fails_is_reported_not_masked(world, no_real_gh):
    """The hook lets the branch through and refuses its deletion (a
    deletion's local sha is all zeros): both outcomes are on the error."""
    world.hook("while read local_ref local_sha remote_ref remote_sha; do\n"
               f"  [ \"$local_sha\" = {'0' * 40} ] && echo 'no deleting' >&2 && exit 1\n"
               "done\nexit 0\n")
    no_real_gh.feed(fail_on={"pr create": "GraphQL: Head sha can't be blank"})

    with pytest.raises(publish.PrCreateFailed) as failed:
        world.publish()

    error = failed.value
    assert error.branch_deleted is False
    assert "no deleting" in error.delete_error
    assert "Head sha can't be blank" in str(error)
    assert "no deleting" in str(error)
    assert world.origin_branches() == ["refs/heads/main", f"refs/heads/{world.branch}"]


def test_a_create_that_timed_out_without_a_pr_deletes_the_branch(world, no_real_gh,
                                                                  monkeypatch):
    no_real_gh.feed(create_hang="before")
    monkeypatch.setattr(publish, "CREATE_TIMEOUT", 2)
    monkeypatch.setattr(publish, "LOOKUP_DELAY", 0)

    with pytest.raises(publish.PrCreateFailed, match="did not finish within 2") as failed:
        world.publish()

    assert failed.value.branch_deleted is True
    assert failed.value.timed_out is True
    assert world.origin_branches() == ["refs/heads/main"]
    lookups = [r["argv"] for r in no_real_gh.call_records() if r["argv"][:2] == ["pr", "list"]]
    assert lookups and all(argv[argv.index("--head") + 1] == world.branch
                           and argv[argv.index("--repo") + 1] == GH_REPO
                           for argv in lookups)


def test_a_create_that_timed_out_after_opening_the_pr_finds_it(world, no_real_gh,
                                                               monkeypatch):
    no_real_gh.feed(create_hang="after")
    monkeypatch.setattr(publish, "CREATE_TIMEOUT", 2)
    monkeypatch.setattr(publish, "LOOKUP_DELAY", 0)

    published = world.publish()

    assert (published.number, published.url) == (
        1, "https://github.com/acme/widgets/pull/1")
    assert world.origin_branches() == ["refs/heads/main", f"refs/heads/{world.branch}"]


def test_a_create_whose_outcome_cannot_be_learned_leaves_the_branch(world, no_real_gh,
                                                                    monkeypatch):
    """Timed out, and the lookup failed too: the PR may exist, and deleting
    its branch would close it, so the branch stays and the error says the
    outcome is unknown."""
    no_real_gh.feed(create_hang="before", fail_on={"pr list": "HTTP 502"})
    monkeypatch.setattr(publish, "CREATE_TIMEOUT", 2)
    monkeypatch.setattr(publish, "LOOKUP_DELAY", 0)

    with pytest.raises(publish.PrOutcomeUnknown, match="HTTP 502") as unknown:
        world.publish()

    assert unknown.value.branch == world.branch
    assert world.origin_branches() == ["refs/heads/main", f"refs/heads/{world.branch}"]


def test_a_create_that_printed_no_pr_url_but_opened_it_finds_it(world, no_real_gh):
    """gh exited 0 but its last line is not a PR URL on origin's repository:
    the outcome is unknown, not a failure (deleting the branch would close
    a PR that does exist), so the lookup decides, and finds it."""
    no_real_gh.feed(create_stdout="https://github.com/someone/else/pull/9\n")

    published = world.publish()

    assert (published.number, published.url) == (
        1, "https://github.com/acme/widgets/pull/1")
    assert world.origin_branches() == ["refs/heads/main", f"refs/heads/{world.branch}"]


def test_a_create_that_printed_no_pr_url_and_cannot_be_looked_up_is_unknown(world,
                                                                         no_real_gh):
    no_real_gh.feed(create_stdout="https://github.com/someone/else/pull/9\n",
                    fail_on={"pr list": "HTTP 503"})

    with pytest.raises(publish.PrOutcomeUnknown, match="someone/else") as unknown:
        world.publish()

    assert "HTTP 503" in str(unknown.value)

    assert world.origin_branches() == ["refs/heads/main", f"refs/heads/{world.branch}"]


def test_a_pr_github_is_still_finishing_is_found_by_a_later_lookup(world, no_real_gh,
                                                                    monkeypatch):
    """After a timeout GitHub may not list the PR yet: the lookup is retried
    (LOOKUP_ATTEMPTS, LOOKUP_DELAY apart) before "none" deletes the branch."""
    no_real_gh.feed(create_hang="late")
    monkeypatch.setattr(publish, "CREATE_TIMEOUT", 2)
    monkeypatch.setattr(publish, "LOOKUP_DELAY", 0)

    published = world.publish()

    assert (published.number, published.url) == (
        1, "https://github.com/acme/widgets/pull/1")
    lookups = [r for r in no_real_gh.call_records() if r["argv"][:2] == ["pr", "list"]]
    assert len(lookups) == 2
    assert world.origin_branches() == ["refs/heads/main", f"refs/heads/{world.branch}"]


def test_the_lookup_is_bounded(world, no_real_gh, monkeypatch):
    no_real_gh.feed(create_hang="before")
    monkeypatch.setattr(publish, "CREATE_TIMEOUT", 2)
    monkeypatch.setattr(publish, "LOOKUP_DELAY", 0)

    with pytest.raises(publish.PrCreateFailed):
        world.publish()

    lookups = [r for r in no_real_gh.call_records() if r["argv"][:2] == ["pr", "list"]]
    assert len(lookups) == publish.LOOKUP_ATTEMPTS


def test_every_publish_error_is_one_family():
    for error in (publish.PublishRefused, publish.PushFailed, publish.PrCreateFailed,
                  publish.PrOutcomeUnknown):
        assert issubclass(error, publish.PublishError)


# --- which repository ------------------------------------------------------------------

@pytest.mark.parametrize("url, named", [
    ("https://github.com/acme/widgets.git", "github.com/acme/widgets"),
    ("https://github.com/acme/widgets", "github.com/acme/widgets"),
    ("https://github.com/acme/widgets/", "github.com/acme/widgets"),
    ("https://someone@github.com/acme/widgets.git", "github.com/acme/widgets"),
    ("git@github.com:acme/widgets.git", "github.com/acme/widgets"),
    ("ssh://git@github.com/acme/widgets.git", "github.com/acme/widgets"),
    ("ssh://git@github.com:22/acme/widgets", "github.com/acme/widgets"),
    ("https://GitHub.com/Acme/Widgets.git", "github.com/Acme/Widgets"),
    ("https://ghe.example.com/team/tool.git", "ghe.example.com/team/tool"),
    ("https://ghe.example.com:8443/team/tool.git", "ghe.example.com:8443/team/tool"),
    ("https://github.com:443/acme/widgets.git", "github.com/acme/widgets"),
    ("http://ghe.example.com:80/team/tool", "ghe.example.com/team/tool"),
    ("ssh://git@ghe.example.com:2222/team/tool.git", "ghe.example.com/team/tool"),
])
def test_origins_github_repository_is_read_from_its_url(tmp_path, url, named):
    repo = tmp_path / "r"
    git(tmp_path, "init", "--quiet", str(repo))
    git(repo, "remote", "add", "origin", url)

    assert publish.origin_gh_repo(repo) == named


def test_origins_push_url_is_the_one_read(tmp_path):
    repo = tmp_path / "r"
    git(tmp_path, "init", "--quiet", str(repo))
    git(repo, "remote", "add", "origin", "https://github.com/upstream/widgets.git")
    git(repo, "config", "remote.origin.pushurl", "git@github.com:acme/widgets.git")

    assert publish.origin_gh_repo(repo) == "github.com/acme/widgets"


@pytest.mark.parametrize("setup, why", [
    ([], "no origin"),
    ([["remote", "add", "origin", "/srv/git/widgets.git"]], "not a GitHub"),
    ([["remote", "add", "origin", "file:///srv/git/widgets.git"]], "not a GitHub"),
    ([["remote", "add", "origin", "https://github.com/acme"]], "not a GitHub"),
    ([["remote", "add", "origin", "https://github.com/a/b/c"]], "not a GitHub"),
    ([["remote", "add", "origin", "https://github.com/acme/widgets.git"],
      ["config", "--add", "remote.origin.pushurl", "git@github.com:acme/widgets.git"],
      ["config", "--add", "remote.origin.pushurl", "git@github.com:acme/other.git"]],
     "more than one"),
])
def test_an_origin_that_names_no_one_github_repository_is_refused(tmp_path, setup, why):
    repo = tmp_path / "r"
    git(tmp_path, "init", "--quiet", str(repo))
    for command in setup:
        git(repo, *command)

    with pytest.raises(publish.PublishRefused, match=why):
        publish.origin_gh_repo(repo)


# --- nothing reaches GitHub ------------------------------------------------------------

def test_the_users_git_reads_the_isolated_global_config(world):
    """publish's git (the user's, with their global config) reads this
    test's HOME, not the developer's: so no global push rewrite or
    hooksPath of theirs applies here."""
    done = publish._user_git(world.repo, ["config", "--global", "--get", ISOLATION_MARKER],
                             30)

    assert done.stdout.decode().strip() == "yes"


def test_a_hostile_global_push_rewrite_loses_to_the_checkouts_own(world, no_real_gh,
                                                                   isolated_home):
    """A global `pushInsteadOf` for all of github.com, as many developers
    have (to an SSH URL; here to a path that does not exist, so a push it
    won would fail): origin's push still lands in the local bare
    repository, because World's own rewrite is the longer match."""
    git(world.repo, "config", "--file", str(isolated_home / ".gitconfig"),
        "url.file:///nonexistent/.pushInsteadOf", "https://github.com/")

    assert git(world.repo, "remote", "get-url", "--push", "origin").strip() == str(
        world.origin)
    published = world.publish()

    assert git(world.origin, "rev-parse", f"refs/heads/{world.branch}").strip() == (
        published.sha)


# --- races and leases (correctness review) ---------------------------------------------

def move_on_origin(world, branch, onto):
    """Someone else's commit on top of `onto`, set as origin's `branch`: its SHA."""
    tree = git(world.origin, "rev-parse", f"{onto}^{{tree}}").strip()
    theirs = git(world.origin, "commit-tree", tree, "-p", onto, "-m", "theirs",
                 env={**os.environ, "GIT_AUTHOR_NAME": "Them", "GIT_AUTHOR_EMAIL": "t@x",
                      "GIT_COMMITTER_NAME": "Them", "GIT_COMMITTER_EMAIL": "t@x"}).strip()
    git(world.origin, "update-ref", f"refs/heads/{branch}", theirs)
    return theirs


def test_a_branch_that_appears_after_the_check_is_never_moved(world, no_real_gh,
                                                              monkeypatch):
    """ls-remote said the branch was new; before the push someone created it
    at an ancestor of our commit. A push without a lease would fast-forward
    it; with `--force-with-lease=<ref>:` (must not exist) it is rejected and
    stays where they put it."""
    checked = publish._require_new_branch

    def then_created(repo, branch):
        checked(repo, branch)
        git(world.origin, "branch", branch, world.base)

    monkeypatch.setattr(publish, "_require_new_branch", then_created)

    with pytest.raises(publish.PushFailed, match="appeared on origin"):
        world.publish()

    assert git(world.origin, "rev-parse", world.branch).strip() == world.base
    assert creates(no_real_gh) == []


def test_the_new_branch_check_asks_the_push_target_not_the_fetch_url(world, no_real_gh,
                                                                    tmp_path):
    """A triangular origin: fetches from a mirror, pushes to the bare
    repository. The branch on the push target refuses; the same branch on
    the mirror alone does not."""
    mirror = tmp_path / "mirror.git"
    git(tmp_path, "clone", "--quiet", "--bare", str(world.origin), str(mirror))
    mirror_url = "https://github.com/acme/widgets-mirror.git"
    git(world.repo, "config", "remote.origin.pushurl", GITHUB_URL)
    git(world.repo, "config", "remote.origin.url", mirror_url)
    git(world.repo, "config", f"url.{mirror}.insteadOf", mirror_url)
    git(mirror, "branch", world.branch, world.base)

    published = world.publish()
    assert git(world.origin, "rev-parse", world.branch).strip() == published.sha

    git(world.origin, "update-ref", "-d", f"refs/heads/{world.branch}")
    git(mirror, "update-ref", "-d", f"refs/heads/{world.branch}")
    git(world.origin, "branch", world.branch, world.base)
    with pytest.raises(publish.PublishRefused, match="already exists on origin"):
        world.publish()


def test_the_deletion_never_removes_someone_elses_commit(world, no_real_gh, monkeypatch):
    """Our push landed, someone pushed on top, then the PR failed: the
    deletion is leased to our SHA, so it is rejected and their commit stays."""
    pushed = publish.push_branch
    moved = []

    def then_moved(repo, sha, branch):
        pushed(repo, sha, branch)
        moved.append(move_on_origin(world, branch, sha))

    monkeypatch.setattr(publish, "push_branch", then_moved)
    no_real_gh.feed(fail_on={"pr create": "GraphQL: Head sha can't be blank"})

    with pytest.raises(publish.PrCreateFailed) as failed:
        world.publish()

    assert failed.value.branch_deleted is False
    assert "no longer points at" in failed.value.delete_error
    assert git(world.origin, "rev-parse", world.branch).strip() == moved[0]


def test_an_error_that_is_not_ghs_after_the_push_deletes_the_branch(world, no_real_gh,
                                                                     monkeypatch):
    """An error that is not gh's after the push (here the body file cannot be
    made; a body that cannot be encoded is refused before the push) stays in
    the PublishError family and cleans up."""
    made = tempfile.mkstemp

    def no_body_file(*args, **kwargs):
        if kwargs.get("prefix") == "curamoir-pr-body-":
            raise OSError(28, "No space left on device")
        return made(*args, **kwargs)

    monkeypatch.setattr(publish.tempfile, "mkstemp", no_body_file)

    with pytest.raises(publish.PrCreateFailed, match="No space left") as failed:
        world.publish()

    assert failed.value.branch_deleted is True
    assert world.origin_branches() == ["refs/heads/main"]
    assert creates(no_real_gh) == []


def test_a_branch_name_with_a_nul_is_refused_before_any_push(world, no_real_gh):
    with pytest.raises(publish.PublishRefused):
        world.publish(branch=world.branch + "\x00x")

    nothing_left_the_machine(world, no_real_gh)


def test_the_lookup_ignores_a_pr_of_another_commit_or_base(world, no_real_gh, monkeypatch):
    """A PR from our branch name whose head is not our commit, or whose base
    is not the default branch, is not ours."""
    url = "https://github.com/acme/widgets/pull/7"
    no_real_gh.feed(create_hang="before", prs=[
        {"headRefName": world.branch, "number": 7, "url": url, "isCrossRepository": False,
         "headRefOid": "0" * 40, "baseRefName": "main", "body": "", "files": []},
        {"headRefName": world.branch, "number": 8, "url": url[:-1] + "8",
         "isCrossRepository": False, "headRefOid": None, "baseRefName": "develop",
         "body": "", "files": []}])
    monkeypatch.setattr(publish, "CREATE_TIMEOUT", 2)
    monkeypatch.setattr(publish, "LOOKUP_DELAY", 0)

    with pytest.raises(publish.PrCreateFailed):
        world.publish()

    lookup = next(r["argv"] for r in no_real_gh.call_records() if r["argv"][:2] == ["pr", "list"])
    fields = lookup[lookup.index("--json") + 1].split(",")
    assert {"headRefOid", "baseRefName"} <= set(fields)


def test_a_create_that_errored_after_opening_the_pr_finds_it(world, no_real_gh):
    """gh exited non-zero (a 5xx after GitHub made the PR): one lookup
    before the branch is deleted finds it."""
    no_real_gh.feed(create_fails_with="HTTP 502: Bad Gateway")

    published = world.publish()

    assert published.number == 1
    assert world.origin_branches() == ["refs/heads/main", f"refs/heads/{world.branch}"]


def test_a_create_that_errored_and_cannot_be_looked_up_is_unknown(world, no_real_gh):
    no_real_gh.feed(fail_on={"pr create": "HTTP 502", "pr list": "HTTP 503"})

    with pytest.raises(publish.PrOutcomeUnknown, match="HTTP 503"):
        world.publish()

    assert world.origin_branches() == ["refs/heads/main", f"refs/heads/{world.branch}"]


def test_a_default_branch_origin_has_no_ref_for_is_refused(world, no_real_gh):
    git(world.repo, "update-ref", "-d", "refs/remotes/origin/main")

    with pytest.raises(publish.PublishRefused, match="refs/remotes/origin/main"):
        world.publish()

    nothing_left_the_machine(world, no_real_gh)


def test_author_and_committer_config_are_honoured(world, no_real_gh):
    git(world.repo, "config", "author.name", "Grace Hopper")
    git(world.repo, "config", "author.email", "grace@example.invalid")

    published = world.publish()

    shown = git(world.origin, "log", "-1", "--format=%an <%ae>|%cn <%ce>", published.sha)
    assert shown.strip() == ("Grace Hopper <grace@example.invalid>|"
                             "Ada Lovelace <ada@example.invalid>")



# --- edge cases (edge-case review; several are its probes) -----------------------------

def test_a_commit_message_that_cannot_be_written_is_refused(world, no_real_gh):
    with pytest.raises(publish.PublishRefused, match="commit message"):
        world.publish(message=world.message + "\ud800\n")

    nothing_left_the_machine(world, no_real_gh)


@pytest.mark.parametrize("change", [
    {"body": "marker\n\ud800\n"},
    {"title": "curamoir sweep\x00"},
    {"title": "curamoir sweep \udcff"},
])
def test_a_title_or_body_gh_cannot_be_given_is_refused_before_the_push(world, no_real_gh,
                                                                       tmp_path, change):
    """Checked before anything leaves the machine: no push, so no hook runs."""
    ran = tmp_path / "hook-ran"
    world.hook(f"touch '{ran}'\nexit 0\n")

    with pytest.raises(publish.PublishRefused, match=r"title|body"):
        world.publish(**change)

    nothing_left_the_machine(world, no_real_gh)
    assert not ran.exists()


# The PR's base is gh's answer, checked against the checkout.

def default_on_origin(world, name):
    """origin's default branch becomes `name` (pushed from main), the
    checkout fetches it, and the fake gh reports it."""
    git(world.repo, "push", "--quiet", "origin", f"main:refs/heads/{name}")
    git(world.origin, "symbolic-ref", "HEAD", f"refs/heads/{name}")
    git(world.repo, "fetch", "--quiet", "origin")


def test_the_base_is_origins_default_as_gh_reports_it_not_a_local_guess(world,
                                                                         no_real_gh):
    """No refs/remotes/origin/HEAD and both `main` and `develop` locally: a
    local guess says `main`; GitHub's default is `develop`."""
    default_on_origin(world, "develop")
    git(world.repo, "branch", "develop", "origin/develop")
    git(world.repo, "remote", "set-head", "origin", "-d")
    no_real_gh.feed(default_branch="develop")

    published = world.publish()

    assert published.base_branch == "develop"
    [create] = creates(no_real_gh)
    assert create["argv"][create["argv"].index("--base") + 1] == "develop"
    views = [r["argv"] for r in no_real_gh.call_records() if r["argv"][:2] == ["repo", "view"]]
    assert views == [["repo", "view", GH_REPO, "--json", "defaultBranchRef"]]


def test_a_stale_origin_head_is_refused_with_the_set_head_hint(world, no_real_gh):
    """The default was renamed on GitHub; the checkout's origin/HEAD still
    names the old one, which `git fetch` alone does not change."""
    default_on_origin(world, "trunk")
    no_real_gh.feed(default_branch="trunk")

    with pytest.raises(publish.PublishRefused, match="git remote set-head origin -a"):
        world.publish()

    no_sweep_branch_and_no_pr(world, no_real_gh)


def test_a_default_branch_never_fetched_is_refused_with_the_fetch_hint(world, no_real_gh):
    git(world.origin, "branch", "trunk", "main")
    git(world.origin, "symbolic-ref", "HEAD", "refs/heads/trunk")
    git(world.repo, "remote", "set-head", "origin", "-d")
    no_real_gh.feed(default_branch="trunk")

    with pytest.raises(publish.PublishRefused, match="git fetch origin"):
        world.publish()

    no_sweep_branch_and_no_pr(world, no_real_gh)


def test_gh_failing_to_name_the_default_is_refused_before_the_push(world, no_real_gh):
    no_real_gh.feed(fail_on={"repo view": "HTTP 502: Bad Gateway"})

    with pytest.raises(publish.PublishRefused, match="HTTP 502"):
        world.publish()

    nothing_left_the_machine(world, no_real_gh)


def test_a_slashed_default_branch_is_the_prs_base(world, no_real_gh):
    default_on_origin(world, "release/main")
    git(world.repo, "remote", "set-head", "origin", "release/main")
    git(world.repo, "push", "--quiet", "origin", "--delete", "main")
    git(world.repo, "update-ref", "-d", "refs/remotes/origin/main")
    no_real_gh.feed(default_branch="release/main")

    published = world.publish()

    assert published.base_branch == "release/main"
    [create] = creates(no_real_gh)
    assert create["argv"][create["argv"].index("--base") + 1] == "release/main"


# The push, under stress.

def test_a_hook_and_what_it_started_are_killed_at_the_push_timeout(world, no_real_gh,
                                                                   monkeypatch, tmp_path):
    """The hook backgrounds a child that would write a marker after the
    timeout: the whole process group goes, so the marker never appears."""
    marker = tmp_path / "late-marker"
    world.hook(f"(sleep 2; touch '{marker}') &\nsleep 30\n")
    monkeypatch.setattr(publish, "PUSH_TIMEOUT", 1)

    with pytest.raises(publish.PushFailed) as failed:
        world.publish()

    assert failed.value.timed_out is True
    time.sleep(2.5)
    assert not marker.exists()
    assert world.origin_branches() == ["refs/heads/main"]


def test_a_branch_another_push_created_meanwhile_gets_the_friendly_refusal(world,
                                                                          no_real_gh):
    """The hook itself creates the branch (at the base) while our push is
    under way: origin refuses ours ("reference already exists"), and the
    message is the same as for the lease's refusal."""
    world.hook(f'[ -n "$INNER" ] && exit 0\n'
               f'INNER=1 git push -q origin {world.base}:refs/heads/{world.branch} >&2\n')

    with pytest.raises(publish.PushFailed, match="appeared on origin"):
        world.publish()

    assert git(world.origin, "rev-parse", world.branch).strip() == world.base


_CHILD = """
import json, sys, time
from curamoir import github, publish
a = json.loads(open(sys.argv[1]).read())
github.GH = a.pop("gh")
a["patch"] = bytes.fromhex(a["patch"])
start = a.pop("start")
while time.time() < start:
    time.sleep(0.01)
repo = a.pop("repo")
try:
    print(json.dumps({"ok": publish.publish(repo, **a).number}))
except publish.PublishError as error:
    print(json.dumps({"error": type(error).__name__, "message": str(error)}))
"""


def test_two_publishes_at_once_give_one_pr(world, no_real_gh, tmp_path):
    """Two processes publish the same branch at the same instant (both pass
    the ls-remote check): one Published, one PushFailed, one PR."""
    arguments = {"repo": str(world.repo), "base": world.base, "patch": world.patch.hex(),
                 "paths": list(ACCEPTED), "branch": world.branch, "branch_prefix": PREFIX,
                 "gh_repo": GH_REPO, "message": world.message, "title": TITLE,
                 "body": world.body, "gh": str(no_real_gh.path), "start": time.time() + 2}
    spec = tmp_path / "args.json"
    spec.write_text(json.dumps(arguments), encoding="utf-8")
    root = Path(__file__).resolve().parents[1]
    environment = {**os.environ, "PYTHONPATH": os.pathsep.join([str(root / "src"), str(root)])}
    children = [subprocess.Popen([sys.executable, "-c", _CHILD, str(spec)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 env=environment) for _ in range(2)]
    results = [json.loads(child.communicate(timeout=90)[0]) for child in children]

    assert sorted(result.get("ok") is not None for result in results) == [False, True]
    [lost] = [result for result in results if "error" in result]
    assert lost["error"] == "PushFailed" and "appeared on origin" in lost["message"]
    assert len(creates(no_real_gh)) == 1
    assert world.origin_branches() == ["refs/heads/main", f"refs/heads/{world.branch}"]


# Stopped by the scheduler: Terminated propagates, nothing is deleted, no
# temporary file is left.

@pytest.fixture
def own_tempdir(tmp_path, monkeypatch):
    """publish's temporary files go here, so a test can see none is left
    (and another process's cannot be mistaken for one)."""
    directory = tmp_path / "tmp"
    directory.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(directory))
    return directory


def terminate_in(seconds):
    """SIGTERM this process in `seconds`, raised as Terminated: the
    scheduler stopping the run. Returns the handler to restore."""
    def handler(signum, frame):
        raise Terminated(signum)

    previous = signal.signal(signal.SIGTERM, handler)
    threading.Timer(seconds, os.kill, (os.getpid(), signal.SIGTERM)).start()
    return previous


def test_terminated_in_the_lookup_wait_deletes_nothing(world, no_real_gh, monkeypatch,
                                                       own_tempdir):
    no_real_gh.feed(create_hang="before")
    monkeypatch.setattr(publish, "CREATE_TIMEOUT", 1)

    def stopped(seconds):
        raise Terminated(signal.SIGTERM)

    monkeypatch.setattr(publish, "time", types.SimpleNamespace(sleep=stopped))

    with pytest.raises(Terminated):
        world.publish()

    assert world.origin_branches() == ["refs/heads/main", f"refs/heads/{world.branch}"]
    assert list(own_tempdir.iterdir()) == []


def test_terminated_during_gh_pr_create_deletes_nothing(world, no_real_gh, monkeypatch,
                                                        own_tempdir):
    no_real_gh.feed(create_hang="before")
    monkeypatch.setattr(publish, "CREATE_TIMEOUT", 30)
    previous = terminate_in(1.5)
    started = time.monotonic()
    try:
        with pytest.raises(Terminated):
            world.publish()
    finally:
        signal.signal(signal.SIGTERM, previous)

    assert time.monotonic() - started < 20
    assert world.origin_branches() == ["refs/heads/main", f"refs/heads/{world.branch}"]
    assert list(own_tempdir.iterdir()) == []


def terminate_when(marker, bound=20):
    """SIGTERM this process, raised as Terminated, as soon as `marker`
    exists (polled, for at most `bound` seconds): stopped at a known point,
    not after a guessed delay. Returns the handler to restore."""
    def handler(signum, frame):
        raise Terminated(signum)

    def watch():
        deadline = time.monotonic() + bound
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        os.kill(os.getpid(), signal.SIGTERM)

    previous = signal.signal(signal.SIGTERM, handler)
    threading.Thread(target=watch, daemon=True).start()
    return previous


def test_terminated_during_the_push_leaves_nothing_running(world, no_real_gh, tmp_path,
                                                           own_tempdir):
    """Stopped while the pre-push hook runs (it has written `started`): the
    push's process group goes with it, so the hook never finishes."""
    started, finished = tmp_path / "hook-started", tmp_path / "hook-finished"
    world.hook(f"touch '{started}'\nsleep 2\ntouch '{finished}'\n")
    previous = terminate_when(started)
    try:
        with pytest.raises(Terminated):
            world.publish()
    finally:
        signal.signal(signal.SIGTERM, previous)

    assert started.exists()
    time.sleep(2.5)
    assert not finished.exists()
    assert world.origin_branches() == ["refs/heads/main"]
    assert list(own_tempdir.iterdir()) == []


# Checkouts and paths.

@pytest.mark.parametrize("name", ["with space.py", "-rf", "new\nline.py", "tab\there.py",
                                  'q"uote.py', unicodedata.normalize("NFC", "café.py")])
def test_odd_path_names_are_committed_exactly(tmp_path, no_real_gh, name):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--quiet", "--bare", "--initial-branch=main",
                    str(origin)], check=True)
    repo = tmp_path / "checkout"
    repo.mkdir()
    (repo / name).write_bytes(b"x = 1\n")
    (repo / "z.py").write_bytes(b"z = 1\n")
    for command in (["init", "--quiet", "--initial-branch=main"],
                    ["config", "user.name", "Ada Lovelace"],
                    ["config", "user.email", "ada@example.invalid"],
                    ["add", "-A"], ["commit", "--quiet", "-m", "base"],
                    ["remote", "add", "origin", GITHUB_URL],
                    ["config", f"url.{origin}.insteadOf", GITHUB_URL],
                    ["config", f"url.{origin}.pushInsteadOf", GITHUB_URL],
                    ["push", "--quiet", "origin", "main"],
                    ["remote", "set-head", "origin", "main"]):
        git(repo, *command)
    base = git(repo, "rev-parse", "HEAD").strip()
    (tmp_path / "sandboxes").mkdir()
    with prepare_sandbox(repo, base, parent=tmp_path / "sandboxes") as sandbox:
        (sandbox.path / name).write_bytes(b"x = 2\n")
        patch = sandbox.patch((name,))

    published = publish.publish(
        repo, base=base, patch=patch, paths=(name,), branch=sweep_branch(
            PREFIX, date(2026, 10, 24), base), branch_prefix=PREFIX, gh_repo=GH_REPO,
        message="m\n", title=TITLE, body=f"{sweep_marker(base)}\nbody\n")

    changed = subprocess.run(["git", "diff-tree", "-r", "-z", "--name-only", base,
                              published.sha], cwd=origin, capture_output=True,
                             check=True).stdout
    assert [n.decode("utf-8") for n in changed.split(b"\0") if n] == [name]


def test_a_linked_worktree_checkout_publishes_and_runs_the_repos_hook(world, no_real_gh,
                                                                      tmp_path):
    ran = tmp_path / "hook-ran"
    world.hook(f"touch '{ran}'\nexit 0\n")
    worktree = tmp_path / "worktree"
    git(world.repo, "worktree", "add", "--quiet", "-b", "elsewhere", str(worktree))

    published = publish.publish(
        worktree, base=world.base, patch=world.patch, paths=ACCEPTED, branch=world.branch,
        branch_prefix=PREFIX, gh_repo=GH_REPO, message=world.message, title=TITLE,
        body=world.body)

    assert git(world.origin, "rev-parse", world.branch).strip() == published.sha
    assert ran.exists()



# --- provenance review: what the other tests left unproven -----------------------------

def after_the_push(monkeypatch, then):
    """Run `then(sha)` right after publish's push succeeds: for fake gh
    payloads that must name the commit the sweep pushed."""
    pushed = publish.push_branch

    def push_then(repo, sha, branch):
        pushed(repo, sha, branch)
        then(sha)

    monkeypatch.setattr(publish, "push_branch", push_then)


def our_pr(world, sha, number=7, **changes):
    """A `pr list` item that is the sweep's PR in every field, changed by
    `changes`."""
    item = {"headRefName": world.branch, "number": number,
            "url": f"https://github.com/acme/widgets/pull/{number}",
            "isCrossRepository": False, "headRefOid": sha, "baseRefName": "main",
            "body": world.body, "files": []}
    item.update(changes)
    return item


def a_fork_pr(world, sha):
    """The recorded fork PR (tests/gh_pr_payloads.py), as a sweep PR from our
    branch name at our commit into the default branch: a match in every
    field but isCrossRepository."""
    item = as_sweep_pr(FORK_PRS[0], world.branch, world.base, number=7)
    item.update(url="https://github.com/acme/widgets/pull/7", headRefOid=sha,
                baseRefName="main")
    assert item["isCrossRepository"] is True
    return item


@pytest.mark.parametrize("listed, outcome", [
    # one field off: not the sweep's PR, so none is found and the branch goes
    (lambda world, sha: [our_pr(world, sha, baseRefName="develop")], publish.PrCreateFailed),
    (lambda world, sha: [a_fork_pr(world, sha)], publish.PrCreateFailed),
    # the lookup cannot tell: its answer is an error, so the outcome is unknown
    (lambda world, sha: [our_pr(world, sha, 7), our_pr(world, sha, 8)],
     publish.PrOutcomeUnknown),
    (lambda world, sha: [our_pr(world, sha, 7,
                                url="https://github.com/acme/widgets/pull/9")],
     publish.PrOutcomeUnknown),
], ids=["another-base", "a-fork", "two-matches", "number-not-the-urls"])
def test_the_lookup_takes_only_the_sweeps_own_pr(world, no_real_gh, monkeypatch, listed,
                                                 outcome):
    """After a failed create, find_open_pr's filters: same commit into
    another base, or from a fork, is not the PR (PrCreateFailed, branch
    deleted); two matches, or a number its URL disagrees with, is a lookup
    error (PrOutcomeUnknown, branch left). Never Published."""
    no_real_gh.feed(fail_on={"pr create": "HTTP 502"})
    after_the_push(monkeypatch, lambda sha: no_real_gh.feed(prs=listed(world, sha)))

    with pytest.raises(outcome):
        world.publish()

    left = f"refs/heads/{world.branch}" in world.origin_branches()
    assert left is (outcome is publish.PrOutcomeUnknown)


def test_unreadable_create_output_gets_every_lookup(world, no_real_gh, monkeypatch):
    """gh exited 0 without the URL and GitHub lists the PR only from the
    second look: CreateOutputUnreadable gets LOOKUP_ATTEMPTS, not one."""
    no_real_gh.feed(create_stdout="Creating pull request...\n", create_hidden_from_lists=1)
    monkeypatch.setattr(publish, "LOOKUP_DELAY", 0)

    published = world.publish()

    assert published.number == 1
    lookups = [r for r in no_real_gh.call_records() if r["argv"][:2] == ["pr", "list"]]
    assert len(lookups) == 2


def test_an_inherited_git_environment_cannot_redirect_publish(world, no_real_gh, monkeypatch,
                                                              tmp_path):
    """GIT_DIR, GIT_INDEX_FILE and GIT_CONFIG_PARAMETERS from whatever started
    the run (a hook, a wrapper) point git at a decoy repository, an index
    of their own and a push rewrite to nowhere; publish's gits drop them."""
    decoy = tmp_path / "decoy.git"
    git(tmp_path, "init", "--quiet", "--bare", str(decoy))
    hostile_index = tmp_path / "hostile-index"
    before = checkout_state(world.repo)
    with monkeypatch.context() as hostile:
        hostile.setenv("GIT_DIR", str(decoy))
        hostile.setenv("GIT_INDEX_FILE", str(hostile_index))
        hostile.setenv("GIT_CONFIG_PARAMETERS",
                       "'url.file:///nowhere/.pushinsteadof'='https://github.com/'")
        published = world.publish()

    assert git(world.origin, "rev-parse", world.branch).strip() == published.sha
    assert git(world.origin, "rev-parse", f"{published.sha}^").strip() == world.base
    assert not hostile_index.exists()
    assert git(decoy, "for-each-ref") == ""
    assert checkout_state(world.repo) == before


def test_the_push_keeps_the_users_transport_and_never_prompts(world, no_real_gh,
                                                              monkeypatch, tmp_path):
    """The pre-push hook sees the push's environment: the user's
    GIT_SSH_COMMAND reaches it, and GIT_TERMINAL_PROMPT is 0 whatever the
    caller had."""
    dump = tmp_path / "hook-env"
    world.hook(f"env > '{dump}'\nexit 0\n")
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "1")

    world.publish()

    seen = dict(line.split("=", 1) for line in dump.read_text().splitlines() if "=" in line)
    assert seen.get("GIT_SSH_COMMAND") == "ssh -o BatchMode=yes"
    assert seen.get("GIT_TERMINAL_PROMPT") == "0"


def test_committer_config_is_honoured(world, no_real_gh):
    git(world.repo, "config", "committer.name", "Katherine Johnson")
    git(world.repo, "config", "committer.email", "katherine@example.invalid")

    published = world.publish()

    shown = git(world.origin, "log", "-1", "--format=%an <%ae>|%cn <%ce>", published.sha)
    assert shown.strip() == ("Ada Lovelace <ada@example.invalid>|"
                             "Katherine Johnson <katherine@example.invalid>")


def test_a_configured_signing_program_never_runs(world, no_real_gh, tmp_path):
    """commit.gpgsign in the checkout, and a signing program that fails (and
    would leave a marker): the commit is made unsigned, and it never runs.
    On git 2.39 `commit-tree` ignores commit.gpgsign anyway, so this does
    not depend on `--no-gpg-sign`; it holds publish to its promise on any
    git that does read it."""
    ran = tmp_path / "signer-ran"
    signer = tmp_path / "signer.sh"
    signer.write_text(f"#!/bin/sh\ntouch '{ran}'\nexit 1\n", encoding="utf-8")
    signer.chmod(0o755)
    git(world.repo, "config", "commit.gpgsign", "true")
    git(world.repo, "config", "gpg.program", str(signer))

    published = world.publish()

    assert git(world.origin, "rev-parse", world.branch).strip() == published.sha
    assert not ran.exists()


@pytest.mark.parametrize("branch, why", [
    ("curamoir/feature-x", "not a sweep branch"),
    ("curamoir/sweep-a..b", "not a valid branch name"),
    ("curamoir/sweep-x.lock", "not a valid branch name"),
])
def test_a_prefixed_branch_that_is_not_a_sweep_branch_is_refused(world, no_real_gh, branch,
                                                                 why):
    with pytest.raises(publish.PublishRefused, match=why):
        world.publish(branch=branch)

    nothing_left_the_machine(world, no_real_gh)



def test_a_branch_that_appeared_at_the_sweeps_own_commit_is_not_taken(world, no_real_gh,
                                                                       monkeypatch):
    """After the ls-remote check, the branch appears on origin at the very
    commit the sweep built (another publish of the same base in the same
    second: commit-tree is deterministic). git reports `=` [up to date] and
    exits 0, which passes the lease; the branch is still someone else's, so
    publish refuses it as "appeared" and opens no PR on it."""
    pushed = publish.push_branch

    def same_commit_lands_first(repo, sha, branch):
        git(world.repo, "push", "--quiet", "origin", f"{sha}:refs/heads/{branch}")
        landed.append(sha)
        pushed(repo, sha, branch)

    landed = []
    monkeypatch.setattr(publish, "push_branch", same_commit_lands_first)

    with pytest.raises(publish.PushFailed, match="appeared on origin") as failed:
        world.publish()

    assert failed.value.timed_out is False
    assert creates(no_real_gh) == []
    assert git(world.origin, "rev-parse", world.branch).strip() == landed[0]
