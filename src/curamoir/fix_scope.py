"""E3-R2's second half: the PR review step verifies a sweep fix's scope.

E3-R2: "Fixes MUST be scoped: only files with violations; no refactors beyond
the violation; no new dependencies; no signature changes without an explicit
principle permitting it. The skill prompt MUST state these limits and the PR
review step MUST verify them."

The prompt states them (src/curamoir/fix_step.py, SCOPE_LIMITS). This module
is the verification, and it is written so that it does not need the prompt to
have been obeyed, read, or even sent: it compares the tree the fix agent left
behind with the commit the lint ran on, and the work list that commit
produced, and nothing else. E0-R1's "enforced in code, not prompt" applied to
the sweep — a cheap-tier model that ignores every word of its instructions
produces a verdict that says so, with file and line, and the runner opens no
PR from it.

Two layers, so the rules can be tested without git:

- `check_scope(changes, violations, ...)`: the pure core. It takes each
  changed file's before and after text and the violations, and returns a
  `ScopeVerdict`: every breach, each with a kind (the KINDS below), a path,
  a line where there is one, and a sentence saying what was seen.
- `verify_fix(repo, base_ref, work_list, principles)`: the git adapter. It
  reads what changed between `base_ref` and the working tree (tracked changes
  with `git diff --raw`, hunks with `git diff -U0`, untracked files with
  `git ls-files --others --exclude-standard`), and what the repository
  already imports at `base_ref`, then calls the core.

**The rules, and why each is drawn where it is.**

- *Files.* Every changed path must be one of the work list's `paths`. A new
  file is never in scope: the lint ran on the base commit, so a file that did
  not exist there has no violation, and E3-R2 says "only files with
  violations". A deleted file is out of scope too, with one exception below.
  Renames are read with `--no-renames`, so a rename is a deletion and an
  addition, and is refused as both.
- *Deleting a file.* Allowed only when the file carries a file-level
  violation: the lint reports a `target: path` rule's match (a name like
  `handlers_v2.py`) as the empty region at 1:1 (docs/curamoir-lint.md), and
  for such a violation the file's existence is the violation, so removing it
  is the fix rather than a refactor.
- *Dependencies.* Any change to a dependency manifest or lockfile (by name:
  DEPENDENCY_FILES) is refused, whatever else is in it and even when the
  file is on the work list. A section-aware check — "pyproject.toml may
  change, just not its dependency tables" — would be a deny-list over an
  open set (project.dependencies, optional-dependencies, dependency-groups,
  build-system.requires, tool.poetry.group.*, tool.uv.sources, hatch envs,
  …): it passes the first table it forgot. Refusing the whole file costs a
  fix the lint's code rules practically never ask for. Separately, a newly
  imported top-level module (Python `import`, JS/TS `import`/`require`) that
  is not in the standard library, not local to the repository and not
  imported anywhere in it at the base commit is a new dependency in all but
  the manifest, and is refused as one.
- *Refactors.* Every hunk (git's `-U0` hunks: the smallest changed regions)
  must touch, or lie within WINDOW lines of, a violation in the same file.
  The one other hunk allowed is an insertion into the module's header — the
  stretch before its first function or class — that adds only imports and
  module-level assignments of names the module does not already bind, to a
  literal or a `logging.getLogger(...)` call (_header_value has the list):
  the bundled no-print rule's own fix is "logger = logging.getLogger(__name__)
  at module level", which lands far from the `print` it replaces, while an
  inserted `TAX_RATE = 0.3` would change every later use of TAX_RATE, and an
  inserted `_boot = __import__("os").system(...)` would run on every import.
- *Signatures.* For Python, both versions are parsed and every function's
  and class's signature (arguments, defaults, annotations, return
  annotation, async-ness, decorators; a class's bases, keywords and
  decorators) compared by qualified name: one changed, removed or added is a
  signature change. It is allowed only where a principle says `permits:
  signature_change` (E2-R1's format, src/curamoir/principles.py) and a
  violation of that principle lies within the definition (± WINDOW). For
  JavaScript and TypeScript there is no parser here, so any changed line that
  looks like a declaration (DECLARATION) counts as one — conservative, and
  blind to a parameter changed on a line of its own (see Limits).

**WINDOW = 3.** A mechanical fix sometimes has to touch a line just above
the violation: chaining a cause (`raise X(...) from error`) needs the
`except` above it to bind `as error`, one to three lines up in ordinary code.
Three lines covers that, and is still small enough that rewriting the rest
of a function to "fix" one line in it is refused.

**Limits, stated rather than hidden.**

- The verdict trusts that the work list was computed on `base_ref` with a
  clean tree, so its line numbers are the base commit's. The runner must
  lint the commit it branches from.
- Files git ignores are not seen: a fix that writes only into an ignored path
  passes. The PR step (cm-fkd.4) must commit exactly `ScopeVerdict.paths`,
  never `git add -A`, so such a file cannot reach the PR either.
- Every changed path is checked, including ones cúramóir's own hooks write
  while the agent runs (a `Stop` hook's `.curamoir/handoff/latest.md`, a
  guard's ledger line). They are not in any work list, so the verdict refuses
  them; the runner must keep its own writes out of the fix step's tree.
- JS/TS signatures are matched by line shape, not parsed: a parameter edited
  on its own line inside a multi-line parameter list is not seen.
- New-import detection knows the standard library from the interpreter
  running this (`sys.stdlib_module_names`, 3.10+; on older interpreters every
  import not already in the repository is reported) and Node's built-ins
  from a list. A package that is declared in a manifest but imported nowhere
  yet is reported as new.
- Python's own semantics only: a change of behaviour inside a function body
  within the window is the violation's fix as far as this module can tell.
  Whether it is a correct fix is E3-R4's question (the tests).

Syntax that imports on 3.9, like sweep.py: the sweep reaches this through
`scripts/curamoir`, whose interpreter may be the fallback `python3`.
"""

