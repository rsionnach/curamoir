"""The git-safety layer under the sweep's review step (E3-R2, cm-fkd.3).

fix_scope decides whether a sweep fix is in scope; this module makes sure
that asking git what the fix changed cannot run anything the fix planted,
and that the tree being asked about is not the user's. The order the runner
calls things in is fix_scope's module docstring, "What the runner (cm-fkd.4)
must do".

**The sandbox.** The fix step never runs in the user's checkout. It runs in
a disposable clone of the pinned base commit (prepare_sandbox), made with
`git clone --no-local --no-hardlinks --no-checkout --template=<empty dir>`
and checked out detached at the base. WHY, from the escapes in-checkout
review kept finding (the decision recorded on cm-fkd.3):

- the owner's `.git/config` can `include.path` a file in the work tree,
  which the agent may edit, and a committed `*.py filter=x` then runs
  whatever clean command the agent wrote there, inside the review's own
  `git diff`. A clone's `.git/config` is written fresh by git: no include,
  no filter, no fsmonitor, no hooks (the empty template copies none of
  git's sample hooks or `info/exclude` either; prepare_sandbox checks the
  config's keys against an allowlist rather than trusting that);
- a file git ignores (`.venv/lib/requests.py`, or anything under a
  self-ignoring nested `.gitignore`) is invisible to `git diff` and to
  `ls-files --exclude-standard`. A fresh clone holds no ignored file, so
  the review lists untracked files without excludes, and the snapshot
  below also hashes every file in the tree, so a file git will not report
  at all (inside an uninitialised submodule) is still seen;
- `.curamoir/principles.md` edited in the tree could grant `permits:
  signature_change`. The review reads principles from the base commit
  (Sandbox.show), never the tree.

What the sandbox is NOT: an operating-system sandbox. It is a directory.
The fix agent's reach beyond it is Claude Code's permission check, scoped
to the sandbox's path (fix_step). The user's uncommitted changes and
ignored files are not in it, by design: the base is the committed commit,
the lint ran on it, and the review compares against it.

**Hardened git.** Every git command here runs through `git()`:

- with the repository's code-running config turned off (_GIT_HARDENING);
- with every inherited `GIT_*` environment variable dropped (a `GIT_DIR`
  or `GIT_CONFIG_PARAMETERS` from whatever started the runner would point
  git elsewhere or plant config), and the system and global config not
  read (`GIT_CONFIG_NOSYSTEM`, `GIT_CONFIG_GLOBAL=/dev/null`, which needs
  git 2.32+; an older git ignores it and reads the global config). Nothing
  here needs the user's config: the sandbox is a local clone, nothing is
  pushed or fetched over a network, and no commit is authored;
- in a session of its own, bounded by a timeout, with its whole process
  group killed at the bound (run_in_group).

**The snapshot.** snapshot_git_state(sandbox), taken just before the fix
agent starts, records digests of the sandbox's git files (config, HEAD,
index, packed-refs, shallow, and the hooks, info, refs and objects/info
trees) and of every file in the work tree. verify_git_state compares the
git files and every `.gitattributes`, and refuses any nested `.git`, by
reading files and before any git command runs; tree_changes lists every
work-tree path that differs, which fix_scope holds git's own account of the
change to. In the sandbox this is defence in depth: the agent is denied
`.git` and nothing else writes there.

**Limits, stated rather than hidden.**

- The source repository's own config is read, by the hardened git that
  resolves the base and by the clone (which runs `git upload-pack` there),
  including any `include.path` it names. That happens before the fix agent
  exists, on files it never reaches, so it is the owner's own setup; this
  module runs no git in the source after the sandbox is made.
- The sandbox follows the base commit, not the source's checkout: it is
  checked out by a clone whose config is git's defaults, so a source with
  `core.symlinks=false` (its symlinks are plain files on disk) has real
  symlinks in the sandbox, and `core.autocrlf` or `core.eol` there do not
  apply. What the lint and the review read is the commit's content.
- Submodules are not initialised: a submodule is an empty directory in the
  sandbox, so a violation inside one cannot be fixed there.
- Git LFS and every other smudge filter are not run (no filter is
  configured in the sandbox), so a file the owner stores through a filter
  is its pointer text in the sandbox.
- A base commit reachable from no ref of the source is fetched by its SHA,
  which the git on this machine allows over a local path; a git that does
  not makes prepare_sandbox fail rather than check out something else.
- Hashing the work tree costs a read of every file, twice per run. For a
  weekly job on a source tree (a fresh clone holds no venv or
  node_modules) that is seconds.

Syntax that imports on 3.9: the sweep reaches this through
`scripts/curamoir`, whose interpreter may be the fallback `python3`.
"""

