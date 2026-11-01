"""cm-2qp.4 (E14.4): `curamoir state push`, a hosted run's ledger lines
committed to a state branch.

**Why.** A hosted runner (a GitHub Actions job, a Claude Code Routine) is
a fresh VM: whatever `curamoir run` appends to `.curamoir/state/ledger.jsonl`
(ledger.LEDGER_PATH, tracked in git) is gone when the VM is, and the
ledger is the only memory between runs (docs/direction-platform-first.md
§3, "Memory between runs"; §8 (1) "commits its ledger line"). The default
branch stays untouched and protected (user decision, 2026-10-30), so the
lines go to a dedicated non-default branch instead: `curamoir/state` by
default (DEFAULT_BRANCH), `claude/curamoir-state` from a Routine (E14.5's
prompt, templates/routine/sweep-prompt.md: the project's chosen name for
the Routine's state branch, kept apart from the workflow's; `claude/` is
only the default prefix of Claude's own pushes, not a confinement, so a
ruleset is what limits either branch). The
workflow template runs it after the sweep with `if: always()`, so a
refusal's line and a failure's line are kept too.

**Which lines are the run's.** The ledger is append-only, so the run's
lines are the work tree's lines that HEAD's committed ledger does not
hold (exact lines, in the work tree's order). Of those, the ones the
state branch already holds are dropped (exact line again: each line has
its own `at` and run fields, so two runs never write the same one), and
the rest are appended to the branch's ledger. A second push of the same
run therefore pushes nothing, and two runs never lose each other's lines.

**What a line may be** (edge cases review). The state branch may be
public, and the ledger is written by the run on a VM whose tests and fix
agent the run does not control, so before anything is compared or
appended each of the run's lines is:
- kept only if it is UTF-8 JSON whose value is an object with no key
  repeated at any depth, and at most MAX_LINE_BYTES (64 KiB) as written.
  Any other line (a NUL, invalid UTF-8, an array, a fragment, a
  megabyte of output, nesting too deep to walk) is skipped, and the
  outcome's sentence counts them ("It skipped N line(s)", which the
  workflow template turns into a warning); a ledger line is always one
  object, so nothing of the run's own is lost. WHY no repeated key: a
  parser keeps only the last value, so a secret in an earlier one would
  never be seen by redaction yet would still be in the bytes.
- redacted (curamoir.redaction.redact, as every artefact cúramóir writes
  is): every string in the object, keys included, and then always
  pushed in its canonical form, serialised exactly as ledger.record
  writes (`json.dumps(..., sort_keys=True, default=str)`), never as the
  raw bytes: what is pushed is then only what redaction saw. For a line
  ledger.record wrote that is its own bytes. Redacting the raw text
  instead could leave invalid JSON (the generic rule can eat a closing
  quote). redact is idempotent and this is deterministic, so the
  branch's lines, put through the same canonical form before they are
  compared, still match a retry's lines and a later run's.

**The branch's first commit is an orphan** holding only the ledger, at
its own path, with the run's lines only. Not a commit on the base: a
child of the default branch whose tree is only the ledger would read as
"delete the whole repository" against its parent in every diff and PR
tool, and one that kept the base's tree would carry a stale copy of the
code that nothing updates. An orphan has nothing to compare against the
code and nothing to merge into it by accident; readers read
`git show <branch>:.curamoir/state/ledger.jsonl`, the same path as on
the default branch.

**An existing branch must be a state branch** (edge cases review): its
tip's tree must hold exactly the ledger, as a regular file, which is the
layout this code creates. Anything else (a branch someone already uses
under that name, a copy of the code, a ledger beside other files) is
REFUSED, with a sentence telling the user to name another branch in
CURAMOIR_STATE_BRANCH. Appending to it would put a ledger commit on a
branch that means something else, and keeping its other files would
publish whatever they are again with every run. The check is made on
every attempt's fetched tip, so a branch replaced mid-race is refused
too.

**The mechanics** follow publish's conventions (curamoir.publish):
- Nothing in the checkout is touched but the object store and the
  remote-tracking ref `refs/remotes/origin/<branch>` the fetch updates:
  not the work tree, the index, HEAD or any local branch. The commit is
  built in a temporary index (GIT_INDEX_FILE) with hardened git
  (git_guard.git: no hooks, fsmonitor or global config) and `git
  commit-tree --no-gpg-sign`, never `git add` or `git commit`.
- Network steps (ls-remote, fetch, push) use the user's git
  (publish._user_git), since hardened git has no credentials; on the
  hosted workflow that is gh's credential helper (`gh auth setup-git`),
  which reads GH_TOKEN at push time.
- The push is `git push --force-with-lease=refs/heads/<branch>:<tip>`
  (empty `<tip>` when the branch is new), so a run that raced another
  can only be refused, never overwrite: on a lease failure (or a
  non-fast-forward) it fetches again and rebuilds on the new tip, at most
  PUSH_ATTEMPTS times. Never `--force`, never `+`, no tags.
- The target is refused when it is origin's default branch (origin's own
  HEAD, asked with `git ls-remote --symref`, and the checkout's view,
  guards.default_branch.detect_default_branch), when origin's default
  cannot be learned (fail closed), or when git would not take it as a
  branch name.
- The work tree's ledger is read without following a symbolic link at
  any component of its path from the repository root (cm-2qp.13):
  `.curamoir` and `.curamoir/state` must be directories and
  `ledger.jsonl` a regular file. A repository can commit any of them as
  a link (or the VM's tests or fix agent can plant one), to a secret on
  the runner or to another file whose JSON lines would then be redacted
  and published on the branch. A link, or a component of another kind,
  is REFUSED with a sentence naming it; a missing one means no ledger.
  Each directory is opened O_NOFOLLOW relative to its parent's
  descriptor and the file's type is checked on the descriptor that is
  read (_work_tree_ledger), so a link swapped in between the check and
  the read is refused, never followed. A ledger over MAX_LEDGER_BYTES
  (64 MiB) is refused too, so a planted huge file cannot exhaust the
  runner's memory. A hard link is not a gap: git cannot commit one, and
  only a process that could write the ledger anyway can make one; a
  committed symbolic link is the real vector. Like git_guard, this is
  POSIX-only (O_NOFOLLOW, dir_fd).

**Outcomes and exit codes** (StatePush.outcome, EXIT_CODES):
- PUSHED (0).
- FAILED (1): git failed (origin's pre-receive hook or a ruleset
  rejecting the push among them, which is not retried: only "the branch
  moved" is), the push did not finish within PUSH_TIMEOUT, or the branch
  kept moving for PUSH_ATTEMPTS attempts. Nothing on the branch was overwritten; on a hosted VM the
  run's lines go with the VM, so the step fails and the run shows it.
- REFUSED (3, as runner.EXIT_REFUSED): nothing was pushed, and the
  sentence says why (the default branch, a bad name, a branch that is
  not a state branch, a link or a wrong kind of file on the ledger's path,
  no identity).
- NOTHING_TO_PUSH (5): the run wrote no line HEAD lacks, or the branch
  has them all.
One sentence per outcome, which cli prints.
"""

