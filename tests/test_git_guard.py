"""The git-safety layer under the sweep's review step (cm-fkd.3, E3-R2).

Three subjects: the hardened `git()` every git command here runs through;
the sandbox, the disposable clone of the pinned base commit that the fix
step runs in instead of the user's checkout (the decision recorded on
cm-fkd.3); and the snapshot that holds a sandbox's `.git` and work tree to
what they were before the fix step.

Every test runs real git on repositories made in `tmp_path`; the planted
programs (fsmonitor, filter drivers, a slow `git`) write a marker file, and
the assertion is that the marker never appears.
"""

import os
import re
import stat
import subprocess
import sys
import time
import unicodedata

import pytest

from curamoir import git_guard
from curamoir.git_guard import (
    GitGuardError,
    git,
    prepare_sandbox,
    resolve_base,
    snapshot_git_state,
    tree_changes,
    verify_git_state,
)


def run(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
                          text=True).stdout


def git_repo(tmp_path, files, name="plain"):
    """A one-commit repository holding `files` (path -> text or bytes)."""
    repo = tmp_path / name
    for path, content in files.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        data = content if isinstance(content, bytes) else content.encode("utf-8")
        (repo / path).write_bytes(data)
    for command in (["init", "--initial-branch=main"],
                    ["config", "user.email", "test@example.invalid"],
                    ["config", "user.name", "test"], ["add", "-A"], ["commit", "-m", "base"]):
        subprocess.run(["git", *command], cwd=repo, check=True, capture_output=True)
    return repo


def planted(tmp_path, name="ran"):
    """A program that touches a marker: (program, marker)."""
    marker = tmp_path / name
    program = tmp_path / f"{name}.sh"
    program.write_text(f"#!/bin/sh\ntouch {marker}\ncat\n", encoding="utf-8")
    program.chmod(0o755)
    return program, marker


@pytest.fixture
def sandboxes(tmp_path):
    """Where this test's sandboxes are made, so a test can see that none is left."""
    parent = tmp_path / "sandboxes"
    parent.mkdir()
    return parent


# --- git hardening ---------------------------------------------------------------------

def test_git_runs_without_the_repos_fsmonitor_and_hooks(tmp_path):
    """core.fsmonitor names a program git runs to refresh the index, which
    `git diff` against the working tree does."""
    repo = git_repo(tmp_path, {"m.py": "x = 1\n"})
    program, marker = planted(tmp_path)
    run(repo, "config", "core.fsmonitor", str(program))
    (repo / "m.py").write_text("x = 2\n", encoding="utf-8")

    git(repo, "diff", "HEAD")
    git(repo, "status")

    assert not marker.exists()


def test_git_drops_inherited_git_environment(tmp_path, monkeypatch):
    """A GIT_DIR, GIT_WORK_TREE or GIT_CONFIG_PARAMETERS in the environment
    the runner was started from (a git hook, a CI step) would point every
    command here at another repository or plant config; git() drops them."""
    repo = git_repo(tmp_path, {"m.py": "x = 1\n"})
    other = git_repo(tmp_path, {"n.py": "y = 1\n"}, name="other")
    program, marker = planted(tmp_path)
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", f"'core.fsmonitor'='{program}'")
    (repo / "m.py").write_text("x = 2\n", encoding="utf-8")

    top = git(repo, "rev-parse", "--show-toplevel").decode().strip()
    git(repo, "diff", "HEAD")

    assert os.path.realpath(top) == os.path.realpath(repo)
    assert not marker.exists()