import contextlib
import functools
import hashlib
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import warnings
from dataclasses import dataclass
from pathlib import Path

#: The git executable. A module attribute so a test can stand in a slow one.
GIT = "git"

#: Seconds a git command may run. Generous: they read one repository.
DEFAULT_GIT_TIMEOUT = 120

#: Seconds the clone may run: it copies every object of the source.
CLONE_TIMEOUT = 900

#: Seconds to wait for a killed process group's pipes to close.
_REAP_TIMEOUT = 5

#: Configuration every git command here runs with. WHY: a repository's own
#: config can name programs git runs as a side effect, `core.fsmonitor` on
#: every index refresh (`git diff` against the working tree is one) and
#: hooks via `core.hooksPath`. External diff drivers and textconv are off
#: per command (`--no-ext-diff --no-textconv`). A filter driver cannot be
#: turned off without knowing its name, which is why the sandbox has no
#: configured filter at all. Submodules are never descended into
#: (`diff.ignoreSubmodules=all`, `submodule.recurse=false`): to look inside
#: one, git runs a git in it, and that repository's config — which a fix
#: agent could have written as `<path>/.git/config` — would decide what that
#: git runs. `core.quotepath=off` keeps non-ASCII paths unquoted in the few
#: outputs not read with -z.
_GIT_HARDENING = ("-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
                  "-c", "diff.ignoreSubmodules=all", "-c", "submodule.recurse=false",
                  "-c", "status.submoduleSummary=false", "-c", "core.quotepath=off")

#: Environment git runs with, on top of the inherited one minus every GIT_*.
_GIT_ENVIRONMENT = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_ATTR_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
    # Read-only commands must not rewrite the index: it is held to the
    # snapshot, and a refresh written by `git diff` would look like a change.
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_NO_REPLACE_OBJECTS": "1",
}

#: The keys a freshly cloned `.git/config` may hold. Anything else means the
#: clone did not come out as git documents it, and no sandbox is made.
_CLONE_CONFIG_KEYS = re.compile(
    r"^(core\.(repositoryformatversion|filemode|bare|logallrefupdates|ignorecase"
    r"|precomposeunicode|symlinks)|remote\.origin\.(url|fetch)|branch\.[^\n]+\.(remote|merge)"
    r"|extensions\.objectformat)$")


class GitGuardError(Exception):
    """git could not answer, or was refused, so nothing it would have said can
    be trusted. The runner must treat this as a refusal: no sandbox, no
    verdict, no PR. fix_scope.ScopeCheckError is a subclass."""


# --- running processes ----------------------------------------------------------------

def scrubbed_environment():
    """The inherited environment without any GIT_* variable."""
    return {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}


def run_in_group(argv, *, cwd, env, timeout, stdin=subprocess.DEVNULL, tail_bytes=None):
    """Run `argv` in a new session and return a CompletedProcess with bytes
    output. At `timeout` seconds, kill its whole process group and raise
    subprocess.TimeoutExpired. WHY a group: killing only the child leaves a
    helper it started (a model CLI's tool process, a git's filter) running,
    and still writing, after the caller has moved on. A descendant that left
    the group with setsid() escapes this; nothing here starts one.

    `tail_bytes` (green_gate's mode, for output nobody here controls): the
    output goes to unlinked temporary files, not pipes, and only the last
    `tail_bytes` of each stream are returned. WHY files: memory stays
    bounded however much a test suite prints (disk does not), and the run
    ends when the child does, even if a setsid() daemon still holds the
    output open, which with pipes holds communicate() until the daemon
    exits. git's own capture keeps pipes and the whole output."""
    if tail_bytes is not None:
        return _run_in_group_to_files(argv, cwd, env, timeout, stdin, tail_bytes)
    process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=stdin, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, start_new_session=True)
    try:
        out, err = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(process)
        out, err = _reap(process)
        raise subprocess.TimeoutExpired(argv, timeout, output=out, stderr=err) from None
    except BaseException:
        _kill_group(process)
        _reap(process)
        raise
    # The child is done; anything it left behind in its group goes with it.
    _kill_group(process)
    return subprocess.CompletedProcess(argv, process.returncode, out, err)