import ast
import difflib
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Optional

#: Lines either side of a violation's span that a fix may touch.
WINDOW = 3

#: Breach kinds. Strings, because the ledger line and the PR body record them.
FILE_NOT_IN_WORK_LIST = "file_not_in_work_list"
FILE_ADDED = "file_added"
FILE_DELETED = "file_deleted"
DEPENDENCY_CHANGE = "dependency_change"
NEW_DEPENDENCY = "new_dependency"
OUTSIDE_VIOLATION = "outside_violation"
SIGNATURE_CHANGE = "signature_change"
UNVERIFIABLE = "unverifiable"

KINDS = (FILE_NOT_IN_WORK_LIST, FILE_ADDED, FILE_DELETED, DEPENDENCY_CHANGE,
         NEW_DEPENDENCY, OUTSIDE_VIOLATION, SIGNATURE_CHANGE, UNVERIFIABLE)

#: The principle permission that lifts SIGNATURE_CHANGE (principles.PERMISSIONS).
SIGNATURE_PERMISSION = "signature_change"

#: Change statuses.
ADDED = "added"
MODIFIED = "modified"
DELETED = "deleted"

#: Dependency manifests and lockfiles, by file name. A change to any of these
#: is DEPENDENCY_CHANGE (the module docstring says why the whole file).
DEPENDENCY_FILES = frozenset({
    # Python
    "pyproject.toml", "setup.py", "setup.cfg", "Pipfile", "Pipfile.lock",
    "poetry.lock", "uv.lock", "pdm.lock", "environment.yml", "environment.yaml",
    "conda-lock.yml",
    # JavaScript / TypeScript
    "package.json", "package-lock.json", "npm-shrinkwrap.json", "yarn.lock",
    "pnpm-lock.yaml", "pnpm-workspace.yaml", "bun.lock", "bun.lockb", "deno.json",
    "deno.jsonc", "deno.lock",
    # Others a polyglot repository may hold
    "Cargo.toml", "Cargo.lock", "go.mod", "go.sum", "Gemfile", "Gemfile.lock",
    "composer.json", "composer.lock",
})

#: requirements.txt, requirements-dev.txt, requirements/base.in, constraints.txt…
_REQUIREMENTS = re.compile(r"^(requirements|constraints)([-_.][^/]*)?\.(txt|in)$")

PYTHON_SUFFIXES = (".py", ".pyi")
SCRIPT_SUFFIXES = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")

#: A JS/TS line that declares something callable or a type: a changed line of
#: this shape is treated as a signature change. Control-flow keywords are
#: excluded from the method form, which would otherwise match `if (x) {`.
DECLARATION = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:abstract\s+)?(?:async\s+)?"
    r"(?:function\b|class\b|interface\b|type\s+\w+\s*(?:<[^=]*>)?\s*=|enum\b)"
    r"|^\s*(?:export\s+)?(?:const|let|var)\s+\w+\s*(?::[^=]+)?=\s*(?:async\s+)?"
    r"(?:function\b|\([^)]*\)\s*(?::[^=]+)?=>|\w+\s*=>)"
    r"|^\s*(?:(?:public|private|protected|static|readonly|async|override|get|set)\s+)*"
    r"(?!(?:if|for|while|switch|catch|return|function|with)\b)[\w$]+\s*(?:<[^>]*>)?"
    r"\s*\([^)]*\)\s*(?::\s*[^{;]+)?\{?\s*$"
)