def test_git_reads_no_global_or_system_config(tmp_path, monkeypatch):
    """The user's ~/.gitconfig can define filters, fsmonitor and includes too;
    nothing here needs it, so it is not read."""
    repo = git_repo(tmp_path, {"m.py": "x = 1\n"})
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text("[sweep]\n\tmarker = global\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    assert "global" in run(repo, "config", "--get", "sweep.marker")

    listing = git(repo, "config", "--list", "--show-origin").decode()

    assert "sweep.marker" not in listing


def test_git_never_descends_into_a_submodule(tmp_path):
    """diff.ignoreSubmodules=all is part of the hardening: a nested repository
    is never opened by a git command run here."""
    repo = git_repo(tmp_path, {"m.py": "x = 1\n"})

    listing = git(repo, "config", "--get", "diff.ignoreSubmodules").decode().strip()

    assert listing == "all"


def test_git_names_a_failure(tmp_path):
    repo = git_repo(tmp_path, {"m.py": "x = 1\n"})

    with pytest.raises(GitGuardError, match="git cat-file"):
        git(repo, "cat-file", "blob", "HEAD:absent.py")


def slow_git(tmp_path):
    """A stand-in `git` that starts a grandchild which writes a marker after
    two seconds, then sleeps itself: a hung git with a helper process."""
    marker = tmp_path / "late"
    script = tmp_path / "slow-git"
    script.write_text(
        f"#!{sys.executable}\n"
        "import subprocess, sys, time\n"
        f"subprocess.Popen(['/bin/sh', '-c', 'sleep 2; touch {marker}'])\n"
        "time.sleep(30)\n",
        encoding="utf-8")
    script.chmod(0o755)
    return script, marker


def test_git_is_bounded_and_its_whole_process_group_is_killed(tmp_path, monkeypatch):
    script, marker = slow_git(tmp_path)
    monkeypatch.setattr(git_guard, "GIT", str(script))
    started = time.monotonic()

    with pytest.raises(GitGuardError, match="did not finish within 1 second"):
        git(tmp_path, "status", timeout=1)
    time.sleep(3)

    assert time.monotonic() - started < 15
    assert not marker.exists(), "the grandchild outlived the timeout"


def test_resolve_base_refuses_an_option_and_a_non_commit(tmp_path):
    """`--output=<file>` as a revision made `git diff` write a file."""
    repo = git_repo(tmp_path, {"m.py": "x = 1\n"})
    target = tmp_path / "zz"

    with pytest.raises(GitGuardError, match="looks like an option"):
        resolve_base(repo, f"--output={target}")
    with pytest.raises(GitGuardError, match="does not name a commit"):
        resolve_base(repo, "no-such-branch")
    with pytest.raises(GitGuardError, match="does not name a commit"):
        resolve_base(repo, "HEAD:m.py")
    assert not target.exists()
    assert re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", resolve_base(repo, "main"))


# --- the sandbox -----------------------------------------------------------------------

def test_the_sandbox_is_a_fresh_clone_of_the_base_with_none_of_the_owners_git_setup(
        tmp_path, sandboxes):
    """The owner's hooks, fsmonitor, include.path and filter drivers live in
    the source's .git, which a clone does not copy; the empty template keeps
    git's own sample hooks and info/exclude out too."""
    repo = git_repo(tmp_path, {"m.py": "x = 1\n", ".gitattributes": "*.py filter=owner\n"})
    program, marker = planted(tmp_path)
    run(repo, "config", "core.fsmonitor", str(program))
    run(repo, "config", "include.path", "../owner.gitconfig")
    (repo / "owner.gitconfig").write_text(f"[filter \"owner\"]\n\tsmudge = {program}\n"
                                          f"\tclean = {program}\n", encoding="utf-8")
    (repo / ".git" / "hooks" / "post-checkout").write_text(f"#!/bin/sh\n{program}\n")
    (repo / ".git" / "hooks" / "post-checkout").chmod(0o755)
    base = run(repo, "rev-parse", "HEAD").strip()

    with prepare_sandbox(repo, "HEAD", parent=sandboxes) as sandbox:
        assert sandbox.base == base and sandbox.source == str(repo)
        assert os.path.realpath(sandbox.path) != os.path.realpath(repo)
        dot = sandbox.path / ".git"
        assert not (dot / "hooks").exists() and not (dot / "info").exists()
        config = (dot / "config").read_text(encoding="utf-8")
        for absent in ("include", "fsmonitor", "filter", "hooksPath"):
            assert absent not in config
        assert run(sandbox.path, "rev-parse", "HEAD").strip() == base
        assert run(sandbox.path, "status", "--porcelain").strip() == ""
        assert (sandbox.path / "m.py").read_text(encoding="utf-8") == "x = 1\n"
    assert not marker.exists()


def test_uncommitted_and_ignored_files_in_the_source_stay_out_of_the_sandbox(
        tmp_path, sandboxes):
    """The sandbox is the committed base and nothing else: a venv, a local
    edit or an untracked file in the user's checkout is not the base, and the
    lint, the agent and the review never see it."""
    repo = git_repo(tmp_path, {"m.py": "x = 1\n", ".gitignore": ".venv/\n"})
    (repo / ".venv" / "lib").mkdir(parents=True)
    (repo / ".venv" / "lib" / "requests.py").write_text("y = 1\n", encoding="utf-8")
    (repo / "m.py").write_text("x = 'local edit'\n", encoding="utf-8")
    (repo / "scratch.py").write_text("z = 1\n", encoding="utf-8")

    with prepare_sandbox(repo, "HEAD", parent=sandboxes) as sandbox:
        assert sorted(p.name for p in sandbox.path.iterdir()) == [".git", ".gitignore", "m.py"]
        assert (sandbox.path / "m.py").read_text(encoding="utf-8") == "x = 1\n"


def test_the_sandbox_is_removed_on_exit_on_error_and_by_cleanup(tmp_path, sandboxes):
    repo = git_repo(tmp_path, {"m.py": "x = 1\n"})

    with prepare_sandbox(repo, "HEAD", parent=sandboxes) as sandbox:
        assert sandbox.path.is_dir()
    assert list(sandboxes.iterdir()) == []

    with pytest.raises(RuntimeError), prepare_sandbox(repo, "HEAD", parent=sandboxes):
        raise RuntimeError("the runner failed")
    assert list(sandboxes.iterdir()) == []

    sandbox = prepare_sandbox(repo, "HEAD", parent=sandboxes)
    sandbox.cleanup()
    sandbox.cleanup()
    assert list(sandboxes.iterdir()) == []


@pytest.mark.parametrize("base", ["--upload-pack=touch x", "no-such-branch"])
def test_a_bad_base_makes_no_sandbox(tmp_path, sandboxes, base):
    repo = git_repo(tmp_path, {"m.py": "x = 1\n"})

    with pytest.raises(GitGuardError):
        prepare_sandbox(repo, base, parent=sandboxes)
    assert list(sandboxes.iterdir()) == []


def test_a_source_that_is_not_a_repository_top_makes_no_sandbox(tmp_path, sandboxes):
    repo = git_repo(tmp_path, {"sub/m.py": "x = 1\n"})
    plain = tmp_path / "not-a-repo"
    plain.mkdir()

    for source in (repo / "sub", plain):
        with pytest.raises(GitGuardError):
            prepare_sandbox(source, "HEAD", parent=sandboxes)
    assert list(sandboxes.iterdir()) == []


def test_a_linked_worktree_is_a_source_like_any_other(tmp_path, sandboxes):
    repo = git_repo(tmp_path, {"m.py": "x = 1\n"})
    (repo / "m.py").write_text("x = 2\n", encoding="utf-8")
    run(repo, "commit", "-qam", "two")
    worktree = tmp_path / "wt"
    run(repo, "worktree", "add", "--detach", str(worktree), "HEAD~1")
    assert (worktree / ".git").is_file()

    with prepare_sandbox(worktree, "HEAD", parent=sandboxes) as sandbox:
        assert sandbox.base == run(worktree, "rev-parse", "HEAD").strip()
        assert (sandbox.path / "m.py").read_text(encoding="utf-8") == "x = 1\n"
        assert (sandbox.path / ".git").is_dir()


def test_a_shallow_source_gives_a_shallow_sandbox_at_its_base(tmp_path, sandboxes):
    """Only the base tree is read (the diff, `ls-tree`, `grep` at the base),
    so a shallow clone of a shallow source is enough."""
    repo = git_repo(tmp_path, {"m.py": "x = 1\n"})
    (repo / "m.py").write_text("x = 2\n", encoding="utf-8")
    run(repo, "commit", "-qam", "two")
    shallow = tmp_path / "shallow"
    run(tmp_path, "clone", "--quiet", "--depth", "1", f"file://{repo}", str(shallow))

    with prepare_sandbox(shallow, "HEAD", parent=sandboxes) as sandbox:
        assert run(sandbox.path, "rev-parse", "--is-shallow-repository").strip() == "true"
        assert (sandbox.path / "m.py").read_text(encoding="utf-8") == "x = 2\n"


def test_a_base_that_no_branch_reaches_is_fetched_into_the_sandbox(tmp_path, sandboxes):
    """A clone copies branches and tags; a detached commit, the base of a
    sweep run from a detached checkout, is fetched by its SHA."""
    repo = git_repo(tmp_path, {"m.py": "x = 1\n"})
    run(repo, "checkout", "-q", "--detach")
    (repo / "m.py").write_text("x = 'detached'\n", encoding="utf-8")
    run(repo, "commit", "-qam", "detached")
    detached = run(repo, "rev-parse", "HEAD").strip()
    run(repo, "checkout", "-q", "main")

    with prepare_sandbox(repo, detached, parent=sandboxes) as sandbox:
        assert sandbox.base == detached
        assert (sandbox.path / "m.py").read_text(encoding="utf-8") == "x = 'detached'\n"


def test_submodules_are_not_initialised_in_the_sandbox(tmp_path, sandboxes):
    """A stated limit: a submodule is an empty directory in the sandbox, so a
    fix inside one cannot be made — and a file written there anyway is
    invisible to git, which the work-tree manifest catches (tree_changes)."""
    sub = git_repo(tmp_path, {"s.txt": "s\n"}, name="sub")
    repo = git_repo(tmp_path, {"m.py": "x = 1\n"})
    run(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub),
        "vendor/sub")
    run(repo, "commit", "-qm", "submodule")

    with prepare_sandbox(repo, "HEAD", parent=sandboxes) as sandbox:
        assert list((sandbox.path / "vendor" / "sub").iterdir()) == []
        state = snapshot_git_state(sandbox)
        (sandbox.path / "vendor" / "sub" / "new.py").write_text("x = 1\n", encoding="utf-8")

        assert git(sandbox.path, "status", "--porcelain", "--ignored").strip() == b""
        assert tree_changes(state) == ("vendor/sub/new.py",)