def _run_in_group_to_files(argv, cwd, env, timeout, stdin, tail_bytes):
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=stdin, stdout=out,
                                   stderr=err, start_new_session=True)
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_group(process)
            process.wait()
            raise subprocess.TimeoutExpired(argv, timeout, output=_file_tail(out, tail_bytes),
                                            stderr=_file_tail(err, tail_bytes)) from None
        except BaseException:
            _kill_group(process)
            process.wait()
            raise
        _kill_group(process)
        return subprocess.CompletedProcess(argv, process.returncode, _file_tail(out, tail_bytes),
                                           _file_tail(err, tail_bytes))


def _file_tail(handle, size):
    handle.seek(0, os.SEEK_END)
    handle.seek(max(0, handle.tell() - size))
    return handle.read()


def _kill_group(process):
    # Gone already (ProcessLookupError), or the id now names a group that is
    # not ours (PermissionError): either way there is nothing of ours to kill.
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)


def _reap(process):
    """The output so far of a process whose group was killed. If a descendant
    outside the group still holds the pipes, give up on them after
    _REAP_TIMEOUT rather than wait for it."""
    try:
        return process.communicate(timeout=_REAP_TIMEOUT)
    except subprocess.TimeoutExpired:
        for pipe in (process.stdout, process.stderr):
            if pipe:
                pipe.close()
        process.wait()
        return b"", b""


# --- git --------------------------------------------------------------------------------

def _run_git(repo, args, timeout, extra_env=None):
    env = scrubbed_environment()
    env.update(_GIT_ENVIRONMENT)
    env.update(extra_env or {})
    try:
        return run_in_group([GIT, *_GIT_HARDENING, *args], cwd=str(repo), env=env,
                            timeout=timeout)
    except subprocess.TimeoutExpired as expired:
        raise GitGuardError(
            f"git {' '.join(args)} did not finish within {timeout} second"
            f"{'' if timeout == 1 else 's'}; its process group was killed") from expired
    except OSError as error:
        raise GitGuardError(f"git could not be started: {error}") from error


def git(repo, *args, ok=(0,), timeout=DEFAULT_GIT_TIMEOUT, extra_env=None):
    """Run hardened git in `repo` and return its stdout as bytes, or raise
    GitGuardError naming the command, if it exits with a code not in `ok`
    or does not finish within `timeout` seconds. `extra_env` is added after
    the inherited GIT_* variables are dropped."""
    result = _run_git(repo, args, timeout, extra_env)
    if result.returncode not in ok:
        raise GitGuardError(
            f"git {' '.join(args)} failed ({result.returncode}): "
            f"{result.stderr.decode('utf-8', 'backslashreplace').strip()}")
    return result.stdout


def resolve_base(repo, base_ref):
    """`base_ref` as a commit SHA, or GitGuardError. `--end-of-options` and
    the leading-dash refusal keep it from being read as an option."""
    if base_ref.startswith("-"):
        raise GitGuardError(f"base ref {base_ref!r} looks like an option")
    out = git(repo, "rev-parse", "--verify", "--quiet", "--end-of-options",
              f"{base_ref}^{{commit}}", ok=(0, 1, 128)).decode("ascii", "replace").strip()
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", out):
        raise GitGuardError(f"base ref {base_ref!r} does not name a commit")
    return out


def _require_top_level(repo):
    top = git(repo, "rev-parse", "--show-toplevel").decode("utf-8", "surrogateescape").strip()
    if os.path.realpath(top) != os.path.realpath(str(repo)):
        raise GitGuardError(
            f"{repo} is not the top of its git work tree ({top}); the sweep lints, "
            "fixes and reviews a whole repository")


