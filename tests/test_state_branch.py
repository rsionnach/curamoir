"""cm-2qp.4 (E14.4): `curamoir state push`, the hosted run's ledger lines
committed to a state branch (curamoir.state_branch).

Every test runs real git, the way tests/test_publish.py does: a user
checkout whose `origin` names a GitHub repository and is redirected, by
the checkout's own `url.<path>.insteadOf` and `pushInsteadOf`, to a local
bare repository, so the fetch and the push really happen and the bare
repository shows what landed. HOME is isolated (test_publish's autouse
`isolated_home`, imported here) so the developer's global git config can
send nothing to github.com.

The checkout's ledger is TRACKED, as it is in a real repository
(ledger.LEDGER_PATH is committed): HEAD holds one earlier line, and a "run"
appends to the work tree's copy, as `curamoir run sweep` does on a runner.
"""

import errno
import json
import os
import stat
import subprocess
import threading

import pytest

from curamoir import state_branch
from curamoir.cli import main
from curamoir.ledger import LEDGER_PATH, repo_ledger
from tests.test_publish import checkout_state, git, isolated_home  # noqa: F401 (fixture)

GITHUB_URL = "https://github.com/acme/widgets.git"
LEDGER = "/".join(LEDGER_PATH)
BRANCH = "curamoir/state"
COMMITTED = '{"job": "sweep", "kind": "job", "run_id": "old", "outcome": "success"}'


class World:
    """A checkout with a committed ledger line, and its bare origin."""

    def __init__(self, tmp_path):
        self.origin = tmp_path / "origin.git"
        subprocess.run(["git", "init", "--quiet", "--bare", "--initial-branch=main",
                        str(self.origin)], check=True)
        self.repo = tmp_path / "checkout"
        self.ledger = self.repo.joinpath(*LEDGER_PATH)
        self.ledger.parent.mkdir(parents=True)
        self.ledger.write_text(COMMITTED + "\n", encoding="utf-8")
        (self.repo / "app.py").write_text("x = 1\n", encoding="utf-8")
        for command in (["init", "--quiet", "--initial-branch=main"],
                        ["config", "user.name", "github-actions[bot]"],
                        ["config", "user.email",
                         "41898282+github-actions[bot]@users.noreply.github.com"],
                        ["add", "-A"], ["commit", "--quiet", "-m", "base"],
                        ["remote", "add", "origin", GITHUB_URL],
                        ["config", f"url.{self.origin}.insteadOf", GITHUB_URL],
                        ["config", f"url.{self.origin}.pushInsteadOf", GITHUB_URL],
                        ["push", "--quiet", "origin", "main"]):
            git(self.repo, *command)
        self.main = self.origin_sha("main")

    def run(self, run_id, outcome="success", reason=None):
        """What one `curamoir run` leaves: its job line, appended to the work tree."""
        return repo_ledger(self.repo).record("sweep", reason, f"{run_id} done", run_id=run_id,
                                             outcome=outcome)

    def fresh_run(self, run_id, **fields):
        """A new runner's checkout: the work tree back at HEAD, then one run."""
        git(self.repo, "checkout", "--quiet", "--", LEDGER)
        return self.run(run_id, **fields)

    def origin_sha(self, branch):
        done = subprocess.run(["git", "rev-parse", "--verify", "--quiet",
                               f"refs/heads/{branch}"], cwd=self.origin,
                              capture_output=True, text=True)
        return done.stdout.strip() or None

    def state_lines(self, branch=BRANCH):
        text = git(self.origin, "show", f"{branch}:{LEDGER}")
        return [json.loads(line) for line in text.splitlines() if line]

    def run_ids(self, branch=BRANCH):
        return [entry.get("run_id") for entry in self.state_lines(branch)]


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


# --- what lands ------------------------------------------------------------------------

def test_the_first_push_creates_the_branch_from_the_runs_lines_only(world):
    world.run("r1")

    pushed = state_branch.push_ledger(world.repo, BRANCH)

    assert pushed.outcome == state_branch.PUSHED, pushed.sentence
    assert pushed.lines == 1
    assert world.origin_sha(BRANCH) == pushed.sha
    # An orphan: no parent, and a tree of exactly the ledger, at its path.
    assert git(world.origin, "rev-list", "--parents", "-n", "1", pushed.sha).split() == [
        pushed.sha]
    assert git(world.origin, "ls-tree", "-r", "--name-only", pushed.sha).split() == [LEDGER]
    # The default branch's committed line is not the run's, so it is not pushed.
    assert world.run_ids() == ["r1"]


def test_a_second_run_appends_to_what_the_branch_holds(world):
    world.run("r1")
    first = state_branch.push_ledger(world.repo, BRANCH)
    world.fresh_run("r2")

    second = state_branch.push_ledger(world.repo, BRANCH)

    assert second.outcome == state_branch.PUSHED, second.sentence
    assert world.run_ids() == ["r1", "r2"]
    assert git(world.origin, "rev-parse", f"{second.sha}^").strip() == first.sha


def test_a_refused_runs_line_is_pushed_too(world):
    world.run("refused-1", outcome="refused", reason="runner")

    pushed = state_branch.push_ledger(world.repo, BRANCH)

    assert pushed.outcome == state_branch.PUSHED, pushed.sentence
    [entry] = world.state_lines()
    assert (entry["run_id"], entry["outcome"], entry["reason"]) == (
        "refused-1", "refused", "runner")


def test_lines_already_on_the_branch_are_not_pushed_again(world):
    world.run("r1")
    first = state_branch.push_ledger(world.repo, BRANCH)

    again = state_branch.push_ledger(world.repo, BRANCH)

    assert again.outcome == state_branch.NOTHING_TO_PUSH, again.sentence
    assert world.origin_sha(BRANCH) == first.sha
    assert world.run_ids() == ["r1"]


def test_a_run_that_wrote_nothing_has_nothing_to_push_and_creates_no_branch(world):
    pushed = state_branch.push_ledger(world.repo, BRANCH)

    assert pushed.outcome == state_branch.NOTHING_TO_PUSH
    assert world.origin_sha(BRANCH) is None


def test_a_branch_ledger_with_an_unterminated_last_line_is_closed_first(world):
    """A truncated line on the branch must not swallow the run's line."""
    world.run("r1")
    state_branch.push_ledger(world.repo, BRANCH)
    blob = git(world.origin, "show", f"{BRANCH}:{LEDGER}").rstrip("\n") + "\n" + '{"trunc'
    _commit_on_origin(world, BRANCH, blob)
    world.fresh_run("r2")

    state_branch.push_ledger(world.repo, BRANCH)

    text = git(world.origin, "show", f"{BRANCH}:{LEDGER}")
    assert text.splitlines()[-2:] == ['{"trunc', text.splitlines()[-1]]
    assert json.loads(text.splitlines()[-1])["run_id"] == "r2"