def test_show_reads_the_base_commit_not_the_work_tree(tmp_path, sandboxes):
    repo = git_repo(tmp_path, {".curamoir/principles.md": "base\n"})

    with prepare_sandbox(repo, "HEAD", parent=sandboxes) as sandbox:
        (sandbox.path / ".curamoir" / "principles.md").write_text("edited\n")
        (sandbox.path / "new.md").write_text("new\n")

        assert sandbox.show(".curamoir/principles.md") == b"base\n"
        assert sandbox.show("new.md") is None
        assert sandbox.show("-p") is None


def test_the_patch_carries_exactly_the_paths_asked_for_binary_safe(tmp_path, sandboxes):
    """How an accepted change leaves the sandbox: `git apply` of the patch in
    another clone of the base reproduces those paths, deletions and binary
    content included, and nothing else."""
    blob = bytes(range(256)) * 4
    repo = git_repo(tmp_path, {"a.py": "a = 1\n", "b.py": "b = 1\n", "c.bin": blob,
                               "d.py": "d = 1\n"})

    with prepare_sandbox(repo, "HEAD", parent=sandboxes) as sandbox:
        (sandbox.path / "a.py").write_text("a = 2\n", encoding="utf-8")
        (sandbox.path / "b.py").unlink()
        (sandbox.path / "c.bin").write_bytes(blob[::-1])
        (sandbox.path / "d.py").write_text("d = 'not accepted'\n", encoding="utf-8")
        before = git(sandbox.path, "status", "--porcelain")

        patch = sandbox.patch(("a.py", "b.py", "c.bin"))

        assert git(sandbox.path, "status", "--porcelain") == before
        assert sandbox.patch(()) == b""

    target = tmp_path / "target"
    run(tmp_path, "clone", "--quiet", str(repo), str(target))
    subprocess.run(["git", "apply", "--index", "-"], cwd=target, input=patch, check=True)
    assert (target / "a.py").read_text() == "a = 2\n"
    assert not (target / "b.py").exists()
    assert (target / "c.bin").read_bytes() == blob[::-1]
    assert (target / "d.py").read_text() == "d = 1\n"


