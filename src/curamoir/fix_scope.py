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
- `verify_fix(sandbox, git_state=...)`: the git adapter. The fix step runs
  in a sandbox, a disposable clone of the base commit (git_guard), never in
  the user's checkout. verify_fix first holds the sandbox's git files and
  work tree to the snapshot taken before the fix step (git_guard), then
  reads what changed between the base commit and the work tree (tracked
  changes with `git diff --raw`, hunks with `git diff -U0`, untracked files
  with `git ls-files --others`, nothing excluded), the sandbox's work list
  and rules (lint_sandbox), the principles at the base commit, and what the
  repository already imports there, and calls the core.

**What the runner (cm-fkd.4) must do.**

1. Make a sandbox: `git_guard.prepare_sandbox(repo, base_ref)`, and use it
   as a context manager so it is removed whatever happens.
2. Lint it before anything edits it: `lint_sandbox(sandbox)`, which records
   the work list of the base, and the rules the lint ran, on the sandbox.
3. Apply the file-level remedies, still before anything else edits it:
   `remedies.apply_file_remedies(sandbox)`, which deletes each file whose
   violation's rule says `remedy: delete_file` and nothing at the base
   refers to (the agent has no deletion tool). Build the prompt from
   `result.for_agent(sandbox.work_list)`, which leaves out what the runner
   handled, and from `base_principles(sandbox)`; report `result.skipped` as
   unfixed, with each reason.
4. Run the fix step in it: `fix_step.run_fix_step(sandbox, prompt)`, which
   snapshots the sandbox (FixStepRun.git_state) before the agent starts,
   so after the remedies; the review still sees their deletions, because
   it diffs against the base commit.
5. Review it: `verify_fix(sandbox, git_state=run.git_state)`. Same sandbox,
   same snapshot, same base: there is nothing else to pass.
6. Treat git_guard.GitGuardError (ScopeCheckError is one) as a refusal: no
   verdict is not a verdict, and an unverified fix is not a fix.
7. On an ok verdict with paths (not `nothing_changed`), test it (E3-R4).
   Trim to `sweep.max_files_per_pr` BEFORE this, so the tree tested is the
   PR's: `gate = green_gate.run_test_gate(sandbox, verdict, paths=trimmed)`,
   which runs the base's test command in the sandbox and, on red, drops
   fixed paths until the rest is green. Then, by outcome:
   - strategy `green` or `reduced`: go on to step 8 and record
     `run.succeeded(..., **gate.ledger_fields())`;
   - strategy `abandoned`: no PR; `run.failed(gate.render(),
     **gate.ledger_fields())` (render() carries the why);
   - GateError raised: a refusal, as in step 6, `run.refused(...)` with no
     `test_gate` field, since there is no GateResult to record.
8. Take exactly `gate.paths_for_pr` out: `gate.patch(sandbox)`, the
   patch of those paths built from the bytes the green run tested (what
   `sandbox.patch(gate.paths_for_pr)` gives when nothing changed since),
   refused with GateError if a kept path no longer holds them (a refusal
   as in step 6: a process the tests left running rewrote one), applied or
   pushed by the runner; list only those paths' fixes in the PR body. When
   it is empty (strategy `abandoned`, with any cause in
   green_gate.CAUSES), open no PR at all. Never `verdict.paths` once
   the gate has run, never `git add -A`, never the whole tree.
9. Never re-run the fix agent in the user's checkout, not even "to apply
   the same fix": the sandbox is what the verdict is about.

**The rules, and why each is drawn where it is.**

- *Files.* Every changed path must be one of the work list's `paths`. A new
  file is never in scope: the lint ran on the base commit, so a file that did
  not exist there has no violation, and E3-R2 says "only files with
  violations". A deleted file is out of scope too, with one exception below.
  Renames are read with `--no-renames`, so a rename is a deletion and an
  addition, and is refused as both. Paths are compared in Unicode NFC on
  both sides: a macOS file system hands names back in the form they were
  written, and git reports NFC.
- *Deleting a file.* Allowed only when the file carries a file-level
  violation — the lint reports a `target: path` rule's match (a name like
  `handlers_v2.py`) as the empty region at 1:1 (docs/curamoir-lint.md) —
  AND that violation's rule says `remedy: delete_file` (rules.REMEDIES), as
  loaded at the base by lint_sandbox. For such a violation the file's
  existence is the violation, so removing it is the fix rather than a
  refactor. The runner makes that deletion itself (remedies), before the
  agent starts; the agent may delete nothing, and the prompt says so. WHY
  the remedy is required here too, and not only the 1:1 region: a path
  rule without it may want a rename instead, and requiring it keeps the
  exception the runner's even if a later toolset could delete.
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
  stretch before its first function or class — of imports and of new
  module-level names bound to a literal or a logger; _only_header_statements
  and _header_value hold the exact rule and the reason for each part of it.
- *Signatures.* For Python, both versions are parsed and every function's
  and class's signature (arguments, defaults, annotations, return
  annotation, async-ness, decorators; a class's bases, keywords and
  decorators) compared by qualified name: one changed, removed or added is a
  signature change. It is allowed only where a principle at the base commit
  says `permits: signature_change` (E2-R1's format, src/curamoir/
  principles.py) and a violation of that principle lies within the
  definition (± WINDOW). For JavaScript and TypeScript there is no parser
  here, so any changed line that looks like a declaration (DECLARATION)
  counts as one — conservative, and blind to a parameter changed on a line
  of its own (see Limits).
