"""File-level remedies: the sweep fixes that Python applies, not the agent (cm-ul5).

A `target: path` rule's violation is a file's name, reported as the empty
region at 1:1 (docs/curamoir-lint.md). For `no-file-variant-proliferation`
on `handlers_v2.py` the fix is deleting the file, and the fix agent cannot
delete: its toolset is Read, Grep, Glob and Edit (fix_step), and that is
deliberate — the sweep is driven by Python, with enforcement in code rather
than in a prompt (cm-80h). Deleting a file the violation names is
mechanical, so it is done here, deterministically, and the agent is never
given a deletion tool.

Not every path rule means "delete": a naming-convention rule wants a
rename, which nothing applies. So a rule opts in with `remedy: delete_file`
(rules.REMEDIES), and only a file-level violation of such a rule is
deleted. fix_scope.verify_fix holds every deletion to the same test, so the
1:1 exception is never anyone else's to use.

**When.** apply_file_remedies runs after fix_scope.lint_sandbox and BEFORE
the fix step, on the sandbox's pristine base (it refuses any other tree).
WHY before: the agent never sees the file, so it cannot spend turns on it
or edit a file about to vanish; the reference check reads exactly the tree
the work list was linted from; and the deletion stays apart from the
agent's diff (it happens before run_fix_step's snapshot, and the review
still sees it, because the review diffs against the base commit, not the
snapshot). The agent's prompt is built from RemedyResult.for_agent, which
leaves out what was handled here.

**What keeps a file.** A file is deleted only if nothing at the base
refers to it, because a variant someone still imports is not dead, and
deleting it would sink the whole sweep at the test step (E3-R4) or, worse,
pass a test suite that does not cover the importer. "Refers to" is read
from git's objects at the base commit (hardened git, git_guard), never the
work tree, and is deliberately broad, since a false "referenced" only
leaves a violation unfixed and reported, while a false "unreferenced"
breaks code:

- any other Python or JS/TS source file (fix_scope's PYTHON_SUFFIXES and
  SCRIPT_SUFFIXES) containing the file's stem as a whole word — an import
  of the module or of its dotted name, a `from . import (` list across
  lines, `import_module("…handlers_v2")`, a JS/TS `./handlers_v2`, and also
  a comment or string that merely names it;
- any other text file of any kind containing `<parent>.<stem>` (for
  `src/sample/web/handlers_v2.py`, `web.handlers_v2`) as a whole word: a
  module path in a config file, an entry point, `python -m`, a server's
  `module:app`.

A file mentioned only by its path or file name in prose (PLANTED.json,
`principles.md`'s `bad` example, a changelog) is not kept for it: a
description of a file is not a use of it.

**The deletion** is a plain os.unlink of a regular file inside the
sandbox. The path must be repository-relative with no empty, `.`, `..` or
`.git` component; every directory on the way is lstat'ed and must be a real
directory (not a symlink), and the file itself must be a regular file, not
a symlink, directory or anything else. A dependency manifest or lockfile is
never deleted: the review refuses any change to one (fix_scope,
DEPENDENCY_CHANGE), so deleting it would only discard the whole fix. Each
refusal is a SkippedRemedy with the reason, for the runner to report the
violation as unfixed.

**Limits, stated rather than hidden.**

- A reference the two searches cannot see keeps nothing: an import whose
  module name is computed (`import_module(f"handlers_{v}")`), a top-level
  module (no parent directory) named only in a non-source file, a config
  that names the file's directory some other way (`--app-dir`). The
  project's tests (E3-R4) are the backstop.
- The searches are word matches, not parses, so they err towards keeping:
  a comment saying "unlike handlers_v2" in another module keeps the file.
- Only the `delete_file` remedy exists. A rename needs the new name, which
  is a judgement, and the agent may not rename either; such a violation is
  left for a person.

Syntax that imports on 3.9, like the rest of what `scripts/curamoir` runs.
"""

import dataclasses
import os
import stat
from dataclasses import dataclass
from pathlib import PurePosixPath

from curamoir.fix_scope import (
    DELETE_FILE,
    PYTHON_SUFFIXES,
    SCRIPT_SUFFIXES,
    ScopeCheckError,
    is_dependency_file,
    is_file_level,
    require_pristine,
)
from curamoir.git_guard import git, require_sandbox

#: How many referring files a SkippedRemedy names before "and N more".
_NAMED_REFERENCES = 5


@dataclass(frozen=True)
class SkippedRemedy:
    """A remedy not applied: the file `path`, the rule whose violation asked
    for it, and why. The violation stays unfixed and is reported as such."""

    path: str
    rule_id: str
    remedy: str
    reason: str


@dataclass(frozen=True)
class RemedyResult:
    """What apply_file_remedies did. `deleted` is sorted paths; `skipped`
    SkippedRemedies in work-list order; `remedied_rules` the ids of the rules
    that carry a remedy, whose file-level violations were the runner's to
    handle, applied or not."""

    deleted: tuple
    skipped: tuple
    remedied_rules: frozenset

    def for_agent(self, work):
        """`work` (a sweep.WorkList) without what the runner handled: every
        violation in a deleted file, and every file-level violation of a rule
        with a remedy. WHY the skipped ones too: the agent cannot apply a
        remedy (it may not delete or rename), so handing it one only invites
        an out-of-scope attempt; the runner reports them as unfixed."""
        deleted = set(self.deleted)
        items = tuple(item for item in work.items
                      if item.path not in deleted
                      and not (is_file_level(item) and item.rule_id in self.remedied_rules))
        return dataclasses.replace(work, items=items)