import errno
import json
import os
import re
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from curamoir.git_guard import GitGuardError, git
from curamoir.guards.default_branch import detect_default_branch
from curamoir.ledger import LEDGER_PATH

# publish's own building blocks for the user's git, its push flags and its
# identity rule: reused, not copied, so the two pushes cannot drift apart.
from curamoir.publish import (
    PUSH_TIMEOUT,
    REMOTE_TIMEOUT,
    PublishRefused,
    _flag,
    _require_branch_name,
    _stderr,
    _user_git,
    user_identity,
)
from curamoir.redaction import redact

#: The state branch when none is named. A Routine passes
#: `claude/curamoir-state` (E14.5), the project's name for the Routine's
#: state branch; nothing confines a Routine's pushes to `claude/*`.
DEFAULT_BRANCH = "curamoir/state"

#: How many times the push is built and tried when another run moved the
#: branch in between. A module attribute, so tests need not race forever.
PUSH_ATTEMPTS = 3

#: The longest line pushed, in bytes as written (module docstring, "What a
#: line may be"). A job line is a few hundred bytes; 64 KiB leaves room for
#: a long reason or detail and none for a dumped log.
MAX_LINE_BYTES = 64 * 1024

PUSHED = "pushed"
FAILED = "failed"
REFUSED = "refused"
NOTHING_TO_PUSH = "nothing to push"

#: The one exit code of `state push` that `curamoir run` does not use. The
#: others are runner's (EXIT_SUCCESS 0, EXIT_FAILURE 1, EXIT_REFUSED 3), and
#: runner's exit-code table notes this one, so the numbering stays one scheme.
EXIT_NOTHING_TO_PUSH = 5