#: Root ignores permission bits, so a locked directory locks nothing for it.
needs_permissions = pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                                       reason="root ignores permission bits")


@needs_permissions
@pytest.mark.parametrize("mode", [0o000, 0o555])
def test_cleanup_removes_a_clone_holding_a_directory_the_agent_locked(tmp_path, sandboxes,
                                                                      mode):
    """And never changes the mode of anything a symlink in it points at."""
    repo = git_repo(tmp_path, {"m.py": "x = 1\n"})
    outside = tmp_path / "outside"
    outside.mkdir()
    outside.chmod(0o500)
    sandbox = prepare_sandbox(repo, "HEAD", parent=sandboxes)
    (sandbox.path / "link").symlink_to(outside)
    locked = sandbox.path / "locked"
    (locked / "deeper").mkdir(parents=True)
    (locked / "deeper" / "f.py").write_text("x\n", encoding="utf-8")
    (locked / "deeper").chmod(mode)
    locked.chmod(mode)
    try:
        sandbox.cleanup()

        assert list(sandboxes.iterdir()) == []
        assert stat.S_IMODE(outside.stat().st_mode) == 0o500
    finally:
        for path in (locked, locked / "deeper", outside):
            if os.path.lexists(path):
                path.chmod(0o755)


@needs_permissions
@pytest.mark.parametrize("mode", [0o755, 0o000])
def test_cleanup_never_follows_a_directory_swapped_for_a_symlink(tmp_path, sandboxes,
                                                                 monkeypatch, mode):
    """A process left in the sandbox can swap a directory for a symlink
    between cleanup's look at it and its chmod. The swap is made to win:
    at cleanup's first stat or open of `victim`. Whatever the symlink
    names keeps its mode."""
    repo = git_repo(tmp_path, {"m.py": "x = 1\n"})
    outside = tmp_path / "outside"
    outside.mkdir()
    outside.chmod(0o500)
    sandbox = prepare_sandbox(repo, "HEAD", parent=sandboxes)
    victim = sandbox.path / "victim"
    victim.mkdir()
    victim.chmod(mode)
    real_lstat, real_open = os.lstat, os.open
    swapped = []

    def swap_first(path):
        if not swapped and os.path.basename(os.fsdecode(path)) == "victim":
            swapped.append(path)
            os.rename(str(victim), str(sandbox.path / "victim-real"))
            os.symlink(str(outside), str(victim))

    def lstat(path, *args, **kwargs):
        result = real_lstat(path, *args, **kwargs)
        swap_first(path)  # after the look: the race cleanup must not lose
        return result

    def opener(path, *args, **kwargs):
        swap_first(path)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", lstat)
    monkeypatch.setattr(os, "open", opener)
    try:
        try:
            sandbox.cleanup()
        finally:
            monkeypatch.undo()

        assert swapped
        assert stat.S_IMODE(outside.stat().st_mode) == 0o500
        assert list(sandboxes.iterdir()) == []
    finally:
        outside.chmod(0o755)
        real = sandbox.path / "victim-real"
        if os.path.lexists(real):
            real.chmod(0o755)
            sandbox.cleanup()