def test_an_existing_branch_with_other_files_is_refused_not_appended_to(world):
    """Only the layout this code creates (a tree of exactly the ledger) is a
    state branch; anything else is someone's branch with the same name."""
    world.run("r1")
    state_branch.push_ledger(world.repo, BRANCH)
    theirs = _commit_on_origin(world, BRANCH, git(world.origin, "show", f"{BRANCH}:{LEDGER}"),
                               extra={"README.md": "state branch\n"})
    world.fresh_run("r2")

    pushed = state_branch.push_ledger(world.repo, BRANCH)

    assert pushed.outcome == state_branch.REFUSED, pushed.sentence
    assert BRANCH in pushed.sentence and "CURAMOIR_STATE_BRANCH" in pushed.sentence
    # Runner-neutral: the command's own flag first, the workflow's variable second.
    assert "with --branch (CURAMOIR_STATE_BRANCH in the Actions workflow)" in pushed.sentence
    assert world.origin_sha(BRANCH) == theirs


def test_an_existing_branch_that_holds_the_code_is_refused(world):
    """A branch someone already uses under the state branch's name (here, a
    copy of main) is never written to."""
    git(world.repo, "push", "--quiet", "origin", f"main:refs/heads/{BRANCH}")
    world.run("r1")

    pushed = state_branch.push_ledger(world.repo, BRANCH)

    assert pushed.outcome == state_branch.REFUSED, pushed.sentence
    assert "not a state branch" in pushed.sentence
    assert world.origin_sha(BRANCH) == world.main


# --- concurrency -----------------------------------------------------------------------

def _commit_on_origin(world, branch, ledger_text, extra=None):
    """Someone else's commit on origin's `branch` holding `ledger_text`: its SHA."""
    scratch = world.repo.parent / "someone-else"
    if scratch.exists():
        subprocess.run(["rm", "-rf", str(scratch)], check=True)
    env = {**os.environ, "GIT_AUTHOR_NAME": "Them", "GIT_AUTHOR_EMAIL": "t@x",
           "GIT_COMMITTER_NAME": "Them", "GIT_COMMITTER_EMAIL": "t@x"}
    git(world.repo.parent, "clone", "--quiet", "--branch", branch, str(world.origin),
        str(scratch))
    path = scratch.joinpath(*LEDGER_PATH)
    path.write_text(ledger_text, encoding="utf-8")
    for name, text in (extra or {}).items():
        (scratch / name).write_text(text, encoding="utf-8")
    git(scratch, "add", "-A")
    git(scratch, "commit", "--quiet", "-m", "theirs", env=env)
    git(scratch, "push", "--quiet", "origin", f"HEAD:refs/heads/{branch}")
    return git(scratch, "rev-parse", "HEAD").strip()


def test_a_concurrent_push_fails_the_lease_and_the_retry_keeps_both_lines(world,
                                                                          monkeypatch):
    world.run("r1")
    state_branch.push_ledger(world.repo, BRANCH)
    world.fresh_run("ours")
    fetched = state_branch._fetch_tip
    raced = []

    def then_someone_pushes(repo, push_url, branch):
        tip = fetched(repo, push_url, branch)
        if not raced:
            text = git(world.origin, "show", f"{BRANCH}:{LEDGER}")
            raced.append(_commit_on_origin(
                world, BRANCH, text + '{"job": "sweep", "run_id": "theirs"}\n'))
        return tip

    monkeypatch.setattr(state_branch, "_fetch_tip", then_someone_pushes)

    pushed = state_branch.push_ledger(world.repo, BRANCH)

    assert pushed.outcome == state_branch.PUSHED, pushed.sentence
    assert pushed.attempts == 2
    assert world.run_ids() == ["r1", "theirs", "ours"]
    assert git(world.origin, "rev-parse", f"{pushed.sha}^").strip() == raced[0]


def test_a_concurrent_first_push_fails_the_empty_lease_and_retries(world, monkeypatch):
    """Two runs both find no state branch; the second must not overwrite the first."""
    world.run("ours")
    fetched = state_branch._fetch_tip
    raced = []

    def then_someone_creates_it(repo, push_url, branch):
        tip = fetched(repo, push_url, branch)
        if not raced:
            raced.append(_create_on_origin(world, '{"job": "sweep", "run_id": "theirs"}\n'))
        return tip

    monkeypatch.setattr(state_branch, "_fetch_tip", then_someone_creates_it)

    pushed = state_branch.push_ledger(world.repo, BRANCH)

    assert pushed.outcome == state_branch.PUSHED, pushed.sentence
    assert world.run_ids() == ["theirs", "ours"]


def _create_on_origin(world, ledger_text):
    """Someone else's orphan state branch on origin, holding `ledger_text`."""
    scratch = world.repo.parent / "first-elsewhere"
    env = {**os.environ, "GIT_AUTHOR_NAME": "Them", "GIT_AUTHOR_EMAIL": "t@x",
           "GIT_COMMITTER_NAME": "Them", "GIT_COMMITTER_EMAIL": "t@x"}
    git(world.repo.parent, "init", "--quiet", "--initial-branch=state", str(scratch))
    path = scratch.joinpath(*LEDGER_PATH)
    path.parent.mkdir(parents=True)
    path.write_text(ledger_text, encoding="utf-8")
    git(scratch, "add", "-A")
    git(scratch, "commit", "--quiet", "-m", "theirs", env=env)
    git(scratch, "push", "--quiet", str(world.origin), f"HEAD:refs/heads/{BRANCH}")
    return git(scratch, "rev-parse", "HEAD").strip()


def test_a_branch_that_never_stops_moving_fails_after_bounded_attempts(world, monkeypatch):
    world.run("r1")
    state_branch.push_ledger(world.repo, BRANCH)
    world.fresh_run("ours")
    fetched = state_branch._fetch_tip
    theirs = []

    def always_raced(repo, push_url, branch):
        tip = fetched(repo, push_url, branch)
        text = git(world.origin, "show", f"{BRANCH}:{LEDGER}")
        theirs.append(_commit_on_origin(
            world, BRANCH, text + json.dumps({"run_id": f"t{len(theirs)}"}) + "\n"))
        return tip

    monkeypatch.setattr(state_branch, "_fetch_tip", always_raced)

    pushed = state_branch.push_ledger(world.repo, BRANCH)

    assert pushed.outcome == state_branch.FAILED
    assert pushed.attempts == state_branch.PUSH_ATTEMPTS
    assert "(on a hosted runner they are lost with the VM)" in pushed.sentence
    assert "ours" not in world.run_ids()
    # Nothing of theirs was lost: origin is at their last commit.
    assert world.origin_sha(BRANCH) == theirs[-1]