EXIT_CODES = {PUSHED: 0, FAILED: 1, REFUSED: 3, NOTHING_TO_PUSH: EXIT_NOTHING_TO_PUSH}

#: The ledger's path inside a tree, as git names it.
_LEDGER = "/".join(LEDGER_PATH)

_COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")

#: What git says when the branch moved under the push: the lease's refusal,
#: origin's own when the ref appeared meanwhile, and a non-fast-forward.
_CONTENDED = ("(stale info)", "reference already exists", "[rejected]", "fetch first",
              "non-fast-forward")


@dataclass(frozen=True)
class StatePush:
    """What `push_ledger` did: `outcome` (PUSHED, FAILED, REFUSED,
    NOTHING_TO_PUSH), the one `sentence` to print, how many `lines` it
    appended, the `sha` origin's branch now points at when it pushed, the
    `attempts` it took, and how many of the run's lines it `skipped` as
    not ledger lines (module docstring, "What a line may be")."""

    outcome: str
    sentence: str
    lines: int = 0
    sha: Optional[str] = None
    attempts: int = 0
    skipped: int = 0

    @property
    def exit_code(self):
        return EXIT_CODES[self.outcome]


class _Refused(Exception):
    """Nothing is pushed, for the reason in the message."""


class _Failed(Exception):
    """git could not do what the push needs; the message says what."""


def push_ledger(repo, branch=DEFAULT_BRANCH):
    """Append the run's ledger lines to origin's `branch` (module
    docstring): a StatePush, never an exception for any outcome above."""
    repo = os.path.abspath(str(repo))
    try:
        return _push_ledger(repo, branch)
    except _Refused as refused:
        return StatePush(REFUSED, f"cúramóir refused to push the ledger to {branch!r}: "
                                  f"{refused}. Nothing was pushed.")
    except _Failed as failed:
        return StatePush(FAILED, f"cúramóir could not push the ledger to {branch!r}: "
                                 f"{failed}")


def _push_ledger(repo, branch):
    _require_checkout(repo)
    # Once: the default-branch check, the fetch and the push all address
    # the repository origin pushes to.
    push_url = _push_url(repo)
    _require_target(repo, branch, push_url)
    run_lines, skipped = _run_lines(repo)
    note = _skipped_note(skipped)
    if not run_lines:
        return StatePush(NOTHING_TO_PUSH, f"cúramóir has no ledger lines to push to "
                                          f"{branch}: the run wrote none that HEAD lacks."
                                          f"{note}", skipped=skipped)
    try:
        identity = user_identity(repo)
    except PublishRefused as refused:
        raise _Refused(str(refused)) from None
    for attempt in range(1, PUSH_ATTEMPTS + 1):
        tip = _fetch_tip(repo, push_url, branch)
        _require_state_layout(repo, branch, tip)
        existing = _branch_ledger(repo, tip)
        # In the run lines' canonical form; a branch line that is not a
        # ledger line is compared as it is (it can then match nothing).
        present = {_pushable(line) or line for line in _lines(existing)}
        new = [line for line in run_lines if line not in present]
        if not new:
            return StatePush(NOTHING_TO_PUSH, f"cúramóir has no ledger lines to push to "
                                              f"{branch}: it already holds the run's "
                                              f"{len(run_lines)}.{note}", attempts=attempt,
                             skipped=skipped)
        sha = _commit(repo, tip, existing, new, identity)
        if _push(repo, branch, tip, sha):
            plural = "" if len(new) == 1 else "s"
            return StatePush(PUSHED, f"cúramóir pushed {len(new)} ledger line{plural} to "
                                     f"{branch} ({sha[:12]}).{note}", lines=len(new),
                             sha=sha, attempts=attempt, skipped=skipped)
    return StatePush(FAILED, f"cúramóir could not push the ledger to {branch!r}: another "
                             f"push moved it each time, {PUSH_ATTEMPTS} attempts. Nothing on "
                             "the branch was overwritten; the run's lines are still in this "
                             "checkout's work tree (on a hosted runner they are lost with the "
                             "VM).", attempts=PUSH_ATTEMPTS)


def _require_checkout(repo):
    try:
        top = git(repo, "rev-parse", "--show-toplevel").decode("utf-8", "surrogateescape")
    except GitGuardError as error:
        raise _Refused(f"{repo} is not a git checkout ({error})") from None
    if os.path.realpath(top.strip()) != os.path.realpath(repo):
        raise _Refused(f"{repo} is not the top of its git work tree ({top.strip()})")