_SCRIPT_IMPORT = re.compile(
    r"""(?:\bfrom\s*|\bimport\s*\(?\s*|\brequire\s*\(\s*|^\s*import\s+)['"]([^'"]+)['"]""")

#: Node's built-in modules; `node:`-prefixed specifiers are built-ins too.
NODE_BUILTINS = frozenset({
    "assert", "async_hooks", "buffer", "child_process", "cluster", "console",
    "constants", "crypto", "dgram", "diagnostics_channel", "dns", "domain",
    "events", "fs", "http", "http2", "https", "inspector", "module", "net", "os",
    "path", "perf_hooks", "process", "punycode", "querystring", "readline",
    "repl", "stream", "string_decoder", "sys", "test", "timers", "tls",
    "trace_events", "tty", "url", "util", "v8", "vm", "wasi", "worker_threads",
    "zlib",
})

_STDLIB = frozenset(getattr(sys, "stdlib_module_names", ()))


@dataclass(frozen=True)
class Hunk:
    """One changed region, in `git diff -U0`'s terms: `old_count` lines from
    `old_start` were replaced by `new_count` lines from `new_start`. A count
    of 0 is a pure insertion or deletion, and then the start is the line
    BEFORE it (0 for the top of the file), as git writes it."""

    old_start: int
    old_count: int
    new_start: int
    new_count: int


@dataclass(frozen=True)
class FileChange:
    """One changed path. `old_text` is None when the file was added and
    `new_text` None when it was deleted. `hunks` None means the core computes
    them (difflib); the git adapter passes git's own. `unverifiable` is set,
    with the reason, when the content cannot be checked (binary, not UTF-8, a
    symlink or submodule, a mode change)."""

    path: str
    status: str
    old_text: Optional[str] = None
    new_text: Optional[str] = None
    hunks: Optional[tuple] = None
    unverifiable: Optional[str] = None


@dataclass(frozen=True)
class Breach:
    """One way the fix went beyond E3-R2's limits. `line` is in the base
    commit's file where it names an old line, else the fixed file's (`detail`
    says which); None for a whole-file breach."""

    kind: str
    path: str
    line: Optional[int]
    detail: str

    def render(self):
        where = self.path if self.line is None else f"{self.path}:{self.line}"
        return f"{self.kind} {where}: {self.detail}"


@dataclass(frozen=True)
class ScopeVerdict:
    """The review step's answer. `paths` is every changed path, sorted: what a
    PR made from an accepted fix commits, and nothing else."""

    breaches: tuple
    paths: tuple

    @property
    def ok(self):
        return not self.breaches

    def kinds(self):
        """The breach kinds present, each once, in KINDS order."""
        present = {breach.kind for breach in self.breaches}
        return tuple(kind for kind in KINDS if kind in present)

    def render(self):
        if self.ok:
            return f"in scope: {len(self.paths)} file(s) changed, all within E3-R2's limits"
        lines = [f"out of scope: {len(self.breaches)} breach(es) of E3-R2"]
        lines.extend(f"- {breach.render()}" for breach in self.breaches)
        return "\n".join(lines)

    def as_dict(self):
        """For the ledger line and the PR body (cm-fkd.4, cm-fkd.6)."""
        return {
            "ok": self.ok,
            "paths": list(self.paths),
            "breaches": [
                {"kind": b.kind, "path": b.path, "line": b.line, "detail": b.detail}
                for b in self.breaches
            ],
        }


# --- the pure core ---------------------------------------------------------------

def check_scope(changes, violations, permitting=frozenset(), known_modules=frozenset(),
                window=WINDOW):
    """The verdict on `changes` (FileChanges) against `violations` (anything
    with path, line, col, end_line, end_col and principle_id: the work list's
    items). `permitting` is the ids of principles that permit signature
    changes; `known_modules` the top-level module and package names the
    repository already uses or contains, which are not new dependencies."""
    by_path = {}
    for violation in violations:
        by_path.setdefault(violation.path, []).append(violation)
    breaches = []
    for change in sorted(changes, key=lambda c: c.path):
        breaches.extend(_check_file(change, by_path.get(change.path, ()), permitting,
                                    known_modules, window))
    return ScopeVerdict(breaches=tuple(breaches),
                        paths=tuple(sorted({c.path for c in changes})))