- *Encoding.* A Python file is reviewed only if CPython decodes it as UTF-8
  on both sides (tokenize.detect_encoding over its bytes, as the
  interpreter does), and it is parsed from those bytes: the review reads
  text as UTF-8, and `ast.parse` of a str ignores a coding cookie, so a
  `# coding: utf-7` line turns comments the review sees into an import and
  a new definition at run time. Any other source encoding is UNVERIFIABLE,
  the base's included. WHY the base too: a latin-1 base with ASCII content
  reads the same either way, but proving that per file is more code than
  refusing a rare file costs, and refusing loses one fix where guessing
  wrong admits one. And the fix may not add, remove or change a coding
  cookie on line 1 or 2 at all, a UTF-8 one included (ENCODING_DECLARATION):
  no lint fix needs to, and with the declaration held fixed the review and
  CPython cannot come to read different text.

**WINDOW = 3.** A mechanical fix sometimes has to touch a line just above
the violation: chaining a cause (`raise X(...) from error`) needs the
`except` above it to bind `as error`, one to three lines up in ordinary code.
Three lines covers that, and is still small enough that rewriting the rest
of a function to "fix" one line in it is refused.

**Limits, stated rather than hidden.**

- Every path that changed in the sandbox is checked, whoever changed it. The
  fix step runs with `--restricted`, which loads no user hooks or plugins
  (fix_step), so cúramóir's own hooks do not write there; anything else
  that does is refused like the agent's own edits (cm-sto).
- JS/TS signatures are matched by line shape, not parsed: a parameter edited
  on its own line inside a multi-line parameter list is not seen.
- New-import detection knows the standard library from the interpreter
  running this (`sys.stdlib_module_names`, 3.10+; on older interpreters every
  import not already in the repository is reported) and Node's built-ins
  from a list. A package that is declared in a manifest but imported nowhere
  yet is reported as new.
- A dynamic import is seen only when its module is a string literal
  (`__import__("x")`, `import_module("x")`); `import_module(name)` with a
  computed name is not. In JS/TS a `require(` or `import(` whose argument
  is not one literal (quoted, or a template with no `${`), or is on
  another line, is NEW_DEPENDENCY: what it loads cannot be known. Lines are
  matched as text, so one in a comment or string counts too (the safe
  direction). Only Python source counts as a local module, and
  a directory holding JS/TS source as a local package; a bare specifier
  that a tsconfig `paths`/`baseUrl` alias resolves locally, and a compiled
  extension module (`.so`, `.pyd`), are reported as new.
- Git. The sandbox's git files are the clone's own, not the owner's, and
  are held to the snapshot as files before any git command runs; git runs
  hardened (git_guard). The base's own committed `.gitattributes` still
  apply to the diff: `-diff` and `binary` are overridden with `--text`, and
  no filter or external driver is configured to run, but an attribute that
  changes how a file is read (`working-tree-encoding`) is the owner's own,
  and a file it makes unreadable as UTF-8 is unverifiable, not passed.
- Python's own semantics only: a change of behaviour inside a function body
  within the window is the violation's fix as far as this module can tell.
  Whether it is a correct fix is E3-R4's question (the tests, green_gate).