# --- the sandbox ------------------------------------------------------------------------

class Sandbox:
    """A disposable clone of `source` at commit `base`, at `path`, made by
    prepare_sandbox. A context manager: leaving it removes the clone, and
    raises GitGuardError if it cannot; leaving on an exception only warns
    (_remove_or_warn), so the exception that ended the block is the one
    that propagates.

    `work_list` and `rules` are fix_scope's slots, not this module's: the
    sweep.WorkList the lint gave for this sandbox at its base, and the rules
    it ran, set by fix_scope.lint_sandbox and read by fix_scope.verify_fix
    and remedies.apply_file_remedies, so the review can only ever compare a
    fix with the work list and rules of the commit the fix was made on.
    Nothing here reads them."""

    def __init__(self, source, path, base, root):
        self.source = source
        self.path = path
        self.base = base
        self.work_list = None
        self.rules = None
        self._root = root

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        # A cleanup failure raises only when nothing else is on its way out.
        if exc_type is None:
            self.cleanup()
        else:
            _remove_or_warn(self._root)
        return False

    def __repr__(self):
        return f"Sandbox(source={self.source!r}, path={str(self.path)!r}, base={self.base!r})"

    def cleanup(self):
        """Remove the clone. Safe to call more than once. GitGuardError if
        any of it is left (remove_tree)."""
        remove_tree(self._root)

    def show(self, path):
        """The bytes of `path` at the base commit, or None if it has no such
        file. Read from git's objects, so nothing done to the work tree
        changes the answer."""
        if path.startswith("-"):
            return None
        blob = git(self.path, "rev-parse", "--verify", "--quiet", "--end-of-options",
                   f"{self.base}:{path}", ok=(0, 1, 128)).decode("ascii", "replace").strip()
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", blob):
            return None
        if git(self.path, "cat-file", "-t", blob).strip() != b"blob":
            return None
        return git(self.path, "cat-file", "blob", blob)

    def head(self):
        """The commit the sandbox's HEAD names now."""
        return git(self.path, "rev-parse", "--verify", "HEAD").decode("ascii").strip()

    def patch(self, paths):
        """A binary-safe patch, `git apply`-able on the base commit, of
        exactly `paths`: how an accepted fix leaves the sandbox (cm-fkd.4
        passes green_gate's GateResult.paths_for_pr, the tested subset of
        ScopeVerdict.paths, and nothing else).

        Built in a throwaway index (GIT_INDEX_FILE), so the sandbox's own
        index and HEAD are untouched; `--force` so a path the base ignores
        is carried as it is rather than dropped. Call it only after
        fix_scope.verify_fix has accepted the fix: it trusts the paths."""
        if not paths:
            return b""
        specs = [f":(literal){path}" for path in paths]
        handle, index = tempfile.mkstemp(prefix="curamoir-index-")
        os.close(handle)
        os.unlink(index)
        try:
            environment = {"GIT_INDEX_FILE": index}
            git(self.path, "read-tree", self.base, extra_env=environment)
            git(self.path, "add", "--all", "--force", "--", *specs, extra_env=environment)
            # WHY check: git add skips, without failing, what it will not
            # stage (a FIFO; a file in a directory it cannot read), and a
            # patch that silently drops an accepted path is not that fix.
            staged = git(self.path, "diff", "--cached", "--name-only", "-z", "--no-renames",
                         self.base, "--", *specs, extra_env=environment)
            names = {name.decode("utf-8", "surrogateescape")
                     for name in staged.split(b"\0") if name}
            left_out = sorted(set(paths) - names)
            if left_out:
                raise GitGuardError(
                    f"git would not stage {', '.join(left_out)} (a FIFO, an unreadable "
                    "file or directory, or no change), so a patch would leave it out")
            return git(self.path, "diff", "--cached", "--binary", "--full-index",
                       "--no-renames", "--no-ext-diff", "--no-textconv", self.base, "--",
                       *specs, extra_env=environment)
        finally:
            if os.path.exists(index):
                os.unlink(index)