def _check_file(change, violations, permitting, known_modules, window):
    path = change.path
    found = []
    if is_dependency_file(path):
        found.append(Breach(
            DEPENDENCY_CHANGE, path, None,
            "a dependency manifest or lockfile changed; a sweep fix adds, removes "
            "and re-pins no dependency"))
    if change.status == ADDED:
        found.append(Breach(
            FILE_ADDED, path, None,
            "a new file; the lint ran on the base commit, so no violation is in it"))
        return found
    if not violations:
        found.append(Breach(
            FILE_NOT_IN_WORK_LIST, path, None,
            "changed, but the work list has no violation in this file"))
        return found
    if change.unverifiable:
        found.append(Breach(UNVERIFIABLE, path, None, change.unverifiable))
        return found
    if change.status == DELETED:
        if not any(_is_file_level(v) for v in violations):
            found.append(Breach(
                FILE_DELETED, path, None,
                "deleted, but none of its violations is about the file itself "
                "(a `target: path` rule's empty region at 1:1)"))
        return found

    old, new = change.old_text, change.new_text
    hunks = change.hunks if change.hunks is not None else compute_hunks(old, new)
    old_lines, new_lines = old.splitlines(), new.splitlines()
    spans = [_span(v, window) for v in violations]
    python = path.endswith(PYTHON_SUFFIXES)
    script = path.endswith(SCRIPT_SUFFIXES)

    old_tree = new_tree = None
    if python:
        old_tree, new_tree, problem = _parse_both(old, new)
        if problem:
            found.append(Breach(UNVERIFIABLE, path, None, problem))
            return found

    header_end = _header_end(old_tree, old_lines, python)
    for hunk in hunks:
        if any(_overlaps(_hunk_range(hunk), span) for span in spans):
            continue
        added = new_lines[hunk.new_start - 1:hunk.new_start - 1 + hunk.new_count]
        if (hunk.old_count == 0 and hunk.old_start < header_end
                and _only_header_statements(added, python, old_tree)):
            continue
        found.append(Breach(
            OUTSIDE_VIOLATION, path, max(hunk.old_start, 1),
            f"{_describe(hunk, old_lines, added)} at base line {max(hunk.old_start, 1)}, "
            f"more than {window} line(s) from every violation in this file"))

    permitted_spans = [_span(v, window) for v in violations if v.principle_id in permitting]
    if python:
        found.extend(_python_signature_breaches(path, old_tree, new_tree, permitted_spans))
        found.extend(_new_import_breaches(path, _python_imports(old_tree),
                                          _python_imports(new_tree), known_modules,
                                          _STDLIB))
    elif script:
        found.extend(_script_signature_breaches(path, hunks, old_lines, new_lines,
                                                permitted_spans))
        found.extend(_new_import_breaches(path, _script_imports(old_lines),
                                          _script_imports(new_lines), known_modules,
                                          NODE_BUILTINS))
    return found


def is_dependency_file(path):
    """True for a dependency manifest or lockfile (DEPENDENCY_FILES, and the
    requirements/constraints family), by its name wherever it sits."""
    name = PurePosixPath(path).name
    return name in DEPENDENCY_FILES or bool(_REQUIREMENTS.match(name)) or (
        PurePosixPath(path).parent.name == "requirements" and name.endswith((".txt", ".in")))


def compute_hunks(old_text, new_text):
    """`-U0`-style hunks from difflib, for callers without git's. Not always
    git's minimal diff, so the adapter passes git's own."""
    matcher = difflib.SequenceMatcher(None, old_text.splitlines(), new_text.splitlines(),
                                      autojunk=False)
    hunks = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        old_count, new_count = i2 - i1, j2 - j1
        hunks.append(Hunk(i1 if old_count == 0 else i1 + 1, old_count,
                          j1 if new_count == 0 else j1 + 1, new_count))
    return tuple(hunks)


_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_hunks(diff_text):
    """The hunks of one file's `git diff -U0` output."""
    hunks = []
    for line in diff_text.splitlines():
        match = _HUNK_HEADER.match(line)
        if match:
            old_start, old_count, new_start, new_count = match.groups()
            hunks.append(Hunk(int(old_start), 1 if old_count is None else int(old_count),
                              int(new_start), 1 if new_count is None else int(new_count)))
    return tuple(hunks)


def _is_file_level(violation):
    return (violation.line, violation.col, violation.end_line, violation.end_col) == (1, 1, 1, 1)


def _span(violation, window):
    """The violation's base lines, widened by `window`. The end is exclusive at
    a column, so a match ending at column 1 of `end_line` ends on the line
    before it."""
    last = violation.end_line
    if violation.end_col == 1 and violation.end_line > violation.line:
        last -= 1
    return (violation.line - window, max(last, violation.line) + window)


def _hunk_range(hunk):
    """The base lines a hunk touches. A pure insertion after line n sits
    between n and n+1, so it is near whatever either of those is near."""
    if hunk.old_count == 0:
        return (hunk.old_start, hunk.old_start + 1)
    return (hunk.old_start, hunk.old_start + hunk.old_count - 1)


def _overlaps(first, second):
    return first[0] <= second[1] and second[0] <= first[1]