@needs_permissions
def test_cleanup_never_loosens_the_directory_the_sandbox_is_in(tmp_path, sandboxes):
    """The caller's directory is not the sandbox's to change: made
    read-only, it keeps the clone's root in place, and cleanup says so
    rather than chmod its way out."""
    repo = git_repo(tmp_path, {"m.py": "x = 1\n"})
    sandbox = prepare_sandbox(repo, "HEAD", parent=sandboxes)
    sandboxes.chmod(0o555)
    try:
        with pytest.raises(GitGuardError, match="could not be removed"):
            sandbox.cleanup()

        assert stat.S_IMODE(sandboxes.stat().st_mode) == 0o555
    finally:
        sandboxes.chmod(0o755)
        sandbox.cleanup()


def test_a_clone_cleanup_cannot_remove_is_an_error_that_never_masks_another(
        tmp_path, sandboxes, monkeypatch):
    repo = git_repo(tmp_path, {"m.py": "x = 1\n"})
    sandbox = prepare_sandbox(repo, "HEAD", parent=sandboxes)
    monkeypatch.setattr(git_guard.shutil, "rmtree", lambda *args, **kwargs: None)

    with pytest.raises(GitGuardError, match="could not be removed"):
        sandbox.cleanup()
    with pytest.warns(RuntimeWarning, match="could not be removed"), \
            pytest.raises(KeyError, match="the runner failed"), sandbox:
        raise KeyError("the runner failed")

    monkeypatch.undo()
    sandbox.cleanup()
    assert list(sandboxes.iterdir()) == []