def _require_target(repo, branch, push_url):
    """_Refused unless `branch` is a valid name that is not the default."""
    if not (isinstance(branch, str) and branch):
        raise _Refused(f"the state branch {branch!r} names no branch")
    try:
        _require_branch_name(repo, branch, "the state branch")
    except PublishRefused as refused:
        raise _Refused(str(refused)) from None
    origins = _origin_default(repo, push_url)
    local = detect_default_branch(repo)
    for default, said in ((origins, "origin's HEAD"), (local, "the checkout")):
        if default == branch:
            raise _Refused(f"{branch!r} is the default branch ({said} says so), which the "
                           "state push never writes")


def _origin_default(repo, push_url):
    """origin's default branch, from its own HEAD; _Refused when it cannot
    be learned, since then "not the default" cannot be shown."""
    listed = _remote(repo, ["ls-remote", "--symref", "--end-of-options", push_url,
                            "HEAD"], "ask origin which branch is its default")
    for line in listed.splitlines():
        if line.startswith("ref: ") and line.endswith("\tHEAD"):
            ref = line[len("ref: "):-len("\tHEAD")]
            if ref.startswith("refs/heads/"):
                return ref[len("refs/heads/"):]
    raise _Refused("origin did not say which branch is its default, so the state branch "
                   "cannot be shown not to be it")


def _push_url(repo):
    done = _user_git_or_fail(repo, ["remote", "get-url", "--push", "origin"])
    if done.returncode != 0:
        raise _Refused(f"the checkout has no origin to push to ({_stderr(done)})")
    return done.stdout.decode("utf-8", "surrogateescape").strip()


def _skipped_note(skipped):
    """The outcome sentence's note on skipped lines: "" when there were none."""
    if not skipped:
        return ""
    plural = "" if skipped == 1 else "s"
    return (f" It skipped {skipped} line{plural} that {'is' if skipped == 1 else 'are'} "
            f"not a JSON object of at most {MAX_LINE_BYTES // 1024} KiB with no repeated "
            "key.")


def _run_lines(repo):
    """The work tree's ledger lines HEAD's committed ledger does not hold,
    in order, each once (module docstring, "Which lines are the run's"),
    in their pushable form (`_pushable`), and how many were skipped as not
    ledger lines."""
    written = _work_tree_ledger(repo)
    if written is None:
        return [], 0
    committed = set(_lines(_head_ledger(repo)))
    run_lines, seen, skipped = [], set(), 0
    for line in _lines(written):
        if line in committed:
            continue
        pushable = _pushable(line)
        if pushable is None:
            skipped += 1
        elif pushable not in seen:
            seen.add(pushable)
            run_lines.append(pushable)
    return run_lines, skipped


#: The largest work-tree ledger read, in bytes (cm-2qp.13 review). A run
#: appends a few hundred bytes per job; a ledger past this is not one, and
#: reading a planted multi-gigabyte file would exhaust the runner's memory.
MAX_LEDGER_BYTES = 64 * 1024 * 1024

#: How a directory on the ledger's path is opened: never through a link.
#: O_CLOEXEC as git_guard opens its own (no descriptor leaks into the git
#: processes the push runs).
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
#: How the ledger itself is opened: never through a link, and without
#: waiting (a FIFO swapped in after the check would otherwise block the
#: open until a writer came) or taking a terminal as the controlling one.
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC

_NOT_A_DIRECTORY = "a directory in the work tree, so the ledger under it is not read"
_NOT_A_FILE = "a regular file in the work tree, so it is not read"