def _describe(hunk, old_lines, added):
    if hunk.old_count == 0:
        return f"{hunk.new_count} line(s) inserted"
    if hunk.new_count == 0:
        return f"{hunk.old_count} line(s) deleted"
    return f"{hunk.old_count} line(s) replaced by {hunk.new_count}"


def _parse_both(old, new):
    """(old tree, new tree, None), or (None, None, why it cannot be checked)."""
    try:
        old_tree = ast.parse(old)
    except (SyntaxError, ValueError) as error:
        return None, None, (f"the base version does not parse ({error.__class__.__name__}: "
                            f"{error}), so its signatures cannot be compared")
    try:
        new_tree = ast.parse(new)
    except (SyntaxError, ValueError) as error:
        return None, None, (f"the fixed version does not parse ({error.__class__.__name__}: "
                            f"{error})")
    return old_tree, new_tree, None


def _header_end(tree, lines, python):
    """The first base line that is past the module header: its first top-level
    function or class (decorators included) for Python, its first line that is
    not blank, a comment or an import for anything else. Insertions before it
    may add imports and module-level assignments."""
    if python:
        starts = [
            min([node.lineno] + [d.lineno for d in node.decorator_list])
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        ]
        return min(starts) if starts else len(lines) + 1
    for number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if stripped and not stripped.startswith(("//", "#", "/*", "*", "import ", "'use ",
                                                 '"use ')) and "require(" not in stripped:
            return number
    return len(lines) + 1


def _only_header_statements(added, python, old_tree=None):
    """True when the added lines are only imports and simple module-level
    assignments (and blank lines or comments): what a fix may add to a
    module's header.

    For Python, every name they bind must also be new to the module, unless
    the statement is an import the module already has, word for word: an
    inserted `TAX_RATE = 0.3` would otherwise pass as "an assignment in the
    header" while changing what every later use of TAX_RATE means.

    And an assignment's value must be one of _header_value's shapes. WHY:
    header code runs whenever the module is imported — the sweep's own test
    step included — and the window rule exists to keep a fix next to its
    violation. Without this, `_boot = __import__("os").system(...)` is "a
    module-level assignment of a new name" and lands anywhere in the header.
    Imports are not narrowed further here: what they may bring in is the
    new-dependency check's job, and running an existing module's top level is
    what importing it has always meant."""
    if python:
        try:
            tree = ast.parse("\n".join(added))
        except (SyntaxError, ValueError):
            return False
        bound, imports = _module_bindings(old_tree)
        loggers = _logging_aliases(old_tree) | _logging_aliases(tree)
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [(a.asname or a.name).split(".", 1)[0] for a in node.names]
                if ast.dump(node) not in imports and any(n in bound for n in names):
                    return False
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if not all(isinstance(t, ast.Name) and t.id not in bound for t in targets):
                    return False
                if node.value is not None and not _header_value(node.value, loggers):
                    return False
                # A module-level annotation is evaluated at import time too
                # (absent `from __future__ import annotations`), so it is held
                # to a type's spelling: `int`, `logging.Logger`.
                if isinstance(node, ast.AnnAssign) and not _dotted_name(node.annotation):
                    return False
            else:
                return False
        return True
    return all(not line.strip() or line.strip().startswith("//")
               or _SCRIPT_HEADER_LINE.match(line.strip()) for line in added)


#: A JS/TS header line a fix may add: a static `import` (with or without
#: bindings) or `require` of a quoted specifier, with nothing chained after
#: it. Text, not a parse: a multi-line `import {` falls to the window rule,
#: which is the safe direction. The specifier is checked for new dependencies
#: elsewhere (_SCRIPT_IMPORT).
_SCRIPT_HEADER_LINE = re.compile(
    r"""^(?:import\s+(?:type\s+)?(?:[\w$*{}\s,]+?\s+from\s*)?"""
    r"""|(?:(?:const|let|var)\s+(?:[\w$]+|\{[\w$\s,:]*\})\s*=\s*)?require\s*\(\s*)"""
    r"""(['"])[^'"]+\1\s*\)?\s*;?$""")

#: The literal types a header constant may hold.
_HEADER_LITERALS = (str, bytes, int, float, complex, bool, type(None))