# --- what is never touched -------------------------------------------------------------

def test_the_checkouts_index_head_and_work_tree_are_unchanged(world):
    world.run("r1")
    (world.repo / "app.py").write_text("x = 'uncommitted'\n", encoding="utf-8")
    before = checkout_state(world.repo)

    state_branch.push_ledger(world.repo, BRANCH)

    assert checkout_state(world.repo) == before


def test_the_default_branch_on_origin_is_never_moved(world):
    world.run("r1")

    state_branch.push_ledger(world.repo, BRANCH)
    world.fresh_run("r2")
    state_branch.push_ledger(world.repo, BRANCH)

    assert world.origin_sha("main") == world.main


@pytest.mark.parametrize("branch", ["main", "-x", "bad..name", "", "a\0b"])
def test_the_default_branch_or_a_bad_name_is_refused_before_anything_is_pushed(world,
                                                                              branch):
    world.run("r1")

    pushed = state_branch.push_ledger(world.repo, branch)

    assert pushed.outcome == state_branch.REFUSED, pushed.sentence
    assert world.origin_sha("main") == world.main
    assert git(world.origin, "for-each-ref", "--format=%(refname)").split() == [
        "refs/heads/main"]


def test_origins_default_is_refused_even_when_the_checkout_does_not_know_it(world):
    """The checkout has no origin/HEAD; origin's own HEAD names the default."""
    git(world.origin, "symbolic-ref", "HEAD", "refs/heads/trunk")
    git(world.origin, "branch", "trunk", "main")
    world.run("r1")

    pushed = state_branch.push_ledger(world.repo, "trunk")

    assert pushed.outcome == state_branch.REFUSED
    assert "default branch" in pushed.sentence


def test_a_ledger_that_is_a_symlink_is_refused(world, tmp_path):
    """A repository may commit the ledger path as a link to a secret on the
    runner; following it would publish the secret on the state branch."""
    secret = tmp_path / "secret"
    secret.write_text("token=hunter2\n", encoding="utf-8")
    world.ledger.unlink()
    world.ledger.symlink_to(secret)

    pushed = state_branch.push_ledger(world.repo, BRANCH)

    assert pushed.outcome == state_branch.REFUSED
    # The lstat pre-check's own sentence, not the open's ELOOP fallback.
    assert f"'{LEDGER}' is a symbolic link in the work tree" in pushed.sentence
    assert world.origin_sha(BRANCH) is None


# --- cm-2qp.13: no symbolic link at any component of the ledger's path --------------------

def _decoy_curamoir(world, tmp_path):
    """A decoy copy of the work tree's `.curamoir` outside the checkout,
    whose ledger also holds a valid line (run `from-the-link`): what a
    link would make the push read and publish if it were followed. Its
    path."""
    elsewhere = tmp_path / "elsewhere" / ".curamoir"
    ledger = elsewhere.joinpath(*LEDGER_PATH[1:])
    ledger.parent.mkdir(parents=True)
    line = json.dumps({"job": "sweep", "kind": "job", "run_id": "from-the-link",
                       "outcome": "success"}, sort_keys=True)
    ledger.write_bytes(world.ledger.read_bytes() + line.encode("utf-8") + b"\n")
    return elsewhere


def test_a_curamoir_directory_that_is_a_symlink_is_refused_and_not_followed(world,
                                                                             tmp_path):
    """A repository may commit `.curamoir` itself as a link (or the VM's
    tests or fix agent may plant one): following it would read another
    file's JSON lines and publish them, redacted, on the state branch."""
    world.run("r1")
    first = state_branch.push_ledger(world.repo, BRANCH)
    world.fresh_run("r2")
    target = _decoy_curamoir(world, tmp_path)
    real = world.repo / ".curamoir"
    real.rename(tmp_path / "real-curamoir")
    real.symlink_to(target, target_is_directory=True)

    pushed = state_branch.push_ledger(world.repo, BRANCH)

    assert pushed.outcome == state_branch.REFUSED, pushed.sentence
    assert "'.curamoir'" in pushed.sentence and "symbolic link" in pushed.sentence
    # Nothing the link points at reached the branch: it did not move.
    assert world.origin_sha(BRANCH) == first.sha
    assert world.run_ids() == ["r1"]


def test_a_state_directory_that_is_a_symlink_is_refused_and_not_followed(world, tmp_path):
    world.run("r1")
    target = _decoy_curamoir(world, tmp_path) / "state"
    state = world.ledger.parent
    state.rename(tmp_path / "real-state")
    state.symlink_to(target, target_is_directory=True)

    pushed = state_branch.push_ledger(world.repo, BRANCH)

    assert pushed.outcome == state_branch.REFUSED, pushed.sentence
    assert "'.curamoir/state'" in pushed.sentence and "symbolic link" in pushed.sentence
    assert world.origin_sha(BRANCH) is None


@pytest.mark.parametrize("component", [".curamoir", ".curamoir/state"])
def test_a_directory_on_the_ledgers_path_that_is_a_regular_file_is_refused(world,
                                                                         component):
    world.run("r1")
    world.ledger.unlink()
    world.ledger.parent.rmdir()
    if component == ".curamoir":
        (world.repo / ".curamoir").rmdir()
    (world.repo / component).write_text('{"run_id": "not-a-directory"}\n',
                                        encoding="utf-8")

    pushed = state_branch.push_ledger(world.repo, BRANCH)

    assert pushed.outcome == state_branch.REFUSED, pushed.sentence
    assert f"'{component}' is not a directory" in pushed.sentence
    assert world.origin_sha(BRANCH) is None


def test_a_ledger_that_is_a_directory_is_refused(world):
    world.ledger.unlink()
    world.ledger.mkdir()

    pushed = state_branch.push_ledger(world.repo, BRANCH)

    assert pushed.outcome == state_branch.REFUSED, pushed.sentence
    assert f"'{LEDGER}'" in pushed.sentence and "not a regular file" in pushed.sentence
    assert world.origin_sha(BRANCH) is None