def _work_tree_ledger(repo):
    """The work tree's ledger's bytes, or None when it is missing (a
    missing `.curamoir` or `.curamoir/state` too): nothing to push.

    cm-2qp.13 (module docstring, "the work tree's ledger"): no component
    of LEDGER_PATH is followed through a symbolic link. `.curamoir` is
    opened with O_NOFOLLOW relative to the repository root's descriptor,
    `state` relative to `.curamoir`'s, and `ledger.jsonl` relative to
    `state`'s, so a link swapped in between a check and the open is
    refused by the open itself; the file's type and size are checked on
    the descriptor that is then read (fstat), so what was checked is what
    is read. Each component is first lstat'ed (relative to the same
    descriptor) only so the refusal can say what it is, and so a FIFO or
    a device is refused before it is ever opened. A ledger larger than
    MAX_LEDGER_BYTES is refused, and the read stops past that bound even
    if the file grows meanwhile. _Refused names the component; _Failed
    for any other error. Every descriptor is closed."""
    descriptors = []
    try:
        try:
            descriptors.append(os.open(repo, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC))
        except OSError as error:
            raise _Failed(f"the work tree could not be opened ({error})") from None
        *directories, file = LEDGER_PATH
        for depth, name in enumerate(directories, start=1):
            component = "/".join(LEDGER_PATH[:depth])
            if not _check_component(descriptors[-1], name, component, stat.S_ISDIR,
                                    _NOT_A_DIRECTORY):
                return None
            descriptor = _open(descriptors[-1], name, component, _DIRECTORY_FLAGS)
            if descriptor is None:
                return None
            descriptors.append(descriptor)
        if not _check_component(descriptors[-1], file, _LEDGER, stat.S_ISREG, _NOT_A_FILE):
            return None
        descriptor = _open(descriptors[-1], file, _LEDGER, _FILE_FLAGS)
        if descriptor is None:
            return None
        descriptors.append(descriptor)
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode):
            raise _Refused(f"{_LEDGER!r} is not {_NOT_A_FILE}")
        if status.st_size > MAX_LEDGER_BYTES:
            raise _Refused(_too_large())
        chunks, total = [], 0
        while chunk := os.read(descriptor, min(1 << 16, MAX_LEDGER_BYTES + 1 - total)):
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_LEDGER_BYTES:
                raise _Refused(_too_large())
        return b"".join(chunks)
    except OSError as error:
        raise _Failed(f"the work tree's ledger could not be read ({error})") from None
    finally:
        for descriptor in descriptors:
            os.close(descriptor)


def _too_large():
    return (f"{_LEDGER!r} is larger than {MAX_LEDGER_BYTES // (1024 * 1024)} MiB, which no "
            "run's ledger is, so it is not read")


def _check_component(parent, name, component, is_kind, refusal_tail):
    """False when `name` is missing under the `parent` descriptor; True
    when it is of `is_kind` (stat.S_ISDIR or S_ISREG); _Refused, naming
    `component`, when it is a symbolic link, or anything else ("is not "
    and `refusal_tail`)."""
    try:
        mode = os.stat(name, dir_fd=parent, follow_symlinks=False).st_mode
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(mode):
        raise _Refused(f"{component!r} is a symbolic link in the work tree, so it is not "
                       "followed and the ledger is not read")
    if not is_kind(mode):
        raise _Refused(f"{component!r} is not {refusal_tail}")
    return True


def _open(parent, name, component, flags):
    """`name` opened under the `parent` descriptor with `flags`, or None
    when it vanished since _check_component looked; _Refused when it was
    replaced by a link or another kind of file meanwhile (O_NOFOLLOW's
    ELOOP, O_DIRECTORY's ENOTDIR)."""
    try:
        return os.open(name, flags, dir_fd=parent)
    except FileNotFoundError:
        return None
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.ENOTDIR):
            raise _Refused(f"{component!r} changed into a symbolic link or another kind "
                           "of file after it was checked, so it is not read") from None
        raise


class _RepeatedKey(ValueError):
    """A JSON object names one key twice (module docstring, "What a line
    may be")."""


def _unique_keys(pairs):
    """json's object_pairs_hook: the object, or _RepeatedKey."""
    entry = dict(pairs)
    if len(entry) != len(pairs):
        raise _RepeatedKey("a key is repeated")
    return entry


def _pushable(line):
    """`line` as it may be pushed, or None when it is not a ledger line
    (module docstring, "What a line may be"): a UTF-8 JSON object of at
    most MAX_LINE_BYTES with no repeated key, every string in it redacted,
    in ledger.record's serialisation."""
    if len(line) > MAX_LINE_BYTES:
        return None
    try:
        entry = json.loads(line.decode("utf-8"), object_pairs_hook=_unique_keys)
        if not isinstance(entry, dict):
            return None
        # ledger.record's own serialisation (curamoir.ledger), so a line it
        # wrote with nothing to redact is pushed as the same bytes.
        return json.dumps(_redact_strings(entry), sort_keys=True, default=str).encode(
            "utf-8")
    # ValueError covers a JSON error, a repeated key and an integer too long
    # to convert; RecursionError nesting too deep for the parser or the walk.
    # No OverflowError: what json.loads parsed json.dumps can write (1e999
    # parses to inf, written back as Infinity).
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None