def _header_value(node, loggers):
    """Whether `node`, the right-hand side of an assignment inserted into a
    module header, is one of the shapes a header fix needs. The allowlist,
    and the fix each entry exists for:

    - a literal — str, bytes, number (optionally negated), bool, None — or a
      tuple or list of them: a named constant (`TIMEOUT = 30`, `__all__ =
      [...]`) is a header's other ordinary content, and a literal runs
      nothing;
    - `<logging>.getLogger()`, with no argument, `__name__` or one string
      literal, where `<logging>` is a name the module (old or inserted) binds
      to the standard `logging` module: the bundled no-print rule's own fix,
      "logger = logging.getLogger(__name__) at module level"
      (examples/python/no-print.yaml).

    Everything else — any other call, attribute chains, lambdas,
    comprehensions, f-strings, walrus, names — is refused here, so such a
    line is held to the window rule like any other change."""
    if isinstance(node, (ast.Tuple, ast.List)):
        return all(_header_literal(element) for element in node.elts)
    if _header_literal(node):
        return True
    if not (isinstance(node, ast.Call) and not node.keywords and len(node.args) <= 1
            and isinstance(node.func, ast.Attribute) and node.func.attr == "getLogger"
            and isinstance(node.func.value, ast.Name) and node.func.value.id in loggers):
        return False
    if not node.args:
        return True
    arg = node.args[0]
    return ((isinstance(arg, ast.Name) and arg.id == "__name__")
            or (isinstance(arg, ast.Constant) and isinstance(arg.value, str)))


def _header_literal(node):
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        node = node.operand
        return isinstance(node, ast.Constant) and type(node.value) in (int, float, complex)
    return isinstance(node, ast.Constant) and type(node.value) in _HEADER_LITERALS


def _dotted_name(node):
    """`int`, `logging.Logger`: a name or an attribute of a name, nothing else."""
    if isinstance(node, ast.Attribute):
        node = node.value
    return isinstance(node, ast.Name)


def _logging_aliases(tree):
    """The names `tree`'s top-level `import logging [as x]` statements bind to
    the standard logging module."""
    if tree is None:
        return set()
    return {alias.asname or "logging"
            for node in tree.body if isinstance(node, ast.Import)
            for alias in node.names if alias.name == "logging"}


def _module_bindings(tree):
    """(names bound at the module's top level, ast dumps of its top-level
    imports). Bindings under a top-level `if`/`try` count: they may run."""
    bound, imports = set(), set()
    if tree is None:
        return bound, imports
    pending = list(tree.body)
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            imports.add(ast.dump(node))
            bound.update((a.asname or a.name).split(".", 1)[0] for a in node.names)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                bound.update(n.id for n in ast.walk(target) if isinstance(n, ast.Name))
        elif isinstance(node, (ast.If, ast.Try)) or type(node).__name__ == "TryStar":
            pending.extend(node.body)
            pending.extend(getattr(node, "orelse", ()))
            pending.extend(getattr(node, "finalbody", ()))
            for handler in getattr(node, "handlers", ()):
                pending.extend(handler.body)
    return bound, imports


# --- signatures --------------------------------------------------------------------

def _python_definitions(tree):
    """Qualified name -> (signatures, span) for every function and class, at
    any depth, including those under a module-level `if`/`try`. Signatures are
    a tuple because one name may be defined more than once (an `@overload`
    set, a property and its setter); line numbers are not part of them."""
    found = {}

    def visit(body, prefix):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = prefix + node.name
                signature = (type(node).__name__, ast.dump(node.args),
                             ast.dump(node.returns) if node.returns else None,
                             tuple(ast.dump(d) for d in node.decorator_list))
                _add(found, name, signature, node)
                visit(node.body, name + ".")
            elif isinstance(node, ast.ClassDef):
                name = prefix + node.name
                signature = ("ClassDef", tuple(ast.dump(b) for b in node.bases),
                             tuple(ast.dump(k) for k in node.keywords),
                             tuple(ast.dump(d) for d in node.decorator_list))
                _add(found, name, signature, node)
                visit(node.body, name + ".")
            elif isinstance(node, (ast.If, ast.For, ast.While, ast.With, ast.AsyncWith,
                                   ast.AsyncFor)):
                visit(node.body, prefix)
                visit(node.orelse if hasattr(node, "orelse") else (), prefix)
            elif isinstance(node, ast.Try) or type(node).__name__ == "TryStar":
                visit(node.body, prefix)
                for handler in node.handlers:
                    visit(handler.body, prefix)
                visit(node.orelse, prefix)
                visit(node.finalbody, prefix)

    visit(tree.body, "")
    return found


def _add(found, name, signature, node):
    start = min([node.lineno] + [d.lineno for d in node.decorator_list])
    end = getattr(node, "end_lineno", None) or node.lineno
    signatures, span = found.get(name, ((), (start, end)))
    found[name] = ((*signatures, signature), (min(span[0], start), max(span[1], end)))


