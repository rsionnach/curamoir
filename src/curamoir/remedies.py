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
deleted. fix_scope.verify_fix accepts a deleted file only for such a
violation of such a rule, whoever deleted it, so a file-level violation
licenses the runner's deletion and nothing else.

**When.** apply_file_remedies runs after fix_scope.lint_sandbox and BEFORE
the fix step, on the sandbox's pristine base (it refuses any other tree).
WHY before: the agent never sees the file, so it cannot spend turns on it
or edit a file about to vanish; the reference check reads exactly the tree
the work list was linted from; and the deletion stays apart from the
agent's diff (it happens before run_fix_step's snapshot, and the review
still sees it, because the review diffs against the base commit, not the
snapshot). The agent's prompt is built from RemedyResult.for_agent, which
leaves out what was handled here.

**The governing rule: never delete a file the base still uses.** A false
"used" only leaves a violation unfixed and reported; a false "unused"
breaks code, sinking the sweep at the test step (E3-R4) or, worse, passing
a suite that does not cover the user. So every doubt keeps the file.

**What is never deleted**, whatever refers to it (a SkippedRemedy with the
reason, for the runner to report the violation as unfixed):

- a path that is not plainly repository-relative (empty, absolute, `.`,
  `..`, empty or `.git` component), or that the base commit does not track
  as a regular file under exactly that spelling (`git ls-tree`): WHY — on a
  case-insensitive file system `A_V2.py` or `.GIT/x.py` would open another
  file than the one linted;
- a test file: a `tests`, `test`, `__tests__` or `spec` directory on the
  way, or a name like `test_*.py`, `*_test.py`, `conftest*.py`, `*.test.*`
  or `*.spec.*` (compared lower-cased). WHY — a test is run by a runner's
  discovery, which no reference names, and it is what exercises the code;
- anything but Python or JS/TS source (fix_scope's PYTHON_SUFFIXES and
  SCRIPT_SUFFIXES, exact case): a template, stylesheet, shell script or
  data file is used by an `{% include %}`, a `<link>`, a cron line or a
  path built at run time, none of which a word search can prove absent;
- a package's `__init__.py` or `__main__.py`: what refers to it is the
  package's name (`import tool_v2`, `python -m tool_v2`), and deleting
  either changes what the package is;
- a dependency manifest or lockfile: the review refuses any change to one
  (fix_scope, DEPENDENCY_CHANGE), so deleting it would discard the fix;