@needs_permissions
def test_a_parent_that_is_missing_or_unwritable_is_a_git_guard_error(tmp_path, sandboxes):
    repo = git_repo(tmp_path, {"m.py": "x = 1\n"})

    with pytest.raises(GitGuardError, match="cannot make a sandbox"):
        prepare_sandbox(repo, "HEAD", parent=tmp_path / "missing")
    sandboxes.chmod(0o555)
    try:
        with pytest.raises(GitGuardError, match="cannot make a sandbox"):
            prepare_sandbox(repo, "HEAD", parent=sandboxes)
    finally:
        sandboxes.chmod(0o755)


def test_the_checkout_is_bounded_by_the_clone_timeout(tmp_path, sandboxes, monkeypatch):
    repo = git_repo(tmp_path, {"m.py": "x = 1\n"})
    timeouts = {}
    real = git_guard.git

    def spy(repo, *args, **kwargs):
        timeouts.setdefault(args[0], kwargs.get("timeout"))
        return real(repo, *args, **kwargs)

    monkeypatch.setattr(git_guard, "git", spy)
    prepare_sandbox(repo, "HEAD", parent=sandboxes, timeout=77).cleanup()

    assert timeouts["clone"] == timeouts["checkout"] == 77


@needs_permissions
def test_a_patch_that_cannot_carry_every_path_asked_for_is_refused(tmp_path, sandboxes):
    """git add skips a FIFO and a file it cannot read without failing; the
    patch is checked against the paths asked for, not trusted."""
    repo = git_repo(tmp_path, {"m.py": "x = 1\n"})
    with prepare_sandbox(repo, "HEAD", parent=sandboxes) as sandbox:
        (sandbox.path / "m.py").write_text("x = 2\n", encoding="utf-8")
        os.mkfifo(str(sandbox.path / "pipe"))
        with pytest.raises(GitGuardError, match="pipe"):
            sandbox.patch(("m.py", "pipe"))
        locked = sandbox.path / "locked"
        locked.mkdir()
        (locked / "f.py").write_text("x\n", encoding="utf-8")
        locked.chmod(0)
        try:
            with pytest.raises(GitGuardError, match=r"locked/f\.py"):
                sandbox.patch(("m.py", "locked/f.py"))
        finally:
            locked.chmod(0o755)


def test_a_patch_refuses_an_nfd_spelling_of_a_non_ascii_path(tmp_path, sandboxes):
    """Deliberate: the verdict's paths are NFC (fix_scope), as git reports
    them, so an NFD spelling is not quietly taken to mean the same file."""
    nfc = unicodedata.normalize("NFC", "café.py")
    repo = git_repo(tmp_path, {nfc: "x = 1\n"})
    with prepare_sandbox(repo, "HEAD", parent=sandboxes) as sandbox:
        (sandbox.path / nfc).write_text("x = 2\n", encoding="utf-8")
        assert sandbox.patch((nfc,))

        with pytest.raises(GitGuardError):
            sandbox.patch((unicodedata.normalize("NFD", nfc),))


# --- the snapshot ------------------------------------------------------------------------

@pytest.fixture
def sandbox(tmp_path, sandboxes):
    repo = git_repo(tmp_path, {"m.py": "x = 1\n", "sub/n.py": "y = 1\n"})
    made = prepare_sandbox(repo, "HEAD", parent=sandboxes)
    yield made
    made.cleanup()