def prepare_sandbox(repo, base_ref, parent=None, timeout=CLONE_TIMEOUT):
    """A Sandbox: a fresh clone of `repo` checked out detached at the commit
    `base_ref` names there. `parent` is the directory to make it in (the
    system temporary directory by default). GitGuardError, and nothing left
    on disk, if `base_ref` names no commit or looks like an option, `repo`
    is not the top of a git work tree, or the clone does not come out as
    documented (module docstring)."""
    source = os.path.abspath(str(repo))
    _require_top_level(source)
    base = resolve_base(source, base_ref)
    try:
        root = Path(tempfile.mkdtemp(prefix="curamoir-sweep-", dir=parent))
    except OSError as error:
        raise GitGuardError(f"cannot make a sandbox in {parent or tempfile.gettempdir()}: "
                            f"{error}") from error
    try:
        template = root / "template"
        template.mkdir()
        path = root / "repo"
        git(root, "clone", "--no-local", "--no-hardlinks", "--no-checkout",
            f"--template={template}", "--quiet", "--", source, str(path), timeout=timeout)
        if _run_git(path, ["cat-file", "-e", f"{base}^{{commit}}"],
                    DEFAULT_GIT_TIMEOUT).returncode != 0:
            git(path, "fetch", "--quiet", "--no-tags", "origin", base, timeout=timeout)
        git(path, "checkout", "--quiet", "--detach", base, timeout=timeout)
        _check_clone(path)
        return Sandbox(source, path, base, root)
    except BaseException:
        _remove_or_warn(root)
        raise


def remove_tree(root):
    """Remove `root` and everything under it; GitGuardError if any of it is
    left. WHY not rmtree(ignore_errors=True): the fix agent can make a
    directory unlistable (mode 000) or read-only (555), and rmtree then
    silently leaves the whole clone. Every directory is made u+rwx first,
    and the error handler does the same to a path and its parent (never to
    anything above `root`: the directory the sandbox was made in is the
    caller's) and retries.

    Nothing is chmodded by path (_make_removable): chmod follows a symlink,
    and a process still running in the sandbox could swap a directory for
    one between a check and the chmod."""
    if not os.path.lexists(root):
        return
    root = os.path.abspath(str(root))
    _make_removable(root, root)
    for directory, subdirs, _ in os.walk(root):
        for name in subdirs:
            _make_removable(root, os.path.join(directory, name))
    retry = functools.partial(_retry_removal, root)
    # 3.12 deprecates onerror (a warning, an error under the tests' -W
    # error) and 3.9, which scripts/curamoir may run this on, lacks onexc.
    if sys.version_info >= (3, 12):  # noqa: UP036
        shutil.rmtree(root, onexc=retry)
    else:
        shutil.rmtree(root, onerror=retry)
    if os.path.lexists(root):
        raise GitGuardError(f"the sandbox at {root} could not be removed; delete it by hand")


def _remove_or_warn(root):
    """remove_tree while another exception is on its way out. WHY a warning,
    not a raise: that exception says why the run failed, and one raised
    here would replace it (kept only as __context__, which a runner that
    catches GitGuardError never reads)."""
    try:
        remove_tree(root)
    except GitGuardError as error:
        warnings.warn(str(error), RuntimeWarning, stacklevel=3)