def _redact_strings(value):
    """`value` with redact applied to every string in it, keys included."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {_redact_strings(key): _redact_strings(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_strings(item) for item in value]
    return value


def _lines(data):
    """The non-blank lines of `data`, without their terminators: `\n`, and
    a `\r` before it. WHY the `\r`: with `* text eol=crlf` or
    core.autocrlf the work tree's ledger has CRLF while HEAD's blob and
    the branch's have LF, and compared as written no line would match, so
    the whole committed ledger would be pushed as the run's. Lines are
    compared, and appended, in this LF form."""
    lines = (line[:-1] if line.endswith(b"\r") else line for line in data.split(b"\n"))
    return [line for line in lines if line.strip()]


def _head_ledger(repo):
    """HEAD's committed ledger, or b"" when HEAD is unborn (no commit yet).
    A HEAD that exists but cannot be read is _Failed, never "no ledger":
    read as empty, every line in the work tree would count as the run's."""
    try:
        head = git(repo, "rev-parse", "--verify", "--quiet", "HEAD", ok=(0, 1)).strip()
    except GitGuardError as error:
        raise _Failed(f"git could not read HEAD ({error})") from None
    if not head:
        return b""
    return _ledger_at(repo, head.decode("ascii"))


def _ledger_at(repo, commit):
    """The bytes of the ledger in `commit`, or b"" when its tree has none.
    _Failed when git cannot read the commit or its tree, or the path is not
    a file there."""
    try:
        entry = git(repo, "ls-tree", "-z", commit, "--", _LEDGER)
        if not entry:
            return b""
        mode_type_id, _, _ = entry.partition(b"\t")
        mode, kind, blob = mode_type_id.decode("ascii").split()
        if kind != "blob" or mode not in ("100644", "100755"):
            raise _Failed(f"{_LEDGER} in {commit[:12]} is not a regular file")
        return git(repo, "cat-file", "blob", blob)
    except (GitGuardError, ValueError) as error:
        raise _Failed(f"git could not read {_LEDGER} in {commit[:12]} ({error})") from None


def _fetch_tip(repo, push_url, branch):
    """The SHA of origin's `branch`, fetched into the checkout's object
    store, or None when origin has no such branch."""
    destination = f"refs/heads/{branch}"
    listed = _remote(repo, ["ls-remote", "--end-of-options", push_url, destination],
                     f"ask origin whether {branch} exists")
    if not any(line.split("\t", 1)[-1] == destination for line in listed.splitlines()):
        return None
    tracking = f"refs/remotes/origin/{branch}"
    _remote(repo, ["fetch", "--quiet", "--no-tags", "--no-write-fetch-head",
                   "--recurse-submodules=no", "--end-of-options", push_url,
                   f"+{destination}:{tracking}"], f"fetch {branch}")
    try:
        tip = git(repo, "rev-parse", "--verify", "--quiet", "--end-of-options",
                  f"{tracking}^{{commit}}").decode("ascii", "replace").strip()
    except GitGuardError as error:
        raise _Failed(f"the fetched {branch} could not be read ({error})") from None
    if not _COMMIT.fullmatch(tip):
        raise _Failed(f"the fetched {branch} does not name a commit")
    return tip


def _require_state_layout(repo, branch, tip):
    """_Refused unless the branch is new (`tip` None) or its tree at `tip`
    is exactly the ledger as a regular file (module docstring, "An
    existing branch must be a state branch")."""
    if tip is None:
        return
    try:
        listed = git(repo, "ls-tree", "-r", "-z", "--full-tree", tip)
    except GitGuardError as error:
        raise _Failed(f"git could not read {branch} at {tip[:12]} ({error})") from None
    entries = [entry for entry in listed.split(b"\0") if entry]
    if len(entries) == 1:
        mode_type_id, _, path = entries[0].partition(b"\t")
        mode, kind = mode_type_id.decode("ascii", "replace").split()[:2]
        if path == _LEDGER.encode() and kind == "blob" and mode in ("100644", "100755"):
            return
    raise _Refused(f"origin's {branch} is not a state branch (its tree is not just "
                   f"{_LEDGER}), so it is not written to; name another branch with "
                   "--branch (CURAMOIR_STATE_BRANCH in the Actions workflow)")