@pytest.mark.parametrize("label,change", [
    (".git/config", lambda g: g.joinpath("config").write_text(
        g.joinpath("config").read_text() + "[core]\n\tfsmonitor = /bin/true\n")),
    (".git/hooks/pre-commit", lambda g: (g.joinpath("hooks").mkdir(),
                                         g.joinpath("hooks", "pre-commit").write_text("x"))),
    (".git/info/attributes", lambda g: (g.joinpath("info").mkdir(),
                                        g.joinpath("info", "attributes").write_text("* diff"))),
    (".git/HEAD", lambda g: g.joinpath("HEAD").write_text("ref: refs/heads/main\n")),
    (".git/index", lambda g: g.joinpath("index").write_bytes(b"")),
    (".git/packed-refs", lambda g: g.joinpath("packed-refs").write_text("\n")),
])
def test_every_change_to_the_sandboxs_git_files_is_reported(sandbox, label, change):
    """`.git` is invisible to `git diff`, so it is compared file by file with
    the snapshot, by reading files: no git command runs to find out."""
    state = snapshot_git_state(sandbox)
    change(sandbox.path / ".git")

    found = verify_git_state(state)

    assert [c.label for c in found] == [label]
    assert found[0].change in ("added", "changed")


def test_a_gitattributes_anywhere_in_the_tree_is_held_to_the_snapshot(sandbox):
    state = snapshot_git_state(sandbox)
    (sandbox.path / "sub" / ".gitattributes").write_text("* filter=x\n", encoding="utf-8")

    found = verify_git_state(state)

    assert [(c.label, c.change) for c in found] == [("sub/.gitattributes", "added")]
    assert "filter" in found[0].reason


def test_an_unchanged_sandbox_reports_nothing(sandbox):
    state = snapshot_git_state(sandbox)
    git(sandbox.path, "diff", "HEAD")

    assert verify_git_state(state) == () and tree_changes(state) == ()


def test_the_manifest_sees_every_file_git_would_not(sandbox):
    """Ignored, nested-ignored and plain edits alike: the work tree is read
    as files, outside git's opinion of what counts."""
    (sandbox.path / ".gitignore").write_text("build/\n", encoding="utf-8")
    state = snapshot_git_state(sandbox)
    (sandbox.path / "build").mkdir()
    (sandbox.path / "build" / "out.py").write_text("x\n", encoding="utf-8")
    (sandbox.path / "sub" / "hidden").mkdir()
    (sandbox.path / "sub" / "hidden" / ".gitignore").write_text("*\n", encoding="utf-8")
    (sandbox.path / "sub" / "hidden" / "new.py").write_text("x\n", encoding="utf-8")
    (sandbox.path / "m.py").write_text("x = 2\n", encoding="utf-8")
    (sandbox.path / "sub" / "n.py").unlink()

    assert tree_changes(state) == ("build/out.py", "m.py", "sub/hidden/.gitignore",
                                   "sub/hidden/new.py", "sub/n.py")


def test_a_snapshot_needs_a_sandbox_with_a_real_git_directory(sandbox, tmp_path):
    with pytest.raises(GitGuardError, match="sandbox"):
        snapshot_git_state(tmp_path)
    real = sandbox.path / ".git"
    moved = tmp_path / "moved-git"
    real.rename(moved)
    real.symlink_to(moved)

    with pytest.raises(GitGuardError, match="git directory"):
        snapshot_git_state(sandbox)


@pytest.mark.parametrize("where", [".git/config", ".git"])
def test_a_nested_git_directory_or_file_is_refused_before_git_runs(sandbox, where):
    """A `.git` below the top (a directory with a config, or a `gitdir:`
    file) makes git treat that directory as a repository of its own, and a
    git that descends into it reads that repository's config: code the
    review's git would run."""
    state = snapshot_git_state(sandbox)
    target = sandbox.path / "sub" / where
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("[core]\n\tfsmonitor = touch /tmp/never\n" if where.endswith("config")
                      else "gitdir: /elsewhere\n", encoding="utf-8")

    found = verify_git_state(state)

    assert [c.label for c in found] == [f"sub/{where}"]
    assert "nested git" in found[0].reason


@needs_permissions
def test_a_directory_the_agent_makes_unreadable_is_a_change(sandbox):
    """os.walk skips a directory it cannot list unless told otherwise; one the
    agent locked is recorded, so what it hides shows as a change."""
    state = snapshot_git_state(sandbox)
    hidden = sandbox.path / "hidden"
    hidden.mkdir()
    (hidden / "new.py").write_text("x = 1\n", encoding="utf-8")
    hidden.chmod(0)
    try:
        assert tree_changes(state) == ("hidden",)
    finally:
        hidden.chmod(0o755)