def _python_signature_breaches(path, old_tree, new_tree, permitted_spans):
    before, after = _python_definitions(old_tree), _python_definitions(new_tree)
    found = []
    for name in sorted(set(before) | set(after)):
        old, new = before.get(name), after.get(name)
        if old is not None and new is not None and old[0] == new[0]:
            continue
        span = (old or new)[1]
        if any(_overlaps(span, allowed) for allowed in permitted_spans):
            continue
        if new is None:
            what, line, side = "removed", old[1][0], "base"
        elif old is None:
            what, line, side = "added", new[1][0], "fixed"
        else:
            what, line, side = "changed its signature", old[1][0], "base"
        found.append(Breach(
            SIGNATURE_CHANGE, path, line,
            f"`{name}` {what} ({side} line {line}); no principle with a violation "
            f"there permits signature changes"))
    return found


def _script_signature_breaches(path, hunks, old_lines, new_lines, permitted_spans):
    found = []
    for hunk in hunks:
        if any(_overlaps(_hunk_range(hunk), allowed) for allowed in permitted_spans):
            continue
        removed = old_lines[hunk.old_start - 1:hunk.old_start - 1 + hunk.old_count]
        added = new_lines[hunk.new_start - 1:hunk.new_start - 1 + hunk.new_count]
        declared = [line for line in removed + added if DECLARATION.match(line)]
        if declared:
            found.append(Breach(
                SIGNATURE_CHANGE, path, max(hunk.old_start, 1),
                f"a declaration line changed near base line {max(hunk.old_start, 1)} "
                f"({declared[0].strip()[:80]!r}); JS/TS declarations are matched by "
                "line shape, and no principle with a violation there permits it"))
    return found


# --- imports ------------------------------------------------------------------------

def _python_imports(tree):
    """Top-level module name -> first line importing it, for absolute imports
    anywhere in the module (function-local ones too)."""
    found = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module]
        else:
            continue
        for name in names:
            top = name.split(".", 1)[0]
            if top not in found or node.lineno < found[top]:
                found[top] = node.lineno
    return found


def script_package(specifier):
    """The package a JS/TS import specifier names, or None for a relative or
    absolute path. `@scope/name/sub` is `@scope/name`; `node:fs` is `fs`."""
    if specifier.startswith((".", "/")):
        return None
    if specifier.startswith("node:"):
        return specifier[5:].split("/", 1)[0]
    parts = specifier.split("/")
    if specifier.startswith("@") and len(parts) > 1:
        return "/".join(parts[:2])
    return parts[0]


def _script_imports(lines):
    found = {}
    for number, line in enumerate(lines, start=1):
        for specifier in _SCRIPT_IMPORT.findall(line):
            package = script_package(specifier)
            if package and package not in found:
                found[package] = number
    return found


def _new_import_breaches(path, before, after, known_modules, builtins):
    found = []
    for name in sorted(set(after) - set(before)):
        if name in builtins or name in known_modules:
            continue
        found.append(Breach(
            NEW_DEPENDENCY, path, after[name],
            f"imports `{name}` (fixed line {after[name]}), which is not in the "
            "standard library, not part of this repository and not imported "
            "anywhere in it at the base commit"))
    return found


# --- the git adapter ---------------------------------------------------------------

def verify_fix(repo, base_ref, work_list, principles, window=WINDOW):
    """E3-R2's review step: the verdict on everything that changed between
    `base_ref` and `repo`'s working tree (staged, unstaged and untracked),
    against `work_list` (sweep.WorkList, computed on `base_ref`) and
    `principles` (a principles.PrinciplesFile, for `permits`)."""
    permitting = frozenset(
        p.id for p in principles.principles if SIGNATURE_PERMISSION in p.permits)
    return check_scope(changes_since(repo, base_ref), work_list.items,
                       permitting=permitting,
                       known_modules=known_modules(repo, base_ref), window=window)


def _git(repo, *args, ok=(0,)):
    result = subprocess.run(["git", "-c", "core.quotepath=off", *args], cwd=repo,
                            capture_output=True)
    if result.returncode not in ok:
        raise ScopeCheckError(
            f"git {' '.join(args)} failed ({result.returncode}): "
            f"{result.stderr.decode('utf-8', 'backslashreplace').strip()}")
    return result.stdout


class ScopeCheckError(Exception):
    """git could not say what changed, so no verdict can be given. The runner
    must treat this as a refusal: an unverified fix is not a fix."""


def changes_since(repo, base_ref):
    """Every path that differs between `base_ref` and the working tree, as
    FileChanges with git's own `-U0` hunks."""
    repo = Path(repo)
    raw = _git(repo, "diff", "--raw", "-z", "--no-renames", "--no-ext-diff",
               "--ignore-submodules=none", base_ref, "--")
    changes = []
    fields = raw.split(b"\0")
    index = 0
    while index + 1 < len(fields) and fields[index].startswith(b":"):
        meta = fields[index][1:].decode("ascii").split()
        path = fields[index + 1].decode("utf-8", "surrogateescape")
        index += 2
        changes.append(_tracked_change(repo, base_ref, path, meta))
    untracked = _git(repo, "ls-files", "--others", "--exclude-standard", "-z")
    for entry in untracked.split(b"\0"):
        if entry:
            path = entry.decode("utf-8", "surrogateescape")
            changes.append(FileChange(path, ADDED, None, _read_text(repo / path)[0]))
    return tuple(changes)