Syntax that imports on 3.9, like sweep.py: the sweep reaches this through
`scripts/curamoir`, whose interpreter may be the fallback `python3`.
"""

import ast
import builtins
import collections
import difflib
import io
import json
import os
import re
import sys
import tokenize
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Optional

from curamoir.git_guard import (
    GitGuardError,
    git,
    require_sandbox,
    tree_changes,
    verify_git_state,
)
from curamoir.principles import PrincipleError, PrinciplesFile, parse_principles
from curamoir.rules import DELETE_FILE, RuleError, load_rules
from curamoir.sweep import work_list

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
GIT_INTERNALS_CHANGED = "git_internals_changed"
#: The sandbox's HEAD no longer names the base commit. The diff is always
#: taken against the base SHA, so this hides nothing from it; it is its own
#: kind because no fix step moves HEAD, and one that did has been doing
#: something other than editing files.
BASE_MOVED = "base_moved"
#: A path changed on disk that git does not report (inside an uninitialised
#: submodule, or hidden from git some other way): a change git cannot see
#: cannot be reviewed, and cannot be carried to a PR either.
HIDDEN_CHANGE = "hidden_change"
#: A Python file's coding cookie (PEP 263, line 1 or 2) was added, removed
#: or changed. Its own kind because it changes how CPython decodes every
#: line the review read (module docstring, *Encoding*).
ENCODING_DECLARATION = "encoding_declaration"

KINDS = (FILE_NOT_IN_WORK_LIST, FILE_ADDED, FILE_DELETED, DEPENDENCY_CHANGE,
         NEW_DEPENDENCY, OUTSIDE_VIOLATION, SIGNATURE_CHANGE, UNVERIFIABLE,
         GIT_INTERNALS_CHANGED, BASE_MOVED, HIDDEN_CHANGE, ENCODING_DECLARATION)

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

#: A JS/TS import specifier written as a literal: quoted, or a template
#: literal with no `$` (so no `${...}` substitution). Two groups, one empty.
_SCRIPT_IMPORT = re.compile(
    r"""(?:\bfrom\s*|\bimport\s*\(?\s*|\brequire\s*\(\s*|^\s*import\s+)"""
    r"""(?:['"]([^'"]+)['"]|`([^`$]+)`)""")

#: A `require(` or `import(` call (not a method of that name), and the one
#: literal argument it must have for _SCRIPT_IMPORT to know what it loads.
_SCRIPT_LOADER_CALL = re.compile(r"(?<![\w$.])(?:require|import)\s*\(")
_SCRIPT_LITERAL_ARGUMENT = re.compile(r"""\s*(?:'[^'\n]*'|"[^"\n]*"|`[^`$\n]*`)\s*[,)]""")

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
    PR made from an accepted fix commits, and nothing else.

    `ok` with no paths (`nothing_changed`) is a fix step that changed
    nothing: in scope, and nothing to open a PR for or count as fixed."""

    breaches: tuple
    paths: tuple

    @property
    def ok(self):
        return not self.breaches

    @property
    def nothing_changed(self):
        return not self.paths

    def kinds(self):
        """The breach kinds present, each once, in KINDS order."""
        present = {breach.kind for breach in self.breaches}
        return tuple(kind for kind in KINDS if kind in present)

    def render(self):
        if self.ok and self.nothing_changed:
            return "in scope: nothing changed, so nothing was fixed"
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


class ScopeCheckError(GitGuardError):
    """No verdict can be given: the review was asked about something it
    cannot check (no snapshot, not a sandbox, no work list, principles that
    do not parse). A GitGuardError, so a runner that refuses on that refuses
    on this too: an unverified fix is not a fix."""


# --- the pure core ---------------------------------------------------------------

def check_scope(changes, violations, permitting=frozenset(), known_modules=frozenset(),
                window=WINDOW, deletable_rules=frozenset()):
    """The verdict on `changes` (FileChanges) against `violations` (anything
    with path, line, col, end_line, end_col, principle_id and rule_id: the
    work list's items). `permitting` is the ids of principles that permit
    signature changes; `known_modules` the top-level module and package names
    the repository already uses or contains, which are not new dependencies;
    `deletable_rules` the ids of rules with `remedy: delete_file` (rules.
    REMEDIES), the only ones whose file-level violation licenses deleting the
    file. Paths are matched in NFC (module docstring)."""
    by_path = {}
    for violation in violations:
        by_path.setdefault(nfc(violation.path), []).append(violation)
    breaches = []
    for change in sorted(changes, key=lambda c: c.path):
        breaches.extend(_check_file(change, by_path.get(nfc(change.path), ()), permitting,
                                    known_modules, window, deletable_rules))
    return ScopeVerdict(breaches=tuple(breaches),
                        paths=tuple(sorted({c.path for c in changes})))


def _check_file(change, violations, permitting, known_modules, window, deletable_rules):
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
        if not any(is_file_level(v) and v.rule_id in deletable_rules
                   for v in violations):
            found.append(Breach(
                FILE_DELETED, path, None,
                "deleted, but none of its violations is about the file itself (a "
                "`target: path` rule's empty region at 1:1) with a rule that says "
                "`remedy: delete_file`"))
        return found

    old, new = change.old_text, change.new_text
    hunks = change.hunks if change.hunks is not None else compute_hunks(old, new)
    old_lines, new_lines = git_lines(old), git_lines(new)
    spans = [_span(v, window) for v in violations]
    python = path.endswith(PYTHON_SUFFIXES)
    script = path.endswith(SCRIPT_SUFFIXES)

    old_tree = new_tree = None
    if python:
        breach = _encoding_breach(path, old, new)
        if breach:
            found.append(breach)
            return found
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
                and _only_header_statements(added, python, old_tree, new_tree,
                                            hunk.new_start)):
            continue
        found.append(Breach(
            OUTSIDE_VIOLATION, path, max(hunk.old_start, 1),
            f"{_describe(hunk)} at base line {max(hunk.old_start, 1)}, "
            f"more than {window} line(s) from every violation in this file"))

    permitted_spans = [_span(v, window) for v in violations if v.principle_id in permitting]
    if python:
        found.extend(_python_signature_breaches(path, old_tree, new_tree, permitted_spans,
                                                hunks))
        found.extend(_new_import_breaches(path, _python_imports(old_tree),
                                          _python_imports(new_tree), known_modules,
                                          _STDLIB))
    elif script:
        found.extend(_script_signature_breaches(path, hunks, old_lines, new_lines,
                                                permitted_spans))
        found.extend(_new_import_breaches(path, _script_imports(old_lines),
                                          _script_imports(new_lines), known_modules,
                                          NODE_BUILTINS))
        found.extend(_computed_load_breaches(path, old_lines, new_lines))
    return found


def nfc(path):
    """`path` in Unicode normalisation form C, the form git reports."""
    return unicodedata.normalize("NFC", path)


def is_dependency_file(path):
    """True for a dependency manifest or lockfile (DEPENDENCY_FILES, and the
    requirements/constraints family), by its name wherever it sits."""
    name = PurePosixPath(path).name
    return name in DEPENDENCY_FILES or bool(_REQUIREMENTS.match(name)) or (
        PurePosixPath(path).parent.name == "requirements" and name.endswith((".txt", ".in")))


def git_lines(text):
    """`text`'s lines as git numbers them: split at "\\n" only, without the
    empty string after a final newline, and with a CRLF line's "\\r" dropped.

    WHY not str.splitlines: it also splits at \\f, \\v, \\x1c-\\x1e, \\x85,
    U+2028/U+2029 and a lone \\r, none of which git counts, so one form feed
    above a hunk shifted every slice below it by a line and the check read
    the line above the one inserted. A lone \\r is the one character Python
    *does* count as a line break where git does not; _parse_both refuses
    such a file rather than match parse lines to the wrong hunk lines."""
    parts = text.split("\n")
    if parts and parts[-1] == "":
        parts.pop()
    return [part[:-1] if part.endswith("\r") else part for part in parts]


def compute_hunks(old_text, new_text):
    """`-U0`-style hunks from difflib, for callers without git's. Not always
    git's minimal diff, so the adapter passes git's own."""
    matcher = difflib.SequenceMatcher(None, git_lines(old_text), git_lines(new_text),
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
    # "\n" only, like git_lines: splitlines would also break an added line at
    # a \f or \x85, and the text after it could pass for a hunk header.
    for line in diff_text.split("\n"):
        match = _HUNK_HEADER.match(line)
        if match:
            old_start, old_count, new_start, new_count = match.groups()
            hunks.append(Hunk(int(old_start), 1 if old_count is None else int(old_count),
                              int(new_start), 1 if new_count is None else int(new_count)))
    return tuple(hunks)


def is_file_level(violation):
    """Whether `violation` is about the file itself: the empty region at 1:1
    that the lint reports for a `target: path` rule (docs/curamoir-lint.md)."""
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


def _describe(hunk):
    if hunk.old_count == 0:
        return f"{hunk.new_count} line(s) inserted"
    if hunk.new_count == 0:
        return f"{hunk.old_count} line(s) deleted"
    return f"{hunk.old_count} line(s) replaced by {hunk.new_count}"


_LONE_CR = re.compile(r"\r(?!\n)")

#: PEP 263's coding cookie, as CPython's tokenizer matches it.
_CODING_COOKIE = re.compile(r"^[ \t\f]*#.*?coding[:=][ \t]*([-\w.]+)")


def _encoding_breach(path, old, new):
    """A Breach if CPython would not decode `old` or `new` as the UTF-8 the
    review read them as, or if the fix touched a coding cookie; else None
    (module docstring, *Encoding*)."""
    for side, text in (("base", old), ("fixed", new)):
        try:
            encoding, _ = tokenize.detect_encoding(io.BytesIO(text.encode("utf-8")).readline)
        except (SyntaxError, UnicodeError) as error:
            return Breach(UNVERIFIABLE, path, None,
                          f"the {side} version's source encoding cannot be read ({error})")
        if encoding not in ("utf-8", "utf-8-sig"):
            return Breach(UNVERIFIABLE, path, None, (
                f"CPython decodes the {side} version as {encoding} (its coding cookie) "
                "and the review reads UTF-8, so what was checked would not be what runs"))
    old_cookies, new_cookies = _coding_cookies(old), _coding_cookies(new)
    for number in (1, 2):
        if old_cookies.get(number) != new_cookies.get(number):
            return Breach(ENCODING_DECLARATION, path, number, (
                f"a coding cookie on line {number} was added, removed or changed; it "
                "decides how CPython reads every line, and a sweep fix does not touch it"))
    return None


def _coding_cookies(text):
    """Line number (1 or 2) -> the coding cookie line there."""
    return {number: line for number, line in enumerate(text.split("\n", 2)[:2], start=1)
            if _CODING_COOKIE.match(line)}


def _parse_both(old, new):
    """(old tree, new tree, None), or (None, None, why it cannot be checked).
    Parsed from the UTF-8 bytes, as CPython reads the file (a BOM included),
    not from the str, whose parse ignores a coding cookie
    (_encoding_breach has already held both sides to UTF-8)."""
    for side, text in (("base", old), ("fixed", new)):
        if _LONE_CR.search(text):
            return None, None, (f"the {side} version has a carriage return that does not "
                                "end a CRLF line; Python counts it as a line break and git "
                                "does not, so the parse cannot be matched to git's hunks")
    try:
        old_tree = ast.parse(old.encode("utf-8"))
    except (SyntaxError, ValueError) as error:
        return None, None, (f"the base version does not parse ({error.__class__.__name__}: "
                            f"{error}), so its signatures cannot be compared")
    try:
        new_tree = ast.parse(new.encode("utf-8"))
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


def _only_header_statements(added, python, old_tree=None, new_tree=None, first=None):
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
    what importing it has always meant.

    The statements are read from `new_tree`, the fixed module, as the whole
    top-level statements that exactly cover its lines `first` onwards (the
    hunk's new side): WHY not a parse of the added lines alone — `Y = True,`
    inserted into a multi-line `make(\\n a,\\n)` is a keyword argument in the
    module and an assignment on its own, and `import os` inside a triple-
    quoted string is text. Without `new_tree` (a caller holding only the
    lines) the lines are parsed on their own, as the whole module.

    A new name must also not be one whose meaning it would change for code
    already there (_shadows): a builtin (`print = None`), a dunder other than
    `__all__` (`__getattr__`, `__builtins__`), a name the module already reads
    anywhere, or any name at all after a `from x import *`, whose names this
    module cannot list."""
    if python:
        if new_tree is None:
            try:
                new_tree = ast.parse("\n".join(added))
            except (SyntaxError, ValueError):
                return False
            first = 1
        nodes = _statements_covering(new_tree, first, first + len(added) - 1, added)
        if nodes is None:
            return False
        bound, imports = _module_bindings(old_tree)
        referenced, star = _module_references(old_tree)
        loggers = (_logging_aliases(old_tree.body if old_tree else ())
                   | _logging_aliases(nodes))
        for node in nodes:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                if ast.dump(node) in imports:
                    continue
                names = [(a.asname or a.name).split(".", 1)[0] for a in node.names]
                if any(n in bound or _shadows(n, referenced, star) for n in names):
                    return False
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if not all(isinstance(t, ast.Name) and t.id not in bound
                           and not _shadows(t.id, referenced, star) for t in targets):
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


def _statements_covering(tree, first, last, lines):
    """The top-level statements of `tree` that lie wholly within lines
    `first`..`last` (`lines` being those lines' text), or None when a
    statement crosses either edge — the lines are then part of something
    larger — or a line in the range is neither in a statement nor blank or a
    comment."""
    nodes, covered = [], set()
    for node in tree.body:
        start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", ())])
        end = getattr(node, "end_lineno", None) or node.lineno
        if end < first or start > last:
            continue
        if start < first or end > last:
            return None
        nodes.append(node)
        covered.update(range(start, end + 1))
    for number in range(first, last + 1):
        text = lines[number - first].strip() if number - first < len(lines) else ""
        if number not in covered and text and not text.startswith("#"):
            return None
    return nodes


_BUILTIN_NAMES = frozenset(vars(builtins))


def _shadows(name, referenced, star):
    """Whether binding `name` at module level could change what existing code
    means (see _only_header_statements)."""
    if star or name in _BUILTIN_NAMES or name in referenced:
        return True
    return name.startswith("__") and name.endswith("__") and name != "__all__"


def _module_references(tree):
    """(every name `tree` mentions as a Name or in a `global`/`nonlocal`,
    whether it has a `from x import *` anywhere)."""
    if tree is None:
        return set(), False
    names, star = set(), False
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            names.update(node.names)
        elif isinstance(node, ast.ImportFrom) and any(a.name == "*" for a in node.names):
            star = True
    return names, star


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


def _logging_aliases(statements):
    """The names the `import logging [as x]` among `statements` (a module's
    top-level statements) bind to the standard logging module."""
    return {alias.asname or "logging"
            for node in statements if isinstance(node, ast.Import)
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


def _python_signature_breaches(path, old_tree, new_tree, permitted_spans, hunks=()):
    before, after = _python_definitions(old_tree), _python_definitions(new_tree)
    # An added definition exists only in the fixed file, so its span is in
    # the fixed file's lines; the permitted spans are the base commit's, and
    # are carried through the hunks before the two are compared. WHY: ten
    # inserted header lines otherwise let a definition added ten lines away
    # from a permitting violation pass, and refused one added right at it.
    shifted = [(_to_new_line(start, hunks), _to_new_line(end, hunks, end=True))
               for start, end in permitted_spans]
    found = []
    for name in sorted(set(before) | set(after)):
        old, new = before.get(name), after.get(name)
        if old is not None and new is not None and old[0] == new[0]:
            continue
        span, allowed_spans = (old[1], permitted_spans) if old else (new[1], shifted)
        if any(_overlaps(span, allowed) for allowed in allowed_spans):
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


def _to_new_line(line, hunks, end=False):
    """Base line `line`'s number in the fixed file, through `hunks`. A line
    inside a replaced region maps to that region's first line in the fixed
    file, or with `end` to its last. WHY: a span's end is mapped with `end`,
    so a span that reaches into a replaced region covers all of what replaced
    it; mapped to the first line, a six-line replacement of a span's last
    three lines would shrink the span to the replacement's first line."""
    delta = 0
    for hunk in sorted(hunks, key=lambda h: h.old_start):
        if hunk.old_count == 0:
            if line > hunk.old_start:
                delta += hunk.new_count
        elif line > hunk.old_start + hunk.old_count - 1:
            delta += hunk.new_count - hunk.old_count
        elif line >= hunk.old_start:
            return hunk.new_start + (max(hunk.new_count, 1) - 1 if end else 0)
    return line + delta


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
    anywhere in the module (function-local ones too), and for the dynamic
    forms whose module is a string literal: `__import__("x")`,
    `importlib.import_module("x")` and a bare `import_module("x")`. A
    dynamic import of a computed name is not seen (Limits)."""
    found = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module]
        elif _dynamic_import(node):
            names = [node.args[0].value]
        else:
            continue
        for name in names:
            top = name.split(".", 1)[0]
            if top not in found or node.lineno < found[top]:
                found[top] = node.lineno
    return found


def _dynamic_import(node):
    """Whether `node` is `__import__`/`import_module` called on an absolute
    module name spelled as a string literal. Any `x.import_module` counts:
    over-matching a look-alike only reports a module the repo does not use."""
    if not (isinstance(node, ast.Call) and node.args):
        return False
    func = node.func
    name = func.id if isinstance(func, ast.Name) else (
        func.attr if isinstance(func, ast.Attribute) else None)
    arg = node.args[0]
    return (name in ("__import__", "import_module") and isinstance(arg, ast.Constant)
            and isinstance(arg.value, str) and bool(arg.value)
            and not arg.value.startswith("."))


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


def _script_specifiers(line):
    return [quoted or template for quoted, template in _SCRIPT_IMPORT.findall(line)]


def _script_imports(lines):
    found = {}
    for number, line in enumerate(lines, start=1):
        for specifier in _script_specifiers(line):
            package = script_package(specifier)
            if package and package not in found:
                found[package] = number
    return found


def _computed_loads(lines):
    """(line number, stripped line) for each line with a `require(` or
    `import(` whose argument is not one literal, in file order."""
    return [(number, line.strip()) for number, line in enumerate(lines, start=1)
            if any(not _SCRIPT_LITERAL_ARGUMENT.match(line, call.end())
                   for call in _SCRIPT_LOADER_CALL.finditer(line))]


def _computed_load_breaches(path, old_lines, new_lines):
    """A NEW_DEPENDENCY Breach for each computed load the fix added.

    WHY the stripped text is the key: what a computed load names cannot be
    known, so the only safe answer is "this exact line was already in the
    base" — moved or re-indented, not written. WHY counted, not looked up:
    a second copy of an old `require(name)` pasted elsewhere, with `name`
    rebound above it, loads something new while matching an old line. So
    the fixed file may hold each text as often as the base does; each
    occurrence beyond that, in file order, is reported. Editing a line that
    holds a computed load changes its text and is refused too: that fails
    closed, at the cost of a fix that only touched the rest of that line."""
    allowed = collections.Counter(text for _, text in _computed_loads(old_lines))
    found = []
    for number, text in _computed_loads(new_lines):
        if allowed[text]:
            allowed[text] -= 1
            continue
        found.append(Breach(
            NEW_DEPENDENCY, path, number,
            f"loads a module named by something other than one literal (fixed line "
            f"{number}), so whether it is a new dependency cannot be checked"))
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
# Every `repo` parameter in this section is a sandbox's path (Sandbox.path),
# never the user's checkout: lint_sandbox and verify_fix, the entry points,
# require a Sandbox and pass its path down.

def lint_sandbox(sandbox, **lint_options):
    """Run the lint (sweep.work_list, with `lint_options`) in `sandbox` while
    its work tree is still exactly its base commit, record the result as
    `sandbox.work_list` for verify_fix, and return it. Record too, as
    `sandbox.rules`, the rules the lint ran (rules.load_rules on the same
    pristine tree): which of them carry a `remedy` is the base's answer for
    remedies.apply_file_remedies and verify_fix, never the edited tree's.

    WHY here and not by the caller: the verdict's line numbers and its "only
    files with violations" are the work list's, so the work list must be the
    base's. A list computed in the user's checkout (another commit, or
    uncommitted edits) would let a fix pass for violations the base never
    had. ScopeCheckError if the tree is not the base (anything modified,
    staged or untracked, ignored or not); sweep.LintUntrusted if the lint's
    report cannot be acted on."""
    require_sandbox(sandbox, ScopeCheckError)
    require_pristine(sandbox)
    result = work_list(sandbox.path, **lint_options)
    try:
        rules = tuple(load_rules(sandbox.path))
    except RuleError as error:
        raise ScopeCheckError(f"the rules at the base commit cannot be loaded: {error}") \
            from error
    require_pristine(sandbox)
    sandbox.work_list = result
    sandbox.rules = rules
    return result


def require_pristine(sandbox):
    """ScopeCheckError unless `sandbox`'s work tree is exactly its base
    commit: nothing modified, staged or untracked, ignored or not."""
    status = git(sandbox.path, "status", "--porcelain", "-z", "--ignored",
                 "--untracked-files=all", "--ignore-submodules=all")
    if status or sandbox.head() != sandbox.base:
        raise ScopeCheckError(
            f"the sandbox at {sandbox.path} is not the base commit {sandbox.base} as "
            "checked out, so a work list computed in it would not be the base's")


def base_principles(sandbox):
    """The principles file at `sandbox`'s base commit (an empty one if it has
    none), read from git's objects. WHY never the work tree: the fix agent
    can edit `.curamoir/principles.md` in the sandbox, and a `permits:
    signature_change` it wrote there would otherwise license its own
    signature change. ScopeCheckError if the base's file does not parse."""
    require_sandbox(sandbox, ScopeCheckError)
    data = sandbox.show(".curamoir/principles.md")
    if data is None:
        return PrinciplesFile()
    try:
        return parse_principles(data.decode("utf-8"))
    except (UnicodeDecodeError, PrincipleError) as error:
        raise ScopeCheckError(
            f"the principles at the base commit cannot be read, so which principles "
            f"permit signature changes is unknown: {error}") from error


def verify_fix(sandbox, *, git_state, window=WINDOW):
    """E3-R2's review step: the verdict on everything that changed between
    `sandbox`'s base commit and its work tree (staged, unstaged and
    untracked, ignored or not), against the work list lint_sandbox recorded
    and the principles at the base commit (base_principles). There is no
    work-list or principles parameter: both are the base's, by construction.

    `git_state` is git_guard.snapshot_git_state(sandbox), taken before the
    fix step ran (fix_step.run_fix_step takes it: FixStepRun.git_state). It
    is compared FIRST, by reading files, and a difference in the sandbox's
    git files or any `.gitattributes` is the whole verdict: WHY — `.git` is
    invisible to `git diff`, and a planted fsmonitor or filter driver would
    run on the very git command that asked what changed. It is required and
    the check fails closed: no snapshot, a snapshot of another sandbox or
    base, `sandbox` not a git_guard.Sandbox, or no work list raises
    ScopeCheckError before any git command runs.

    Then: a HEAD that left the base is BASE_MOVED, and the whole verdict.
    Every path the snapshot's work-tree manifest saw change but git does not
    report is HIDDEN_CHANGE, alongside the core's breaches."""
    if git_state is None:
        raise ScopeCheckError("no git-state snapshot taken before the fix step; refusing "
                              "to run git on the edited tree")
    require_sandbox(sandbox, ScopeCheckError)
    if os.path.realpath(git_state.path) != os.path.realpath(str(sandbox.path)):
        raise ScopeCheckError(f"the git-state snapshot is of {git_state.path}, not "
                              f"{sandbox.path}")
    if git_state.base != sandbox.base:
        raise ScopeCheckError(f"the git-state snapshot was taken for base {git_state.base}, "
                              f"not {sandbox.base}")
    if sandbox.work_list is None or sandbox.rules is None:
        raise ScopeCheckError("no work list or rules were linted in this sandbox at its "
                              "base (fix_scope.lint_sandbox), so there is nothing to hold "
                              "the fix to")
    changed = verify_git_state(git_state)
    if changed:
        return ScopeVerdict(breaches=tuple(
            Breach(GIT_INTERNALS_CHANGED, c.label, None,
                   f"{c.change} while the fix step ran; {c.reason}, so nothing more of "
                   "this tree is read")
            for c in changed), paths=())
    base = sandbox.base
    head = sandbox.head()
    if head != base:
        return ScopeVerdict(breaches=(Breach(
            BASE_MOVED, "HEAD", None,
            f"names {head}, not the base {base} the sandbox was checked out at; the fix "
            "step edits files and never moves HEAD, so nothing more of this tree is "
            "read"),), paths=())
    changes = changes_since(sandbox.path, base)
    seen = {nfc(change.path) for change in changes}
    hidden = sorted(({nfc(path) for path in tree_changes(git_state)}
                     | set(_missing_submodules(sandbox.path, base))) - seen)
    permitting = frozenset(p.id for p in base_principles(sandbox).principles
                           if SIGNATURE_PERMISSION in p.permits)
    verdict = check_scope(changes, sandbox.work_list.items, permitting=permitting,
                          known_modules=base_known_modules(sandbox.path, base), window=window,
                          deletable_rules=rules_licensing_deletion(sandbox.rules))
    if not hidden:
        return verdict
    return ScopeVerdict(
        breaches=verdict.breaches + tuple(Breach(
            HIDDEN_CHANGE, path, None,
            "changed on disk while the fix step ran, but git does not report it (inside "
            "an uninitialised submodule, or hidden another way); a change git cannot see "
            "can be neither reviewed nor carried to a PR") for path in hidden),
        paths=tuple(sorted(set(verdict.paths) | set(hidden))))


def rules_licensing_deletion(rules):
    """The ids of `rules` whose file-level violation licenses deleting the
    file: those with `remedy: delete_file` (rules.REMEDIES)."""
    return frozenset(rule.id for rule in rules if rule.remedy == DELETE_FILE)


def _missing_submodules(repo, base):
    """The base's submodule paths (gitlinks) that are no longer an empty-or-
    not directory in the work tree. WHY: git is told never to look inside a
    submodule (git_guard), so `git diff` no longer reports one removed or
    replaced by a file, and the work-tree manifest records files, not empty
    directories. What is put inside one is the manifest's to see."""
    missing = []
    for entry in git(repo, "ls-tree", "-r", "-z", "--full-tree", base).split(b"\0"):
        meta, _, name = entry.partition(b"\t")
        if meta.startswith(b"160000 "):
            path = name.decode("utf-8", "surrogateescape")
            target = Path(repo) / path
            if target.is_symlink() or not target.is_dir():
                missing.append(nfc(path))
    return missing


def changes_since(repo, base):
    """Every path that differs between commit `base` and `repo`'s working
    tree, as FileChanges with git's own `-U0` hunks. Untracked files are
    listed with no exclude rules: in a sandbox, a fresh clone holds no
    ignored file, so any untracked file, ignored or not, is new."""
    repo = Path(repo)
    raw = git(repo, "diff", "--raw", "-z", "--no-renames", "--no-ext-diff",
              "--ignore-submodules=all", base, "--")
    changes = []
    fields = raw.split(b"\0")
    index = 0
    while index + 1 < len(fields) and fields[index].startswith(b":"):
        meta = fields[index][1:].decode("ascii").split()
        path = fields[index + 1].decode("utf-8", "surrogateescape")
        index += 2
        changes.append(_tracked_change(repo, base, path, meta))
    untracked = git(repo, "ls-files", "--others", "-z")
    for entry in untracked.split(b"\0"):
        if entry:
            path = entry.decode("utf-8", "surrogateescape")
            changes.append(FileChange(path, ADDED, None, _read_text(repo / path)[0]))
    return tuple(changes)


def _tracked_change(repo, base, path, meta):
    old_mode, new_mode, status = meta[0], meta[1], meta[4]
    kind = {"A": ADDED, "D": DELETED}.get(status[0], MODIFIED)
    if kind == ADDED:
        return FileChange(path, ADDED, None, _read_text(repo / path)[0])
    if kind == DELETED:
        return FileChange(path, DELETED, _blob_text(repo, base, path)[0], None)
    unusual = {"120000": "a symlink", "160000": "a submodule"}
    for mode in (old_mode, new_mode):
        if mode in unusual:
            return FileChange(path, MODIFIED, unverifiable=(
                f"{unusual[mode]} changed; only regular text files can be checked"))
    if old_mode != new_mode:
        return FileChange(path, MODIFIED, unverifiable=(
            f"its mode changed ({old_mode} to {new_mode}); a fix edits text, not modes"))
    old, old_problem = _blob_text(repo, base, path)
    new, new_problem = _read_text(repo / path)
    if old_problem or new_problem:
        return FileChange(path, MODIFIED, unverifiable=old_problem or new_problem)
    # --text: a committed `-diff` or `binary` attribute would otherwise print
    # "Binary files differ" and no hunks, and no hunks pass the window rule.
    # The content is UTF-8 without NULs, checked just above.
    diff = git(repo, "diff", "-U0", "--text", "--no-color", "--no-ext-diff", "--no-textconv",
               "--no-renames", base, "--", f":(literal){path}")
    return FileChange(path, MODIFIED, old, new,
                      hunks=parse_hunks(diff.decode("utf-8", "surrogateescape")))


def _decode(data, where):
    if b"\0" in data:
        return None, f"{where} is binary; only text files can be checked"
    try:
        return data.decode("utf-8"), None
    except UnicodeDecodeError as error:
        return None, f"{where} is not UTF-8 ({error}); only text files can be checked"


def _blob_text(repo, base, path):
    return _decode(git(repo, "cat-file", "blob", f"{base}:{path}"), "the base version")


def _read_text(path):
    try:
        return _decode(Path(path).read_bytes(), "the fixed version")
    except OSError as error:
        return None, f"the fixed version cannot be read: {error}"


_PY_IMPORT_LINE = re.compile(r"^\s*(?:import\s+([\w., ]+)|from\s+(\w[\w.]*)\s+import\b)")


def base_known_modules(repo, base):
    """Top-level module and package names that are not a new dependency at
    commit `base`: every Python file's module name and every directory
    holding Python or JS/TS source (a local module or package, wherever it
    sits), every module a Python file imports, every package a JS/TS file
    imports, and package.json's declared packages.

    Only source counts as local: WHY — `docs/pandas.md` is not a module named
    pandas, and counting every file stem let `import pandas` pass as local.
    The grep patterns use POSIX classes (`[[:space:]]`), not `\\s`: Apple's
    git 2.39 does not know `\\s` in `-E`, matched nothing, and every import
    in the repository looked new on macOS."""
    known = set()
    listing = git(repo, "ls-tree", "-r", "-z", "--name-only", base)
    for entry in listing.split(b"\0"):
        parts = [p for p in entry.decode("utf-8", "surrogateescape").split("/") if p]
        if not parts or not parts[-1].endswith(PYTHON_SUFFIXES + SCRIPT_SUFFIXES):
            continue
        if parts[-1].endswith(PYTHON_SUFFIXES):
            known.add(parts[-1].split(".", 1)[0])
        known.update(parts[:-1])
    python = git(repo, "grep", "-h", "-I", "-E", r"^[[:space:]]*(import|from)[[:space:]]",
                 base, "--", "*.py", "*.pyi", ok=(0, 1))
    for line in python.decode("utf-8", "surrogateescape").splitlines():
        match = _PY_IMPORT_LINE.match(line)
        if not match:
            continue
        names = match.group(1).split(",") if match.group(1) else [match.group(2)]
        for name in names:
            name = name.strip().split(" ", 1)[0]
            if name:
                known.add(name.split(".", 1)[0])
    scripts = git(repo, "grep", "-h", "-I", "-E",
                  r"(from|import|require)[[:space:]]*\(?[[:space:]]*['\"`]",
                  base, "--", *(f"*{suffix}" for suffix in SCRIPT_SUFFIXES), ok=(0, 1))
    for line in scripts.decode("utf-8", "surrogateescape").splitlines():
        for specifier in _script_specifiers(line):
            package = script_package(specifier)
            if package:
                known.add(package)
    known.update(_declared_packages(repo, base))
    return frozenset(known)


def _declared_packages(repo, base):
    raw = git(repo, "cat-file", "-p", f"{base}:package.json", ok=(0, 128))
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