_NO_FOLLOW_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _make_removable(root, path):
    """Make the directory `path`, inside `root` or `root` itself, u+rwx,
    without following a symlink anywhere below the directory `root` is in.

    WHY by file descriptor: from root's parent (the caller's, never
    changed) each component is opened with O_NOFOLLOW relative to the one
    before, so a component swapped for a symlink fails to open (ELOOP or
    ENOTDIR) and is skipped rather than followed; the last one is fchmodded
    through the descriptor that was checked to be a directory. A directory
    that cannot be opened for reading (mode 000) gets fchmodat with
    AT_SYMLINK_NOFOLLOW relative to its opened parent instead; where the
    platform has no such call (Linux with glibc before 2.32) it is left as
    it is, and remove_tree's last check reports it. Anything else in the
    way is left to that check too."""
    parts = [os.path.basename(root)]
    if path != root:
        parts.extend(os.path.relpath(path, root).split(os.sep))
    try:
        parent = os.open(os.path.dirname(root), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError:
        return
    try:
        for part in parts[:-1]:
            child = os.open(part, _NO_FOLLOW_DIRECTORY, dir_fd=parent)
            os.close(parent)
            parent = child
        _loosen(parent, parts[-1])
    except OSError:
        pass  # what is still in the way, remove_tree's last check reports
    finally:
        os.close(parent)


def _loosen(parent, name):
    try:
        handle = os.open(name, _NO_FOLLOW_DIRECTORY, dir_fd=parent)
    except PermissionError:
        with contextlib.suppress(OSError, NotImplementedError, ValueError):
            mode = os.stat(name, dir_fd=parent, follow_symlinks=False).st_mode
            if stat.S_ISDIR(mode):
                os.chmod(name, stat.S_IMODE(mode) | stat.S_IRWXU, dir_fd=parent,
                         follow_symlinks=False)
        return
    try:
        mode = os.fstat(handle).st_mode
        if stat.S_ISDIR(mode):
            os.fchmod(handle, stat.S_IMODE(mode) | stat.S_IRWXU)
    finally:
        os.close(handle)


def _retry_removal(root, function, path, _error):
    if function not in (os.rmdir, os.unlink, os.remove):
        return  # a failed open or listing: retrying it would remove nothing
    if path != root:  # root's parent is the caller's directory, not ours
        _make_removable(root, os.path.dirname(path))
    _make_removable(root, path)
    with contextlib.suppress(OSError):  # reported by remove_tree's last check
        function(path)


def _check_clone(path):
    hooks = path / ".git" / "hooks"
    if hooks.exists():
        raise GitGuardError(f"the sandbox clone has a hooks directory ({hooks}) although "
                            "it was made from an empty template")
    keys = git(path, "config", "--file", str(path / ".git" / "config"), "--name-only",
               "--list").decode("utf-8", "replace").split()
    unexpected = [key for key in keys if not _CLONE_CONFIG_KEYS.match(key)]
    if unexpected:
        raise GitGuardError("the sandbox clone's .git/config has keys a fresh clone does "
                            f"not write: {', '.join(unexpected)}")


# --- the snapshot -----------------------------------------------------------------------

@dataclass(frozen=True)
class GitState:
    """What snapshot_git_state saw in a sandbox: its `path`, its `git_dir`,
    label -> digest for its git files (`digests`) and for every file in its
    work tree (`tree`), and the `base` commit it was taken for."""

    path: str
    git_dir: str
    digests: tuple
    tree: tuple
    base: str


@dataclass(frozen=True)
class StateChange:
    """One git file or `.gitattributes` added, removed or changed since the
    snapshot: `label` is its sandbox-relative path, `change` one of "added",
    "removed", "changed", and `reason` why that matters."""

    label: str
    change: str
    reason: str


#: The sandbox's git files that decide what git runs or sees. Objects are
#: content-addressed and read here only by SHA, so only objects/info (where
#: an `alternates` file would point git at another object store) is held.
_GIT_STATE_FILES = ("config", "config.worktree", "commondir", "HEAD", "index",
                    "packed-refs", "shallow")
_GIT_STATE_TREES = ("hooks", "info", "refs", "objects/info")


def require_sandbox(sandbox, error=GitGuardError):
    """Raise `error` (a GitGuardError class) unless `sandbox` is a Sandbox.
    One check for the fix step, the snapshot and the review, so all three
    refuse a checkout the same way."""
    if not isinstance(sandbox, Sandbox):
        raise error(f"{sandbox} is not a sandbox (git_guard.prepare_sandbox); the fix step "
                    "and its review run in a sandbox, never in a checkout")


def snapshot_git_state(sandbox):
    """The digests of `sandbox`'s git files and work tree (module docstring),
    read as files, never through git, so the same reading after the fix step
    runs nothing the fix step planted. GitGuardError if `sandbox` is not a
    Sandbox or its `.git` is not a real directory (a symlinked `.git` could
    be swapped for another directory unseen)."""
    require_sandbox(sandbox)
    path = Path(sandbox.path)
    git_dir = path / ".git"
    if git_dir.is_symlink() or not git_dir.is_dir():
        raise GitGuardError(f"the sandbox at {path} has no git directory of its own")
    return GitState(str(path), str(git_dir), _git_digests(path, git_dir),
                    _tree_digests(path), sandbox.base)


def verify_git_state(state):
    """A StateChange per git file or `.gitattributes` added, removed or
    changed since `state` was taken, and per work-tree entry inside or named
    a nested `.git` (present now or changed), in label order; () if none.

    WHY the nested `.git`: a directory holding one is a repository of its
    own to git, and a git command that opens it (a submodule status, a
    diff that descends) reads its config, which can name a program to run.
    git here is told never to descend (_GIT_HARDENING); this refuses the
    tree before git is asked anything, in case some command still would. A
    fresh clone has none: git does not track `.git` paths."""
    path = Path(state.path)
    found = list(_compare(dict(state.digests), dict(_git_digests(path, Path(state.git_dir))),
                          "git's own files can make the next git command run code, and "
                          "`git diff` never shows them"))
    before, after = dict(state.tree), dict(_tree_digests(path))
    found.extend(_compare(
        {k: v for k, v in before.items() if _is_attributes(k)},
        {k: v for k, v in after.items() if _is_attributes(k)},
        "a `.gitattributes` can point a filter or diff driver at a file, which the "
        "next `git diff` would apply"))
    for label in sorted(set(before) | set(after)):
        if ".git" not in label.split("/"):
            continue
        change = ("added" if label not in before else "removed" if label not in after
                  else "changed" if before[label] != after[label] else "present")
        found.append(StateChange(
            label, change, "a nested git directory or `.git` file makes git treat its "
            "directory as a repository of its own, whose config can make git run code"))
    return tuple(sorted(found, key=lambda change: change.label))


def tree_changes(state):
    """Every work-tree path (sandbox-relative, as the file system names it)
    added, removed or changed since `state` was taken, sorted."""
    before, after = dict(state.tree), dict(_tree_digests(Path(state.path)))
    return tuple(sorted(label for label in set(before) | set(after)
                        if before.get(label) != after.get(label)))


def _compare(before, after, reason):
    for label in set(before) | set(after):
        if before.get(label) == after.get(label):
            continue
        change = ("added" if label not in before else
                  "removed" if label not in after else "changed")
        yield StateChange(label, change, reason)


def _git_digests(root, git_dir):
    found = {}
    for name in _GIT_STATE_FILES:
        target = git_dir / name
        if target.exists() or target.is_symlink():
            found[_label(root, target)] = _digest(target)
    for name in _GIT_STATE_TREES:
        for directory, subdirs, files in os.walk(git_dir / name):
            for entry in subdirs + files:
                target = Path(directory) / entry
                if entry in files or target.is_symlink():
                    found[_label(root, target)] = _digest(target)
    return tuple(sorted(found.items()))


def _tree_digests(root):
    """Every non-directory entry under `root` but its own `.git`, at any
    depth, ignored or not. Symlinked directories are recorded as links and
    not followed."""
    found = {}

    def unreadable(error):
        # WHY: os.walk skips a directory it cannot list unless told, and
        # the agent can lock one to hide what it wrote in it. Recorded, the
        # directory is a change (fix_scope: HIDDEN_CHANGE).
        if error.filename:
            found[_label(root, Path(error.filename))] = (
                f"unreadable-dir: {error.__class__.__name__}")

    for directory, subdirs, files in os.walk(root, onerror=unreadable):
        if Path(directory) == root:
            subdirs[:] = [d for d in subdirs if d != ".git"]
        for entry in files + [d for d in subdirs if (Path(directory) / d).is_symlink()]:
            target = Path(directory) / entry
            found[_label(root, target)] = _digest(target)
    return tuple(sorted(found.items()))


def _is_attributes(label):
    return label == ".gitattributes" or label.endswith("/.gitattributes")


def _label(root, path):
    return path.relative_to(root).as_posix()


def _digest(path):
    try:
        mode = os.lstat(path).st_mode
        if stat.S_ISLNK(mode):
            return "link:" + os.readlink(path)
        if not stat.S_ISREG(mode):
            # A FIFO would block a read; its existence is what is recorded.
            return f"special:{stat.S_IFMT(mode):o}"
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as error:
        return f"unreadable: {error.__class__.__name__}"