def _tracked_change(repo, base_ref, path, meta):
    old_mode, new_mode, status = meta[0], meta[1], meta[4]
    kind = {"A": ADDED, "D": DELETED}.get(status[0], MODIFIED)
    if kind == ADDED:
        return FileChange(path, ADDED, None, _read_text(repo / path)[0])
    if kind == DELETED:
        return FileChange(path, DELETED, _blob_text(repo, base_ref, path)[0], None)
    unusual = {"120000": "a symlink", "160000": "a submodule"}
    for mode in (old_mode, new_mode):
        if mode in unusual:
            return FileChange(path, MODIFIED, unverifiable=(
                f"{unusual[mode]} changed; only regular text files can be checked"))
    if old_mode != new_mode:
        return FileChange(path, MODIFIED, unverifiable=(
            f"its mode changed ({old_mode} to {new_mode}); a fix edits text, not modes"))
    old, old_problem = _blob_text(repo, base_ref, path)
    new, new_problem = _read_text(repo / path)
    if old_problem or new_problem:
        return FileChange(path, MODIFIED, unverifiable=old_problem or new_problem)
    diff = _git(repo, "diff", "-U0", "--no-color", "--no-ext-diff", "--no-textconv",
                "--no-renames", base_ref, "--", f":(literal){path}")
    return FileChange(path, MODIFIED, old, new,
                      hunks=parse_hunks(diff.decode("utf-8", "surrogateescape")))


def _decode(data, where):
    if b"\0" in data:
        return None, f"{where} is binary; only text files can be checked"
    try:
        return data.decode("utf-8"), None
    except UnicodeDecodeError as error:
        return None, f"{where} is not UTF-8 ({error}); only text files can be checked"


def _blob_text(repo, base_ref, path):
    return _decode(_git(repo, "cat-file", "blob", f"{base_ref}:{path}"), "the base version")


def _read_text(path):
    try:
        return _decode(Path(path).read_bytes(), "the fixed version")
    except OSError as error:
        return None, f"the fixed version cannot be read: {error}"


_PY_IMPORT_LINE = re.compile(r"^\s*(?:import\s+([\w., ]+)|from\s+(\w[\w.]*)\s+import\b)")


def known_modules(repo, base_ref):
    """Top-level module and package names that are not a new dependency at
    `base_ref`: every file and directory name in the tree (a local module or
    package, wherever it sits), every module a Python file imports, every
    package a JS/TS file imports, and package.json's declared packages."""
    known = set()
    listing = _git(repo, "ls-tree", "-r", "-z", "--name-only", base_ref)
    for entry in listing.split(b"\0"):
        for part in entry.decode("utf-8", "surrogateescape").split("/"):
            if part:
                known.add(part.split(".", 1)[0] if "." in part[1:] else part)
    python = _git(repo, "grep", "-h", "-I", "-E", r"^\s*(import|from)\s", base_ref,
                  "--", "*.py", "*.pyi", ok=(0, 1))
    for line in python.decode("utf-8", "surrogateescape").splitlines():
        match = _PY_IMPORT_LINE.match(line)
        if not match:
            continue
        names = match.group(1).split(",") if match.group(1) else [match.group(2)]
        for name in names:
            name = name.strip().split(" ", 1)[0]
            if name:
                known.add(name.split(".", 1)[0])
    scripts = _git(repo, "grep", "-h", "-I", "-E", r"(from|import|require)\s*\(?\s*['\"]",
                   base_ref, "--", *(f"*{suffix}" for suffix in SCRIPT_SUFFIXES), ok=(0, 1))
    for line in scripts.decode("utf-8", "surrogateescape").splitlines():
        for specifier in _SCRIPT_IMPORT.findall(line):
            package = script_package(specifier)
            if package:
                known.add(package)
    known.update(_declared_packages(repo, base_ref))
    return frozenset(known)


def _declared_packages(repo, base_ref):
    raw = _git(repo, "cat-file", "-p", f"{base_ref}:package.json", ok=(0, 128))
    try:
        manifest = json.loads(raw.decode("utf-8")) if raw else {}
    except ValueError:
        return ()
    if not isinstance(manifest, dict):
        return ()
    names = []
    for table in ("dependencies", "devDependencies", "peerDependencies",
                  "optionalDependencies"):
        if isinstance(manifest.get(table), dict):
            names.extend(manifest[table])
    return names