- a symlink, a directory, or a path through a symlinked directory (every
  directory on the way is lstat'ed; a remedy follows no link).

**What keeps a file.** Otherwise a file is deleted only if nothing at the
base refers to it. "Refers to" is read from git's objects at the base
commit (hardened git, git_guard), never the work tree, and is deliberately
broad: any other file at the base containing the file's stem as a whole
word (`handlers_v2` for `src/sample/web/handlers_v2.py`; `.` and `/` end a
word, so the file name, its path and a dotted module path `web.handlers_v2`
all hold it). That is an import, `from . import (` across lines,
`import_module("…handlers_v2")`, a JS/TS `./handlers_v2`, `python
scripts/migrate_v2.py` in a Makefile or a Jenkinsfile, `gunicorn
app_v2:app`, an `ExecStart=` line, a `.vscode/launch.json` `program`, a
Vue or Svelte component's import — and also a comment or string that only
names it.

WHY every file and not a list of the kinds that can refer: such a list
kept missing real uses (a `.vue`, `.pyx`, `.ipynb`, systemd unit, Jenkinsfile
or `.ps1`), and each miss deletes a file in use. So the search reads
everything but what is listed as not a reference:

- prose, case-insensitively: `*.md`, `*.markdown`, `*.rst`, `*.adoc`, and
  `CHANGELOG*` and `LICENSE*` files (_NOT_REFERENCES). Plain `*.txt` is
  read: it can be a crontab or a list of commands;
- cúramóir's and beads' own records, `.curamoir/` and `.beads/`;
- any file the project declares documentation with the git attribute
  `linguist-documentation` (set or `=true`; GitHub Linguist's attribute,
  read from the base's `.gitattributes`) that is not code: one whose
  suffix (lower-cased) is not Python, JS/TS, `.vue`, `.svelte`, `.pyx`,
  `.pxd` or `.ipynb` (_CODE_SUFFIXES). WHY: a record that names files
  without using them (the sample fixture's PLANTED.json) is the project's
  to declare; cúramóir knows no file names. WHY not code: the attribute
  only steers GitHub's language statistics, so `examples/**` is commonly
  marked with it, and an example script's import runs; a code file the
  attribute covers is searched like any other.

Binary-looking files are searched as text (`git grep -a`: a NUL byte does
not hide an import), and nothing is skipped as binary, so an image or
archive that happens to hold the stem keeps the file. A mention in what is
excluded keeps nothing: a description of a file is not a use of it.

The search is one `git grep` over the base for every candidate's stem at
once, its matches mapped back to the stems; a stem with a character other
than a letter, digit or `_` gets a search of its own (_base_references).

**Two phases.** Every decision, with every search, is made before anything
is deleted. WHY: a search that fails (a git timeout is a GitGuardError)
must leave the sandbox as it was; the exception propagates, and the runner,
which opened the sandbox as a context manager (git_guard.prepare_sandbox),
discards it unanswered. The deletion itself is a plain os.unlink of each
regular file decided on.

**Emptied directories are left.** Deleting `pkg/old_v2.py` may leave `pkg/`
empty in the sandbox. That is harmless: git tracks no directory, so the
diff the review reads and the patch carried to the PR are the same either
way, and removing directories would be one more thing to get wrong.

**Limits, stated rather than hidden.**

- A reference the search cannot see keeps nothing: an import whose
  module name is computed (`import_module(f"handlers_{v}")`), a use written
  only in a Markdown or reStructuredText file (an executed notebook in
  `.md`), a config that names the file's directory some other way
  (`--app-dir`). The project's tests (E3-R4) are the backstop.
- A Sphinx `.. automodule:: pkg.h_v2` in a `.rst` is prose to the search,
  so it does not keep `pkg/h_v2.py`; deleting the module may then make
  the docs build warn, or fail under `-W`, which the tests do not catch.
- The search is a word match, not a parse, so it errs towards keeping: a
  comment saying "unlike handlers_v2" in another module, a requirements or
  data file, or a CI step naming a different file with the same stem,
  keeps the file.
- Only the `delete_file` remedy exists. A rename needs the new name, which
  is a judgement, and the agent may not rename either; such a violation is
  left for a person.

Syntax that imports on 3.9, like the rest of what `scripts/curamoir` runs.
"""

import dataclasses
import os
import re
import stat
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import PurePosixPath

from curamoir.fix_scope import (
    PYTHON_SUFFIXES,
    SCRIPT_SUFFIXES,
    ScopeCheckError,
    is_dependency_file,
    is_file_level,
    require_pristine,
    rules_licensing_deletion,
)
from curamoir.git_guard import git, require_sandbox
from curamoir.rules import DELETE_FILE

#: How many referring files a SkippedRemedy names before "and N more".
_NAMED_REFERENCES = 5

#: The source a remedy may delete (module docstring, "What is never
#: deleted"). Not where references are looked for: that is every text file.
_SOURCE_SUFFIXES = PYTHON_SUFFIXES + SCRIPT_SUFFIXES

#: Directories whose files are tests, and test file names (lower-cased).
_TEST_DIRECTORIES = ("tests", "test", "__tests__", "spec")
_TEST_NAMES = ("test_*.py", "*_test.py", "conftest*.py", "*.test.*", "*.spec.*")

#: Subtracted from the reference search, which is otherwise every file at
#: the base: prose (a mention there describes a file, it does not use it)
#: and cúramóir's and beads' own records. Case-insensitive, so README.MD and
#: changelog.rst are prose too. WHY not *.txt: a .txt can hold a crontab or
#: a list of commands, and keeping a file wrongly costs less than deleting
#: one wrongly (module docstring, "The governing rule").
_NOT_REFERENCES = tuple(f":(exclude,glob,icase){pattern}" for pattern in (
    "**/*.md", "**/*.markdown", "**/*.rst", "**/*.adoc", "**/CHANGELOG*",
    "**/LICENSE*", ".curamoir/**", ".beads/**"))

#: The git attribute by which a project declares a file documentation
#: (GitHub Linguist's): a mention in such a file keeps nothing either,
#: unless the file is code (_CODE_SUFFIXES).
_DOCUMENTATION_ATTRIBUTE = "linguist-documentation"

#: Suffixes (compared lower-cased) of a file whose mention is a use even
#: when it is marked `linguist-documentation`: the source a remedy weighs,
#: plus code that imports it from another kind of file. WHY wider than
#: _SOURCE_SUFFIXES: this list decides what keeps a file, so it errs
#: towards keeping (module docstring, "The governing rule").
_CODE_SUFFIXES = frozenset(suffix.lower() for suffix in (
    *_SOURCE_SUFFIXES, ".vue", ".svelte", ".pyx", ".pxd", ".ipynb"))

#: How many paths one `git check-attr` is given. WHY: every path is an
#: argument, and a sweep's referrers could exceed the OS's argument-list
#: length (ARG_MAX) in one call.
_CHECK_ATTR_BATCH = 500

#: A stem made of git grep -w's word characters alone, which the one
#: batched search handles (_base_references).
_WORD_STEM = re.compile(r"[A-Za-z0-9_]+")


@dataclass
class _Decision:
    """Phase one's verdict on one path: delete `target` (the absolute path
    to unlink), or keep it for `reason` (`target` None). Mutable: the
    batched reference search may turn a deletion into a keep."""

    path: str
    rule_id: str
    target: object = None
    reason: str = None


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
    SkippedRemedies in work-list order; `deletable_rules` the ids of the
    rules that carry `remedy: delete_file` (fix_scope.rules_licensing_deletion),
    whose file-level violations were the runner's to handle, applied or not."""

    deleted: tuple
    skipped: tuple
    deletable_rules: frozenset

    def for_agent(self, work):
        """`work` (a sweep.WorkList) without what the runner handled: every
        violation in a deleted file, and every file-level violation of a rule
        with a remedy. WHY the skipped ones too: the agent cannot apply a
        remedy (it may not delete or rename), so handing it one only invites
        an out-of-scope attempt; the runner reports them as unfixed."""
        deleted = set(self.deleted)
        items = tuple(item for item in work.items
                      if item.path not in deleted
                      and not (is_file_level(item) and item.rule_id in self.deletable_rules))
        return dataclasses.replace(work, items=items)


def apply_file_remedies(sandbox):
    """Apply every `remedy: delete_file` the base's work list asks for in
    `sandbox`, and return a RemedyResult (module docstring: what is never
    deleted, what keeps a file, the two phases).

    Needs fix_scope.lint_sandbox to have run (the work list and the rules it
    recorded), and the sandbox's tree to be its base commit still: run it
    once, before the fix step. ScopeCheckError otherwise; GitGuardError if a
    search fails, with nothing deleted."""
    require_sandbox(sandbox, ScopeCheckError)
    if sandbox.work_list is None or sandbox.rules is None:
        raise ScopeCheckError("no work list or rules were linted in this sandbox at its base "
                              "(fix_scope.lint_sandbox), so there is nothing to remedy from")
    require_pristine(sandbox)
    # rules.REMEDIES is delete_file alone; a second remedy needs its own step.
    deletable = rules_licensing_deletion(sandbox.rules)
    by_path = {}
    for item in sandbox.work_list.items:
        if item.rule_id in deletable:
            by_path.setdefault(item.path, []).append(item)
    # Phase one: every decision, every search; nothing is touched.
    decisions = []  # _Decision, in work-list order
    for path, items in by_path.items():
        # WHY any item, not the first: one file-level violation of a
        # deletable rule is the license, whatever else the file has.
        file_level = [item for item in items if is_file_level(item)]
        if not file_level:
            decisions.append(_Decision(
                path, items[0].rule_id,
                reason="its violation is not file-level (the empty region at 1:1 a "
                       "`target: path` rule reports), so the file is not the violation"))
            continue
        target, reason = _check(sandbox, path)
        decisions.append(_Decision(path, file_level[0].rule_id, target, reason))
    # WHY one search for every candidate, not one per file: a sweep can hold
    # a hundred variants, and a search of the whole base each is slow.
    referrers = _base_references(sandbox, [d.path for d in decisions if d.target is not None])
    for decision in decisions:
        if referrers.get(decision.path):
            decision.target, decision.reason = None, _still_used(referrers[decision.path])
    deleted = [d.path for d in decisions if d.target is not None]
    skipped = [SkippedRemedy(d.path, d.rule_id, DELETE_FILE, d.reason)
               for d in decisions if d.target is None]
    # Phase two: the deletions, all decided on the untouched base.
    for decision in decisions:
        if decision.target is not None:
            os.unlink(decision.target)
    return RemedyResult(deleted=tuple(sorted(deleted)), skipped=tuple(skipped),
                        deletable_rules=deletable)


def _check(sandbox, path):
    """(absolute path to delete, None) for `path` in `sandbox` if nothing
    but a reference could keep it, or (None, why it is kept). Searches for
    no reference and deletes nothing."""
    problem = _unsafe(path) or _never_deleted(path)
    if problem:
        return None, problem
    # _not_tracked_at_base is None when the base tracks it, else (None, why).
    return _not_tracked_at_base(sandbox, path) or _regular_file(str(sandbox.path), path)


def _still_used(referrers):
    named = ", ".join(referrers[:_NAMED_REFERENCES])
    more = len(referrers) - _NAMED_REFERENCES
    return (f"the base still refers to it, from {named}"
            + (f" and {more} more" if more > 0 else "")
            + "; deleting a file something uses is not a mechanical fix")


def _unsafe(path):
    """Why `path` is not a plain repository-relative path, or None."""
    parts = path.split("/")
    if not path or "\0" in path or any(part in ("", ".", "..") for part in parts):
        return (f"{path!r} is not a plain repository-relative path (empty, absolute, or "
                "with an empty, `.` or `..` component)")
    if ".git" in parts:
        return f"{path!r} is inside a .git directory"
    return None


def _never_deleted(path):
    """Why `path` is of a kind a remedy never deletes (module docstring), or
    None."""
    pure = PurePosixPath(path)
    name = pure.name
    lowered = [part.lower() for part in pure.parts]
    if name in ("__init__.py", "__main__.py"):
        return (f"a package's {name}: what refers to it is the package's name, and "
                "deleting it changes what the package is")
    if any(part in _TEST_DIRECTORIES for part in lowered[:-1]) \
            or any(fnmatchcase(lowered[-1], pattern) for pattern in _TEST_NAMES):
        return ("a test file: a test runner finds it by discovery, which no reference "
                "names, and it is what exercises the code")
    if is_dependency_file(path):
        return ("a dependency manifest or lockfile; the review refuses any change to "
                "one, so deleting it would discard the whole fix")
    if pure.suffix not in _SOURCE_SUFFIXES:
        return ("not Python or JS/TS source: a template, stylesheet, script or data file "
                "is used in ways a search for its name cannot prove absent")
    return None


def _not_tracked_at_base(sandbox, path):
    """(None, why) unless the base commit has a regular file at exactly
    `path`, read from its tree byte for byte (no case folding, no pathspec
    magic); None if it has."""
    listing = git(sandbox.path, "ls-tree", "-z", sandbox.base, "--", path)
    for entry in listing.split(b"\0"):
        meta, _, name = entry.partition(b"\t")
        if name.decode("utf-8", "surrogateescape") != path:
            continue
        mode = meta.split(b" ")[0]
        if mode == b"120000":
            return None, f"{path} is a symlink at the base, and a remedy follows none"
        if mode in (b"100644", b"100755"):
            return None
        break
    return None, (f"{path} is not a regular file the base commit tracks under exactly "
                  "that name, so it is not the file that was linted")


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


def _stem(path):
    """The file name before its first `.` (`handlers_v2` for `web/handlers_v2.py`)."""
    name = PurePosixPath(path).name
    return name.split(".", 1)[0] or name


def _base_references(sandbox, paths):
    """{path: the other files at `sandbox`'s base commit that refer to it,
    sorted} for each of `paths` (module docstring, "What keeps a file")."""
    by_stem = {}
    for path in paths:
        by_stem.setdefault(_stem(path), []).append(path)
    hits = {stem: set() for stem in by_stem}
    batched = [stem for stem in by_stem if _WORD_STEM.fullmatch(stem)]
    # WHY batching only word stems: two whole-word matches of word-only
    # strings are maximal runs of word characters, so they never partly
    # overlap and `-o` reports each. A stem like `d-x_v2` can overlap
    # another's match (`x_v2`) and be swallowed, so it gets its own search.
    for referrer, word in _grep_words(sandbox, batched):
        hits[word].add(referrer)
    for stem in by_stem:
        if stem not in batched:
            hits[stem].update(_grep(sandbox, stem))
    documentation = _documentation(sandbox, set().union(*hits.values()))
    return {path: tuple(sorted(hits[_stem(path)] - documentation - {path})) for path in paths}


def _grep_words(sandbox, words):
    """(path, word) for each of `words` that a file at the base holds as a
    whole word, in one search of every file but _NOT_REFERENCES. `-a`: a
    file git thinks binary (a NUL byte) is searched as text too, erring
    towards keeping; nothing is skipped as binary. Fixed strings, each after
    `-e`, so a word starting with `-` is not an option."""
    if not words:
        return []
    patterns = [arg for word in words for arg in ("-e", word)]
    listing = git(sandbox.path, "grep", "-a", "-o", "-z", "-w", "-F", *patterns,
                  sandbox.base, "--", *_NOT_REFERENCES, ok=(0, 1))
    # Each match is `<base>:<path>\0<word>\n`; a path may hold a newline
    # but no NUL, and a word holds neither, so read NUL first, then newline.
    found, position = set(), 0
    while position < len(listing):
        nul = listing.index(b"\0", position)
        end = listing.index(b"\n", nul)
        found.add((_unprefix(sandbox, listing[position:nul]),
                   listing[nul + 1:end].decode("utf-8", "surrogateescape")))
        position = end + 1
    return sorted(found)


def _grep(sandbox, word):
    """Paths at the base, but _NOT_REFERENCES, whose content holds `word` as
    a whole word (fixed string; `-a` and `-e` as in _grep_words)."""
    listing = git(sandbox.path, "grep", "-a", "-l", "-z", "-w", "-F", "-e", word,
                  sandbox.base, "--", *_NOT_REFERENCES, ok=(0, 1))
    return [_unprefix(sandbox, entry) for entry in listing.split(b"\0") if entry]


def _unprefix(sandbox, entry):
    """A `git grep <base>` path entry decoded, without its `<base>:` prefix."""
    name = entry.decode("utf-8", "surrogateescape")
    prefix = f"{sandbox.base}:"
    return name[len(prefix):] if name.startswith(prefix) else name


def _documentation(sandbox, paths):
    """Those of `paths` that are not code (_CODE_SUFFIXES) and that the
    project marks `linguist-documentation` (set or `=true`). Read from the
    index (`--cached`): require_pristine has made it the base commit's
    tree, and git 2.39 has no `check-attr --source`."""
    # WHY code is never documentation here: an example script marked so
    # for GitHub's statistics still imports what it names.
    paths = sorted(path for path in paths
                   if PurePosixPath(path).suffix.lower() not in _CODE_SUFFIXES)
    marked = set()
    for start in range(0, len(paths), _CHECK_ATTR_BATCH):
        listing = git(sandbox.path, "check-attr", "--cached", "-z", _DOCUMENTATION_ATTRIBUTE,
                      "--", *paths[start:start + _CHECK_ATTR_BATCH])
        fields = listing.split(b"\0")
        for index in range(0, len(fields) - 2, 3):
            if fields[index + 2] in (b"set", b"true"):
                marked.add(fields[index].decode("utf-8", "surrogateescape"))
    return marked