def apply_file_remedies(sandbox):
    """Apply every `remedy: delete_file` the base's work list asks for in
    `sandbox`, and return a RemedyResult (module docstring: when, what keeps a
    file, how it is deleted).

    Needs fix_scope.lint_sandbox to have run (the work list and the rules it
    recorded), and the sandbox's tree to be its base commit still: run it
    once, before the fix step. ScopeCheckError otherwise."""
    require_sandbox(sandbox, ScopeCheckError)
    if sandbox.work_list is None or sandbox.rules is None:
        raise ScopeCheckError("no work list or rules were linted in this sandbox at its base "
                              "(fix_scope.lint_sandbox), so there is nothing to remedy from")
    require_pristine(sandbox)
    # rules.REMEDIES is delete_file alone; a second remedy needs its own step.
    remedied = frozenset(rule.id for rule in sandbox.rules if rule.remedy == DELETE_FILE)
    deleted, skipped, seen = [], [], set()
    for item in sandbox.work_list.items:
        if item.rule_id not in remedied or item.path in seen:
            continue
        seen.add(item.path)
        if not is_file_level(item):
            reason = ("its violation is not file-level (the empty region at 1:1 a "
                      "`target: path` rule reports), so the file is not the violation")
        else:
            reason = _delete(sandbox, item.path)
        if reason is None:
            deleted.append(item.path)
        else:
            skipped.append(SkippedRemedy(item.path, item.rule_id, DELETE_FILE, reason))
    return RemedyResult(deleted=tuple(sorted(deleted)), skipped=tuple(skipped),
                        remedied_rules=remedied)


def _delete(sandbox, path):
    """Delete `path` in `sandbox` and return None, or return why not."""
    problem = _unsafe(path)
    if problem:
        return problem
    if is_dependency_file(path):
        return ("a dependency manifest or lockfile; the review refuses any change to "
                "one, so deleting it would discard the whole fix")
    target, problem = _regular_file(str(sandbox.path), path)
    if problem:
        return problem
    referrers = base_references(sandbox, path)
    if referrers:
        named = ", ".join(referrers[:_NAMED_REFERENCES])
        more = len(referrers) - _NAMED_REFERENCES
        return (f"the base still refers to it, from {named}"
                + (f" and {more} more" if more > 0 else "")
                + "; deleting a file something uses is not a mechanical fix")
    os.unlink(target)
    return None


def _unsafe(path):
    """Why `path` is not a plain repository-relative path, or None."""
    parts = path.split("/")
    if not path or "\0" in path or any(part in ("", ".", "..") for part in parts):
        return (f"{path!r} is not a plain repository-relative path (empty, absolute, or "
                "with an empty, `.` or `..` component)")
    if ".git" in parts:
        return f"{path!r} is inside a .git directory"
    return None


def _regular_file(root, path):
    """(absolute path, None) if `path` under `root` is a regular file reached
    through real directories only, else (None, why not)."""
    current = root
    parts = path.split("/")
    for index, part in enumerate(parts):
        current = os.path.join(current, part)
        where = "/".join(parts[:index + 1])
        try:
            mode = os.lstat(current).st_mode
        except OSError as error:
            return None, f"{where} cannot be read ({error.strerror})"
        if stat.S_ISLNK(mode):
            return None, f"{where} is a symlink, and a remedy follows none"
        last = index == len(parts) - 1
        if not last and not stat.S_ISDIR(mode):
            return None, f"{where} is not a directory"
        if last and not stat.S_ISREG(mode):
            return None, f"{where} is not a regular file"
    return current, None


def base_references(sandbox, path):
    """The other files at `sandbox`'s base commit that refer to `path`
    (module docstring, "What keeps a file"), sorted."""
    name = PurePosixPath(path).name
    stem = name.split(".", 1)[0] or name
    exclude = f":(exclude,literal){path}"
    found = set(_grep(sandbox, stem, *(f"*{suffix}" for suffix in
                                       PYTHON_SUFFIXES + SCRIPT_SUFFIXES), exclude))
    parent = PurePosixPath(path).parent.name
    if parent:
        found.update(_grep(sandbox, f"{parent}.{stem}", exclude))
    return tuple(sorted(found))


def _grep(sandbox, word, *pathspecs):
    """Paths at the base whose text holds `word` as a whole word (fixed
    string, binary files skipped). `-e` so a word starting with `-` is not
    an option."""
    listing = git(sandbox.path, "grep", "-I", "-l", "-z", "-w", "-F", "-e", word,
                  sandbox.base, "--", *pathspecs, ok=(0, 1))
    prefix = f"{sandbox.base}:"
    names = []
    for entry in listing.split(b"\0"):
        if entry:
            name = entry.decode("utf-8", "surrogateescape")
            names.append(name[len(prefix):] if name.startswith(prefix) else name)
    return names