def test_a_ledger_that_is_a_fifo_is_refused_without_blocking(world):
    """Opening a FIFO for reading waits for a writer; the push must refuse
    it, not hang the workflow step."""
    world.ledger.unlink()
    os.mkfifo(world.ledger)

    pushed = state_branch.push_ledger(world.repo, BRANCH)

    assert pushed.outcome == state_branch.REFUSED, pushed.sentence
    assert world.origin_sha(BRANCH) is None


def _opened_names(monkeypatch):
    """Record every name os.open is given (and open it as usual); the list."""
    real_open, opened = os.open, []

    def recording_open(path, *args, **kwargs):
        opened.append(os.fsdecode(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", recording_open)
    return opened


@pytest.mark.parametrize("kind", ["fifo", "directory"])
def test_a_ledger_of_another_kind_is_refused_before_it_is_ever_opened(world, monkeypatch,
                                                                      kind):
    """The lstat check refuses a FIFO or a directory at the ledger's name
    before any open (module docstring: never opened), not only the fstat
    check after it: the ledger's name is never passed to os.open."""
    world.ledger.unlink()
    if kind == "fifo":
        os.mkfifo(world.ledger)
    else:
        world.ledger.mkdir()
    opened = _opened_names(monkeypatch)

    pushed = state_branch.push_ledger(world.repo, BRANCH)

    assert pushed.outcome == state_branch.REFUSED, pushed.sentence
    opened_names = {os.path.basename(name) for name in opened}
    assert LEDGER_PATH[1] in opened_names  # the spy saw the walk's opens
    assert LEDGER_PATH[-1] not in opened_names
    assert world.origin_sha(BRANCH) is None

@pytest.mark.parametrize("missing", [".curamoir/state", ".curamoir"])
def test_a_missing_directory_on_the_ledgers_path_has_nothing_to_push(world, missing):
    world.ledger.unlink()
    world.ledger.parent.rmdir()
    if missing == ".curamoir":
        (world.repo / ".curamoir").rmdir()

    pushed = state_branch.push_ledger(world.repo, BRANCH)

    assert pushed.outcome == state_branch.NOTHING_TO_PUSH, pushed.sentence
    assert world.origin_sha(BRANCH) is None


@pytest.mark.parametrize("vanished", [".curamoir/state/ledger.jsonl", ".curamoir/state",
                                      ".curamoir"])
def test_a_component_that_vanishes_after_the_check_has_nothing_to_push(world, monkeypatch,
                                                                       vanished):
    """A component that passed the lstat check and is gone by its open
    (FileNotFoundError at the open) is a missing ledger: nothing to push,
    not a failure, and the branch does not move."""
    world.run("r1")
    world.ledger.unlink()
    if vanished != ".curamoir/state/ledger.jsonl":
        world.ledger.parent.rmdir()
    if vanished == ".curamoir":
        (world.repo / ".curamoir").rmdir()
    monkeypatch.setattr(state_branch, "_check_component", lambda *args: True)

    pushed = state_branch.push_ledger(world.repo, BRANCH)

    assert pushed.outcome == state_branch.NOTHING_TO_PUSH, pushed.sentence
    assert world.origin_sha(BRANCH) is None

@pytest.mark.parametrize("linked", [".curamoir", ".curamoir/state",
                                    ".curamoir/state/ledger.jsonl"])
def test_a_link_swapped_in_after_the_check_is_refused_by_the_open_itself(world, tmp_path,
                                                                        monkeypatch,
                                                                        linked):
    """The lstat check only names what it finds; the guarantee is the
    O_NOFOLLOW open. With the check blinded (a link swapped in between
    the two), the open still refuses and reads nothing through the link."""
    world.run("r1")
    target = _decoy_curamoir(world, tmp_path)
    path = {".curamoir": world.repo / ".curamoir", ".curamoir/state": world.ledger.parent,
            ".curamoir/state/ledger.jsonl": world.ledger}[linked]
    pointed = {".curamoir": target, ".curamoir/state": target / "state",
               ".curamoir/state/ledger.jsonl": target.joinpath(*LEDGER_PATH[1:])}[linked]
    path.rename(tmp_path / "moved-aside")
    path.symlink_to(pointed, target_is_directory=not linked.endswith(".jsonl"))
    monkeypatch.setattr(state_branch, "_check_component", lambda *args: True)

    pushed = state_branch.push_ledger(world.repo, BRANCH)

    assert pushed.outcome == state_branch.REFUSED, pushed.sentence
    assert "changed into a symbolic link" in pushed.sentence
    assert world.origin_sha(BRANCH) is None


@pytest.mark.parametrize("component", [".curamoir", ".curamoir/state"])
def test_a_file_swapped_in_for_a_directory_after_the_check_is_refused_by_the_open(
        world, monkeypatch, component):
    """O_DIRECTORY refuses the swapped-in file at its own open (ENOTDIR),
    naming it; without it the file would be opened and only a later
    component's open would fail."""
    world.run("r1")
    world.ledger.unlink()
    world.ledger.parent.rmdir()
    if component == ".curamoir":
        (world.repo / ".curamoir").rmdir()
    (world.repo / component).write_text('{"run_id": "a-file"}\n', encoding="utf-8")
    monkeypatch.setattr(state_branch, "_check_component", lambda *args: True)

    pushed = state_branch.push_ledger(world.repo, BRANCH)

    assert pushed.outcome == state_branch.REFUSED, pushed.sentence
    assert f"'{component}' changed into a symbolic link or another kind" in pushed.sentence
    assert world.origin_sha(BRANCH) is None


def test_a_fifo_swapped_in_after_the_check_is_refused_by_fstat_without_blocking(
        world, monkeypatch):
    """The open does not wait for a writer (O_NONBLOCK), and fstat on the
    descriptor refuses what is not a regular file. The read runs in a
    thread: were the open to block, the test opens the FIFO for writing to
    release it and fails, rather than hang the suite."""
    world.ledger.unlink()
    os.mkfifo(world.ledger)
    monkeypatch.setattr(state_branch, "_check_component", lambda *args: True)
    outcome = []

    def read():
        try:
            outcome.append(state_branch._work_tree_ledger(str(world.repo)))
        except state_branch._Refused as refused:
            outcome.append(refused)

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    reader.join(timeout=10)
    blocked = reader.is_alive()
    if blocked:
        writer = os.open(world.ledger, os.O_WRONLY | os.O_NONBLOCK)
        try:
            reader.join(timeout=10)
        finally:
            os.close(writer)

    assert not blocked, "opening the FIFO blocked waiting for a writer"
    [refused] = outcome
    assert isinstance(refused, state_branch._Refused)
    assert f"'{LEDGER}' is not a regular file" in str(refused)


def test_a_ledger_larger_than_the_bound_is_refused_not_read(world, monkeypatch):
    """A planted huge file must not exhaust the runner's memory."""
    world.run("r1")
    monkeypatch.setattr(state_branch, "MAX_LEDGER_BYTES", world.ledger.stat().st_size - 1)

    pushed = state_branch.push_ledger(world.repo, BRANCH)

    assert pushed.outcome == state_branch.REFUSED, pushed.sentence
    assert "is larger than" in pushed.sentence
    assert world.origin_sha(BRANCH) is None


def test_a_ledger_larger_than_the_bound_is_refused_before_any_byte_is_read(world,
                                                                          monkeypatch):
    """fstat's size refuses it up front: not one os.read on it."""
    world.run("r1")
    monkeypatch.setattr(state_branch, "MAX_LEDGER_BYTES", world.ledger.stat().st_size - 1)
    reads = []
    real_read = os.read
    monkeypatch.setattr(os, "read", lambda fd, n: reads.append(fd) or real_read(fd, n))

    with pytest.raises(state_branch._Refused, match="is larger than"):
        state_branch._work_tree_ledger(str(world.repo))

    assert reads == []


def _understated_size(monkeypatch):
    """os.fstat reporting a regular file's size as 0: a ledger that grows
    after fstat, as far as the size check can tell."""
    real_fstat = os.fstat

    def fstat(fd):
        status = real_fstat(fd)
        if not stat.S_ISREG(status.st_mode):
            return status
        fields = list(status)
        fields[stat.ST_SIZE] = 0
        return os.stat_result(fields)

    monkeypatch.setattr(os, "fstat", fstat)


def test_a_ledger_that_grows_past_the_bound_after_fstat_stops_being_read(world,
                                                                        monkeypatch):
    """Each read asks for no more than the bound leaves, so a ledger that
    grows past it is refused having read at most MAX_LEDGER_BYTES + 1
    bytes, never a whole chunk beyond."""
    world.run("r1")
    assert world.ledger.stat().st_size > 17  # more than the bound plus one to read
    monkeypatch.setattr(state_branch, "MAX_LEDGER_BYTES", 16)
    _understated_size(monkeypatch)
    real_read, taken = os.read, []

    def counting_read(descriptor, size):
        chunk = real_read(descriptor, size)
        taken.append(len(chunk))
        return chunk

    monkeypatch.setattr(os, "read", counting_read)

    with pytest.raises(state_branch._Refused, match="is larger than"):
        state_branch._work_tree_ledger(str(world.repo))
    assert taken and sum(taken) <= 16 + 1  # the spy saw the reads


def test_a_ledger_of_exactly_the_bound_is_read(world, monkeypatch):
    world.run("r1")
    monkeypatch.setattr(state_branch, "MAX_LEDGER_BYTES", world.ledger.stat().st_size)

    pushed = state_branch.push_ledger(world.repo, BRANCH)

    assert pushed.outcome == state_branch.PUSHED, pushed.sentence
    assert world.run_ids() == ["r1"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 file")
def test_a_ledger_that_cannot_be_read_fails_with_exit_1(world):
    world.run("r1")
    world.ledger.chmod(0)
    try:
        pushed = state_branch.push_ledger(world.repo, BRANCH)
    finally:
        world.ledger.chmod(0o644)

    assert pushed.outcome == state_branch.FAILED, pushed.sentence
    assert pushed.exit_code == 1
    assert "could not be read" in pushed.sentence
    assert world.origin_sha(BRANCH) is None


def test_the_bytes_pushed_are_the_ones_the_walk_read(world, tmp_path, monkeypatch):
    """What was checked is what is read: the push takes the walk's own
    bytes. A ledger swapped for a link to a decoy right after the walk
    returns changes nothing on the branch."""
    world.run("r1")
    decoy = _decoy_curamoir(world, tmp_path).joinpath(*LEDGER_PATH[1:])
    real_walk = state_branch._work_tree_ledger

    def walk_then_swap(repo):
        written = real_walk(repo)
        world.ledger.unlink()
        world.ledger.symlink_to(decoy)
        return written

    monkeypatch.setattr(state_branch, "_work_tree_ledger", walk_then_swap)

    pushed = state_branch.push_ledger(world.repo, BRANCH)

    assert pushed.outcome == state_branch.PUSHED, pushed.sentence
    assert world.run_ids() == ["r1"]  # never "from-the-link"


def test_a_work_tree_root_that_cannot_be_opened_is_a_failure(tmp_path):
    """The walk's root open failing (here, the root is a file) is _Failed,
    never nothing to push. push_ledger checks the checkout first, so this
    is the walk's own contract."""
    not_a_directory = tmp_path / "file"
    not_a_directory.write_text("x", encoding="utf-8")

    with pytest.raises(state_branch._Failed, match="could not be opened"):
        state_branch._work_tree_ledger(str(not_a_directory))


def test_a_read_error_is_a_failure_with_exit_1_as_root_too(world, monkeypatch):
    """The OSError-to-FAILED mapping, independent of file modes (root reads
    a mode-000 file, so the test above is skipped there): a read error in
    the walk is _Failed, and push_ledger makes _Failed FAILED, exit 1."""
    world.run("r1")

    def failing_read(descriptor, size):
        raise OSError(errno.EIO, "Input/output error")

    with monkeypatch.context() as patched:
        patched.setattr(os, "read", failing_read)
        with pytest.raises(state_branch._Failed, match="could not be read"):
            state_branch._work_tree_ledger(str(world.repo))

    def failed(repo):
        raise state_branch._Failed("the work tree's ledger could not be read (EIO)")

    monkeypatch.setattr(state_branch, "_work_tree_ledger", failed)
    pushed = state_branch.push_ledger(world.repo, BRANCH)

    assert pushed.outcome == state_branch.FAILED and pushed.exit_code == 1, pushed.sentence
    assert world.origin_sha(BRANCH) is None

def test_reading_the_ledger_leaves_no_descriptor_open(world, tmp_path):
    """Every directory and file descriptor the walk opens is closed, on
    the read and on each refusal."""
    def open_fds():
        return len(os.listdir("/dev/fd"))

    world.run("r1")
    before = open_fds()
    for _ in range(3):
        assert state_branch._work_tree_ledger(str(world.repo)) is not None
    target = _decoy_curamoir(world, tmp_path) / "state"
    state = world.ledger.parent
    state.rename(tmp_path / "real-state")
    state.symlink_to(target, target_is_directory=True)
    for _ in range(3):
        with pytest.raises(state_branch._Refused):
            state_branch._work_tree_ledger(str(world.repo))

    assert open_fds() == before


def test_the_fstat_and_size_refusals_leave_no_descriptor_open(world, monkeypatch):
    def open_fds():
        return len(os.listdir("/dev/fd"))

    world.run("r1")
    size = world.ledger.stat().st_size
    before = open_fds()
    with monkeypatch.context() as patched:
        patched.setattr(state_branch, "MAX_LEDGER_BYTES", size - 1)
        for _ in range(3):
            with pytest.raises(state_branch._Refused, match="is larger than"):
                state_branch._work_tree_ledger(str(world.repo))
    with monkeypatch.context() as patched:
        patched.setattr(state_branch, "MAX_LEDGER_BYTES", 16)
        _understated_size(patched)
        for _ in range(3):
            with pytest.raises(state_branch._Refused, match="is larger than"):
                state_branch._work_tree_ledger(str(world.repo))
    world.ledger.unlink()
    os.mkfifo(world.ledger)
    with monkeypatch.context() as patched:
        patched.setattr(state_branch, "_check_component", lambda *args: True)
        for _ in range(3):
            with pytest.raises(state_branch._Refused, match="not a regular file"):
                state_branch._work_tree_ledger(str(world.repo))

    assert open_fds() == before


def test_a_checkout_with_no_identity_is_refused(world):
    world.run("r1")
    git(world.repo, "config", "--unset", "user.email")

    pushed = state_branch.push_ledger(world.repo, BRANCH)

    assert pushed.outcome == state_branch.REFUSED
    assert "user.email" in pushed.sentence
    assert world.origin_sha(BRANCH) is None


def test_a_directory_that_is_not_a_checkout_is_refused(tmp_path):
    pushed = state_branch.push_ledger(tmp_path, BRANCH)

    assert pushed.outcome == state_branch.REFUSED


# --- the command -----------------------------------------------------------------------

def test_the_cli_exit_codes_tell_the_outcomes_apart(world, capsys):
    world.run("r1")

    assert main(["state", "push", "--repo", str(world.repo), "--branch", BRANCH]) == 0
    assert "pushed 1 ledger line" in capsys.readouterr().out
    assert main(["state", "push", "--repo", str(world.repo), "--branch", BRANCH]) == 5
    assert main(["state", "push", "--repo", str(world.repo), "--branch", "main"]) == 3
    assert "refused" in capsys.readouterr().out


def test_the_default_branch_name_is_curamoir_state(world):
    world.run("r1")

    assert main(["state", "push", "--repo", str(world.repo)]) == 0
    assert world.origin_sha(state_branch.DEFAULT_BRANCH) is not None
    assert state_branch.DEFAULT_BRANCH == "curamoir/state"


# --- reading the committed ledger (correctness review) ---------------------------------

def test_a_crlf_work_tree_pushes_only_the_runs_line_in_lf(world):
    """With `* text eol=crlf` the work tree's ledger is CRLF and HEAD's blob
    LF; compared as written, the whole committed ledger read as the run's."""
    (world.repo / ".gitattributes").write_text("* text eol=crlf\n", encoding="utf-8")
    git(world.repo, "add", ".gitattributes")
    git(world.repo, "commit", "--quiet", "-m", "crlf")
    world.ledger.unlink()
    git(world.repo, "checkout", "--quiet", "--", LEDGER)
    assert world.ledger.read_bytes().endswith(b"\r\n")
    world.run("r1")

    pushed = state_branch.push_ledger(world.repo, BRANCH)

    assert pushed.outcome == state_branch.PUSHED, pushed.sentence
    assert world.run_ids() == ["r1"]
    assert b"\r" not in subprocess.run(["git", "show", f"{BRANCH}:{LEDGER}"],
                                       cwd=world.origin, capture_output=True).stdout


def test_an_unborn_head_counts_every_work_tree_line_as_the_runs(tmp_path):
    repo = tmp_path / "unborn"
    git(tmp_path, "init", "--quiet", str(repo))
    ledger = repo_ledger(repo)
    ledger.record("sweep", None, "first", run_id="a")
    ledger.record("sweep", None, "second", run_id="b")

    lines, skipped = state_branch._run_lines(str(repo))

    assert [json.loads(line)["run_id"] for line in lines] == ["a", "b"]
    assert skipped == 0


def test_a_head_whose_tree_cannot_be_read_fails_and_pushes_nothing(world):
    """Not "no committed ledger": that would push the whole ledger as the run's."""
    world.run("r1")
    tree = git(world.repo, "rev-parse", "HEAD^{tree}").strip()
    (world.repo / ".git" / "objects" / tree[:2] / tree[2:]).unlink()

    pushed = state_branch.push_ledger(world.repo, BRANCH)

    assert pushed.outcome == state_branch.FAILED, pushed.sentence
    assert world.origin_sha(BRANCH) is None


# --- edge cases review: what is pushed --------------------------------------------------

TOKEN = "ghp_0123456789abcdefghijklmnopqrstuvwxyz"


def test_a_secret_in_a_runs_line_is_redacted_before_it_is_pushed(world):
    """The state branch may be public; a job line can carry an exception
    message, and the message a token."""
    repo_ledger(world.repo).record("sweep", None, f"gh failed: {TOKEN}",
                                   run_id="r1")

    pushed = state_branch.push_ledger(world.repo, BRANCH)
    again = state_branch.push_ledger(world.repo, BRANCH)

    assert pushed.outcome == state_branch.PUSHED, pushed.sentence
    text = git(world.origin, "show", f"{BRANCH}:{LEDGER}")
    assert TOKEN not in text and "[REDACTED]" in text
    [entry] = world.state_lines()
    assert entry["run_id"] == "r1"
    # Compared in the redacted form, so the retry pushes nothing.
    assert again.outcome == state_branch.NOTHING_TO_PUSH, again.sentence
    assert world.origin_sha(BRANCH) == pushed.sha


def test_a_line_that_redaction_leaves_alone_is_pushed_byte_for_byte(world):
    world.run("r1")
    written = world.ledger.read_bytes().splitlines()[-1]

    state_branch.push_ledger(world.repo, BRANCH)

    assert subprocess.run(["git", "show", f"{BRANCH}:{LEDGER}"], cwd=world.origin,
                          capture_output=True).stdout == written + b"\n"


@pytest.mark.parametrize("junk", [
    b'{"run_id": "nul"}\x00',
    b"\xff\xfe not utf-8",
    b'{"run_id": "bad \xff"}',
    b"[1, 2]",
    b'"a string"',
    b"42",
    b"not json at all",
    b'{"run_id": "big", "detail": "' + b"a" * (64 * 1024) + b'"}',
], ids=["nul", "invalid-utf8", "invalid-utf8-in-object", "array", "string", "number",
        "text", "over-64-KiB"])
def test_a_line_that_is_not_a_json_object_of_at_most_64_kib_is_skipped(world, junk):
    world.run("r1")
    with world.ledger.open("ab") as handle:
        handle.write(junk + b"\n")

    pushed = state_branch.push_ledger(world.repo, BRANCH)

    assert pushed.outcome == state_branch.PUSHED, pushed.sentence
    assert (pushed.lines, pushed.skipped) == (1, 1)
    assert "skipped 1 line" in pushed.sentence
    assert world.run_ids() == ["r1"]


def test_a_line_of_exactly_64_kib_is_pushed(world):
    head = b'{"run_id": "edge", "detail": "'
    tail = b'"}'
    line = head + b"a" * (64 * 1024 - len(head) - len(tail)) + tail
    assert len(line) == 64 * 1024
    with world.ledger.open("ab") as handle:
        handle.write(line + b"\n")

    pushed = state_branch.push_ledger(world.repo, BRANCH)

    assert (pushed.outcome, pushed.lines, pushed.skipped) == (state_branch.PUSHED, 1, 0)


def test_a_run_whose_only_lines_are_junk_has_nothing_to_push_and_says_so(world):
    with world.ledger.open("ab") as handle:
        handle.write(b"[]\nnot json\n")

    pushed = state_branch.push_ledger(world.repo, BRANCH)

    assert pushed.outcome == state_branch.NOTHING_TO_PUSH, pushed.sentence
    assert pushed.skipped == 2 and "skipped 2 lines" in pushed.sentence
    assert world.origin_sha(BRANCH) is None


# --- edge cases review: when the push itself fails --------------------------------------

def _pre_receive(world, script):
    hook = world.origin / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\n" + script, encoding="utf-8")
    hook.chmod(0o755)


def test_a_push_origin_rejects_with_a_hook_fails_once_and_is_not_retried(world, capsys):
    """A pre-receive hook (a ruleset, a server policy) refusing is not "the
    branch moved": retrying cannot help, so it fails at once."""
    calls = world.origin.parent / "hook-calls"
    _pre_receive(world, f'echo called >> "{calls}"\necho "state pushes are closed" >&2\n'
                        "exit 1\n")
    world.run("r1")

    code = main(["state", "push", "--repo", str(world.repo), "--branch", BRANCH])

    out = capsys.readouterr().out
    assert code == 1, out
    assert "could not push the ledger" in out and "state pushes are closed" in out
    assert calls.read_text(encoding="utf-8").splitlines() == ["called"]
    assert world.origin_sha(BRANCH) is None


def test_a_push_that_does_not_finish_in_time_fails_with_a_sentence(world, monkeypatch):
    _pre_receive(world, "sleep 30\n")
    monkeypatch.setattr(state_branch, "PUSH_TIMEOUT", 1)
    world.run("r1")

    pushed = state_branch.push_ledger(world.repo, BRANCH)

    assert pushed.outcome == state_branch.FAILED
    assert "did not finish within 1 second(s)" in pushed.sentence
    assert pushed.exit_code == 1


# --- provenance review: what is pushed is only what redaction saw ------------------------

def _append(world, *raw):
    with world.ledger.open("ab") as handle:
        for line in raw:
            handle.write(line + b"\n")


@pytest.mark.parametrize("line", [
    b'{"run_id": "dup", "detail": "' + TOKEN.encode() + b'", "detail": "fine"}',
    b'{"run_id": "dup", "nested": {"m": "' + TOKEN.encode() + b'", "m": "x"}}',
], ids=["top-level", "nested"])
def test_a_line_with_a_repeated_key_is_skipped_so_a_hidden_secret_is_never_pushed(world,
                                                                                    line):
    """The parser keeps the last value, so redaction would see "fine" while
    the raw bytes still held the token."""
    world.run("r1")
    _append(world, line)

    pushed = state_branch.push_ledger(world.repo, BRANCH)

    assert (pushed.outcome, pushed.lines, pushed.skipped) == (state_branch.PUSHED, 1, 1)
    assert TOKEN not in git(world.origin, "show", f"{BRANCH}:{LEDGER}")
    assert world.run_ids() == ["r1"]


def test_lines_are_pushed_in_the_ledgers_own_canonical_form(world):
    _append(world, b'{"run_id":"r9",  "a":1}')

    pushed = state_branch.push_ledger(world.repo, BRANCH)

    assert pushed.outcome == state_branch.PUSHED, pushed.sentence
    stored = git(world.origin, "show", f"{BRANCH}:{LEDGER}")
    assert stored == json.dumps({"run_id": "r9", "a": 1}, sort_keys=True, default=str) + "\n"


def test_a_line_the_ledger_wrote_is_pushed_as_its_own_bytes(world):
    """ledger.record's serialisation is the canonical form."""
    world.run("r1")
    written = world.ledger.read_bytes().splitlines()[-1]

    assert state_branch._pushable(written) == written


def test_the_branch_is_compared_in_the_canonical_form_too(world):
    """A branch line written in another spelling of the same object, or
    before redaction, still counts as already there."""
    world.run("seed")
    state_branch.push_ledger(world.repo, BRANCH)
    _commit_on_origin(world, BRANCH, '{"run_id":"r9","a":1}\n'
                                     f'{{"run_id": "r10", "detail": "{TOKEN}"}}\n')
    git(world.repo, "checkout", "--quiet", "--", LEDGER)
    _append(world, b'{"a": 1, "run_id": "r9"}',
            f'{{"detail": "{TOKEN}", "run_id": "r10"}}'.encode())
    before = world.origin_sha(BRANCH)

    pushed = state_branch.push_ledger(world.repo, BRANCH)

    assert pushed.outcome == state_branch.NOTHING_TO_PUSH, pushed.sentence
    assert world.origin_sha(BRANCH) == before


def test_nesting_too_deep_to_walk_is_skipped_not_a_crash(world):
    world.run("r1")
    _append(world, b'{"run_id": "deep", "x": ' + b"[" * 2000 + b"]" * 2000 + b"}",
            b'{"run_id": "deep2", "x": ' + b'{"a": ' * 2000 + b"1" + b"}" * 2000 + b"}")

    pushed = state_branch.push_ledger(world.repo, BRANCH)

    assert (pushed.outcome, pushed.lines, pushed.skipped) == (state_branch.PUSHED, 1, 2)


def test_a_line_one_byte_over_the_limit_is_skipped(world):
    head, tail = b'{"run_id": "edge", "detail": "', b'"}'
    line = head + b"a" * (state_branch.MAX_LINE_BYTES + 1 - len(head) - len(tail)) + tail
    assert len(line) == state_branch.MAX_LINE_BYTES + 1
    _append(world, line)

    pushed = state_branch.push_ledger(world.repo, BRANCH)

    assert (pushed.outcome, pushed.skipped) == (state_branch.NOTHING_TO_PUSH, 1)
    assert world.origin_sha(BRANCH) is None


def test_a_line_written_twice_in_one_run_is_pushed_once(world):
    world.run("r1")
    _append(world, world.ledger.read_bytes().splitlines()[-1])

    pushed = state_branch.push_ledger(world.repo, BRANCH)

    assert pushed.lines == 1
    assert world.run_ids() == ["r1"]


def test_a_work_tree_with_no_ledger_has_nothing_to_push(world):
    world.ledger.unlink()

    pushed = state_branch.push_ledger(world.repo, BRANCH)

    assert pushed.outcome == state_branch.NOTHING_TO_PUSH, pushed.sentence
    assert world.origin_sha(BRANCH) is None


# --- provenance review: the target and the layout -------------------------------------

def _refs(world):
    return git(world.origin, "for-each-ref", "--format=%(refname)").split()


def test_origin_with_a_detached_head_is_refused_fail_closed(world):
    """origin's default cannot be learned, so "not the default" cannot be shown."""
    (world.origin / "HEAD").write_text(world.main + "\n", encoding="utf-8")
    world.run("r1")

    pushed = state_branch.push_ledger(world.repo, BRANCH)

    assert pushed.outcome == state_branch.REFUSED, pushed.sentence
    assert "did not say which branch is its default" in pushed.sentence
    assert _refs(world) == ["refs/heads/main"]


def test_origin_whose_head_points_outside_refs_heads_is_refused(world):
    git(world.origin, "tag", "v1", "main")
    git(world.origin, "symbolic-ref", "HEAD", "refs/tags/v1")
    world.run("r1")

    pushed = state_branch.push_ledger(world.repo, BRANCH)

    assert pushed.outcome == state_branch.REFUSED, pushed.sentence
    assert "refs/heads/curamoir/state" not in _refs(world)


def test_the_checkouts_own_default_refuses_even_when_origins_head_differs(world):
    """origin's live HEAD is main, but the checkout's refs/remotes/origin/HEAD
    names the state branch: either view naming it is enough to refuse."""
    git(world.repo, "update-ref", f"refs/remotes/origin/{BRANCH}", "HEAD")
    git(world.repo, "symbolic-ref", "refs/remotes/origin/HEAD",
        f"refs/remotes/origin/{BRANCH}")
    world.run("r1")

    pushed = state_branch.push_ledger(world.repo, BRANCH)

    assert pushed.outcome == state_branch.REFUSED, pushed.sentence
    assert "the checkout says so" in pushed.sentence
    assert _refs(world) == ["refs/heads/main"]


def _orphan_on_origin(world, files, symlinks=()):
    """An orphan BRANCH on origin holding `files` (path: text) and
    `symlinks` (path: target): its SHA."""
    scratch = world.repo.parent / "orphan"
    git(world.repo.parent, "init", "--quiet", "--initial-branch=x", str(scratch))
    for name, text in files.items():
        path = scratch / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    for name, target in dict(symlinks).items():
        path = scratch / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(target)
    env = {**os.environ, "GIT_AUTHOR_NAME": "Them", "GIT_AUTHOR_EMAIL": "t@x",
           "GIT_COMMITTER_NAME": "Them", "GIT_COMMITTER_EMAIL": "t@x"}
    git(scratch, "add", "-A")
    git(scratch, "commit", "--quiet", "-m", "theirs", env=env)
    git(scratch, "push", "--quiet", str(world.origin), f"HEAD:refs/heads/{BRANCH}")
    return git(scratch, "rev-parse", "HEAD").strip()


def test_a_one_file_branch_at_another_path_is_not_a_state_branch(world):
    theirs = _orphan_on_origin(world, {"notes/ledger.jsonl": COMMITTED + "\n"})
    world.run("r1")

    pushed = state_branch.push_ledger(world.repo, BRANCH)

    assert pushed.outcome == state_branch.REFUSED, pushed.sentence
    assert "not a state branch" in pushed.sentence
    assert world.origin_sha(BRANCH) == theirs


def test_a_branch_whose_only_ledger_is_a_symlink_is_not_a_state_branch(world):
    theirs = _orphan_on_origin(world, {}, symlinks={LEDGER: "/etc/passwd"})
    world.run("r1")

    pushed = state_branch.push_ledger(world.repo, BRANCH)

    assert pushed.outcome == state_branch.REFUSED, pushed.sentence
    assert "not a state branch" in pushed.sentence
    assert world.origin_sha(BRANCH) == theirs


def test_a_push_that_exits_0_without_reporting_the_ref_fails(world, monkeypatch):
    """Exit 0 alone is not "it landed": origin must report the ref updated
    in the porcelain output, or the outcome is FAILED."""
    real = state_branch._user_git

    def silent_push(repo, args, timeout):
        if args[0] == "push":
            return subprocess.CompletedProcess(args, 0, b"Done\n", b"")
        return real(repo, args, timeout)

    monkeypatch.setattr(state_branch, "_user_git", silent_push)
    world.run("r1")

    pushed = state_branch.push_ledger(world.repo, BRANCH)

    assert pushed.outcome == state_branch.FAILED, pushed.sentence
    assert "did not report" in pushed.sentence


def test_the_exit_codes_are_one_scheme_with_curamoir_runs():
    """0, 1 and 3 mean what they mean for `curamoir run`; 5 is state push's
    own, and run does not use it."""
    from curamoir import runner

    assert state_branch.EXIT_CODES == {
        state_branch.PUSHED: runner.EXIT_SUCCESS, state_branch.FAILED: runner.EXIT_FAILURE,
        state_branch.REFUSED: runner.EXIT_REFUSED,
        state_branch.NOTHING_TO_PUSH: state_branch.EXIT_NOTHING_TO_PUSH}
    run_codes = {value for name, value in vars(runner).items() if name.startswith("EXIT_")}
    assert state_branch.EXIT_NOTHING_TO_PUSH not in run_codes
