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
commit (hardened git, git_guard), never the work tree, binary-looking files
included (`git grep -a`: a NUL byte does not hide an import), and is
deliberately broad:

- any other Python or JS/TS source file containing the file's stem as a
  whole word — an import of the module or of its dotted name, a `from .
  import (` list across lines, `import_module("…handlers_v2")`, a JS/TS
  `./handlers_v2`, and also a comment or string that merely names it;
- any build, CI or config file (_CONFIG_FILES: TOML, INI/cfg, YAML,
  Makefiles, Dockerfiles, shell scripts, Procfile, HTML and Jinja
  templates, package.json, tsconfig/jsconfig, anything under `.github/`)
  containing the stem as a whole word. That one search covers the file name
  (`python scripts/migrate_v2.py`, `CMD ["python", "serve_v2.py"]`, since
  `.` ends a word), the bare stem (`gunicorn app_v2:app`) and an entry
  point (`run = "run_old:main"`);
- any other text file of any kind containing `<parent>.<stem>` (for
  `src/sample/web/handlers_v2.py`, `web.handlers_v2`): a module path in
  a config file of a kind not listed above, `python -m`, a server's
  `module:app`.

Prose is not searched for the stem: Markdown, reStructuredText and plain
text anywhere, and cúramóir's and beads' own files (`.curamoir/`, `.beads/`).
A file mentioned only by its path or name in a description (PLANTED.json,
`principles.md`'s `bad` example, a changelog) is not kept for it: a
description of a file is not a use of it.

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

- A reference the searches cannot see keeps nothing: an import whose
  module name is computed (`import_module(f"handlers_{v}")`), a top-level
  module (no parent directory) named in a file of a kind not searched for
  the stem (say a `.json` other than package.json/tsconfig), a config that
  names the file's directory some other way (`--app-dir`). The project's
  tests (E3-R4) are the backstop.
- The searches are word matches, not parses, so they err towards keeping:
  a comment saying "unlike handlers_v2" in another module, or a CI step
  naming a different file with the same stem, keeps the file.
- Only the `delete_file` remedy exists. A rename needs the new name, which
  is a judgement, and the agent may not rename either; such a violation is
  left for a person.

Syntax that imports on 3.9, like the rest of what `scripts/curamoir` runs.
"""

import dataclasses
import os
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

#: The source a remedy may delete, and in which other files the stem is a
#: reference (module docstring).
_SOURCE_SUFFIXES = PYTHON_SUFFIXES + SCRIPT_SUFFIXES

#: Directories whose files are tests, and test file names (lower-cased).
_TEST_DIRECTORIES = ("tests", "test", "__tests__", "spec")
_TEST_NAMES = ("test_*.py", "*_test.py", "conftest*.py", "*.test.*", "*.spec.*")

#: The files that name a module or script they run: build, CI and config
#: files (module docstring, "What keeps a file"). Glob pathspecs, so
#: `**/Makefile` finds a nested one too.
_CONFIG_FILES = tuple(f":(glob){pattern}" for pattern in (
    "**/*.toml", "**/*.cfg", "**/*.ini", "**/*.conf", "**/*.yml", "**/*.yaml",
    "**/Makefile", "**/makefile", "**/GNUmakefile", "**/*.mk", "**/justfile",
    "**/Justfile", "**/Dockerfile*", "**/Containerfile*", "**/*.dockerfile",
    "**/*.sh", "**/*.bash", "**/Procfile", "**/*.html", "**/*.htm", "**/*.j2",
    "**/*.jinja*", "**/package.json", "**/tsconfig*.json", "**/jsconfig*.json",
    ".github/**"))

#: Prose and cúramóir's or beads' own records: a mention there describes a
#: file, it does not use it.
_NOT_REFERENCES = tuple(f":(exclude,glob){pattern}" for pattern in (
    "**/*.md", "**/*.rst", "**/*.txt", ".curamoir/**", ".beads/**"))


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
    targets, deleted, skipped = [], [], []
    for path, items in by_path.items():
        # WHY any item, not the first: one file-level violation of a
        # deletable rule is the license, whatever else the file has.
        file_level = [item for item in items if is_file_level(item)]
        if not file_level:
            skipped.append(SkippedRemedy(
                path, items[0].rule_id, DELETE_FILE,
                "its violation is not file-level (the empty region at 1:1 a "
                "`target: path` rule reports), so the file is not the violation"))
            continue
        target, reason = _decide(sandbox, path)
        if reason is None:
            targets.append(target)
            deleted.append(path)
        else:
            skipped.append(SkippedRemedy(path, file_level[0].rule_id, DELETE_FILE, reason))
    # Phase two: the deletions, all decided on the untouched base.
    for target in targets:
        os.unlink(target)
    return RemedyResult(deleted=tuple(sorted(deleted)), skipped=tuple(skipped),
                        deletable_rules=deletable)


def _decide(sandbox, path):
    """(absolute path to delete, None) for `path` in `sandbox`, or (None,
    why it is kept). Deletes nothing."""
    problem = _unsafe(path) or _never_deleted(path)
    if problem:
        return None, problem
    target, problem = _untracked(sandbox, path) or _regular_file(str(sandbox.path), path)
    if problem:
        return None, problem
    referrers = _base_references(sandbox, path)
    if referrers:
        named = ", ".join(referrers[:_NAMED_REFERENCES])
        more = len(referrers) - _NAMED_REFERENCES
        return None, (f"the base still refers to it, from {named}"
                      + (f" and {more} more" if more > 0 else "")
                      + "; deleting a file something uses is not a mechanical fix")
    return target, None


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


def _untracked(sandbox, path):
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


def _base_references(sandbox, path):
    """The other files at `sandbox`'s base commit that refer to `path`
    (module docstring, "What keeps a file"), sorted."""
    name = PurePosixPath(path).name
    stem = name.split(".", 1)[0] or name
    exclude = f":(exclude,literal){path}"
    found = set(_grep(sandbox, stem, *(f"*{suffix}" for suffix in _SOURCE_SUFFIXES),
                      exclude))
    found.update(_grep(sandbox, stem, *_CONFIG_FILES, *_NOT_REFERENCES, exclude))
    parent = PurePosixPath(path).parent.name
    if parent:
        found.update(_grep(sandbox, f"{parent}.{stem}", exclude))
    return tuple(sorted(found))


def _grep(sandbox, word, *pathspecs):
    """Paths at the base whose content holds `word` as a whole word (fixed
    string). `-a`: a file git thinks binary (a NUL byte) is searched too,
    erring towards keeping. `-e` so a word starting with `-` is not an
    option."""
    listing = git(sandbox.path, "grep", "-a", "-l", "-z", "-w", "-F", "-e", word,
                  sandbox.base, "--", *pathspecs, ok=(0, 1))
    prefix = f"{sandbox.base}:"
    names = []
    for entry in listing.split(b"\0"):
        if entry:
            name = entry.decode("utf-8", "surrogateescape")
            names.append(name[len(prefix):] if name.startswith(prefix) else name)
    return names