def _branch_ledger(repo, tip):
    """The branch's ledger at `tip`, its unterminated last line closed."""
    if tip is None:
        return b""
    existing = _ledger_at(repo, tip)
    return existing if not existing or existing.endswith(b"\n") else existing + b"\n"


def _commit(repo, tip, existing, new, identity):
    """A commit on `tip` (an orphan when None) whose ledger is `existing`
    plus `new`, built through a temporary index: its SHA."""
    content = existing + b"".join(line + b"\n" for line in new)
    plural = "" if len(new) == 1 else "s"
    message = (f"curamoir state: {len(new)} ledger line{plural}\n\n"
               "Appended by `curamoir state push` (cm-2qp.4).\n")
    author = {"GIT_AUTHOR_NAME": identity.name, "GIT_AUTHOR_EMAIL": identity.email,
              "GIT_COMMITTER_NAME": identity.committer_name or identity.name,
              "GIT_COMMITTER_EMAIL": identity.committer_email or identity.email}
    with tempfile.TemporaryDirectory(prefix="curamoir-state-") as scratch:
        index = {"GIT_INDEX_FILE": os.path.join(scratch, "index")}
        ledger_file = os.path.join(scratch, "ledger")
        message_file = os.path.join(scratch, "message")
        Path(ledger_file).write_bytes(content)
        Path(message_file).write_text(message, encoding="utf-8")
        try:
            git(repo, "read-tree", *([tip] if tip else ["--empty"]), extra_env=index)
            # --no-filters: no clean filter or EOL conversion from
            # .gitattributes runs on the ledger; its bytes are stored as built.
            blob = git(repo, "hash-object", "-w", "--no-filters", ledger_file).decode(
                "ascii").strip()
            git(repo, "update-index", "--add", "--cacheinfo", f"100644,{blob},{_LEDGER}",
                extra_env=index)
            tree = git(repo, "write-tree", extra_env=index).decode("ascii").strip()
            parents = ["-p", tip] if tip else []
            return git(repo, "commit-tree", "--no-gpg-sign", tree, *parents, "-F",
                       message_file, extra_env=author).decode("ascii").strip()
        except GitGuardError as error:
            raise _Failed(f"the state commit could not be built ({error})") from None


def _push(repo, branch, tip, sha):
    """Push `sha` to origin's `branch`, leased on `tip`: True when it
    landed, False when the branch moved meanwhile; _Failed otherwise."""
    destination = f"refs/heads/{branch}"
    # To `origin` by name, as publish.push_branch pushes: git then uses
    # origin's push URL (the push_url the fetch and the default-branch check
    # used) and updates refs/remotes/origin/<branch>, as publish's push does.
    try:
        done = _user_git(repo, ["push", "--porcelain", f"--force-with-lease={destination}:"
                                f"{tip or ''}", "--no-follow-tags", "--recurse-submodules=no",
                                "origin", f"{sha}:{destination}"], PUSH_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise _Failed(f"`git push` did not finish within {PUSH_TIMEOUT} second(s) and was "
                      "stopped; whether the lines arrived is unknown, and a later push "
                      "will not add them twice") from None
    except OSError as error:
        raise _Failed(f"git could not be started ({error})") from None
    said = done.stdout.decode("utf-8", "replace") + done.stderr.decode("utf-8", "replace")
    if done.returncode != 0:
        if any(sign in said for sign in _CONTENDED):
            return False
        raise _Failed(f"`git push` exited {done.returncode}: {_stderr(done)}")
    # `*` a new branch, ` ` a fast-forward, `=` origin already at this very
    # commit (an identical push of the same lines in the same second).
    if _flag(said, f"{sha}:{destination}") not in ("*", " ", "="):
        raise _Failed(f"origin did not report {branch} updated")
    return True


def _remote(repo, args, doing):
    """The stdout of the user's `git args`, or _Failed saying what failed."""
    done = _user_git_or_fail(repo, args)
    if done.returncode != 0:
        raise _Failed(f"could not {doing}: {_stderr(done)}")
    return done.stdout.decode("utf-8", "replace")


def _user_git_or_fail(repo, args):
    try:
        return _user_git(repo, args, REMOTE_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError, ValueError) as error:
        raise _Failed(f"git {args[0]} could not run ({error})") from None
