"""R5-R1 and R5-R33: round 0, the gate's deterministic pre-pass.

Before any model is dispatched, round 0 runs on ``BASE..HEAD`` what needs
none: the caller's steps (``curamoir-lint``, the configured linters and type
check, the full suite) and the provenance mechanical checks of R5-R33. Any
failure ends the gate ``PREFLIGHT_FAILED``, with the tool output as the
record (§11: a ``preflight`` round's ``preflight.steps``).

**The R5-R33 checks**, over the test files the change adds or touches:

- the bundled test guard rules (``examples/python/``, principle
  ``tests-actually-run``: a module-level skip, a layout-relative path, an
  early return in a test body, an except that swallows, a collection-time
  parametrise list with no minimum), run in process by ``curamoir-lint``'s
  own runner, whatever rules the repository installs;
- from the suite's own per-test results (JUnit XML, each case attributed
  to a test file by its class name): no test the change added or changed
  was skipped or left out of the run (deselected, or never collected), and
  no changed test module collected nothing.

A hit is a blocking finding under the provenance lens, recorded in the round
with ``model`` None; a check that cannot run blocks as well, so round 0
never passes having looked at less than it says.

**What the caller supplies** (Rob, 2026-11-18). The steps and the function
that runs them. Round 0 has no default run function and reads no
configuration: where a step runs and under what isolation (R5-R44's OS
sandbox, or a hosted runner's VM) is the gate job's, as are the config keys
that name the steps. A run function takes a :class:`Step` and the checkout
and returns a :class:`StepRun`; for the suite step (``suite=True``) it also
returns the JUnit XML of that run, produced with
``--junitxml=<file> --rootdir=<checkout>``: pytest names each case's class
from its rootdir, not its working directory, so ``--rootdir`` is what makes
those names the checkout's modules. A case is attributed to a changed test
file by that class name alone (``_attribute``); results named for no test
file in the checkout are refused as unreadable, never read as "collected
nothing", and a case two files fit equally makes them "cannot attribute". Round 0 itself runs no
repository code: the guard rules are read by ast-grep, and the test files by
``ast``.

R5-R2 onwards (the review rounds, the merge, the loop) are the next bead's,
in this module.
"""

from __future__ import annotations

import ast
import datetime
import math
import re
import xml.etree.ElementTree as ElementTree
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from curamoir import plugin_files
from curamoir.gate import record
from curamoir.gate.findings import fingerprint
from curamoir.git_guard import GitGuardError, git
from curamoir.lint import DEFAULT_TIMEOUT, LintError, run_rules
from curamoir.principles import PrincipleError, parse_principles
from curamoir.rules import ALWAYS_EXCLUDED, ROOT_EXCLUDED, RuleError, parse_rule

#: The principle the R5-R33 guard rules cite (templates/principles.md).
GUARD_PRINCIPLE = "tests-actually-run"

#: A test file, as the guard rules scope it (`files:` in each rule) and as
#: pytest collects one by default.
TEST_FILE = re.compile(r"(?:.*/)?(?:test_[^/]*|[^/]*_test)\.py")

#: pytest's default prefixes for a test function and a test class.
TEST_FUNCTION_PREFIX = "test"
TEST_CLASS_PREFIX = "Test"

#: The most JUnit XML round 0 reads. A suite's results are kilobytes to a
#: few megabytes; anything past this is refused, not parsed.
MAX_JUNIT_BYTES = 64 << 20

LENS = "provenance"

_HUNK = re.compile(rb"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)


class Round0Error(ValueError):
    """Round 0 was called wrongly: the checkout is not ``head`` as
    committed, or the steps are not a valid set. Nothing was run."""


@dataclass(frozen=True)
class Step:
    """One deterministic step: a name for the record, the argv to run, and
    a bound in seconds that the run function enforces. ``suite`` marks the
    full suite, whose run also returns its JUnit XML."""

    name: str
    argv: tuple
    timeout: float
    suite: bool = False


@dataclass(frozen=True)
class StepRun:
    """What a run function returns for one step: its exit status (None if
    it was killed or never started, with the reason in ``output``), its
    combined stdout and stderr, how long it took, and for the suite step its
    JUnit XML (None when there is none)."""

    exit: int | None
    output: bytes
    duration_s: float
    junit_xml: bytes | None = None


RunFunction = Callable[[Step, Path], StepRun]


@dataclass(frozen=True)
class Round0:
    """Round 0's outcome: the record round (``kind: preflight``), ready for
    :func:`record.append_round`, and whether it passed."""

    round: dict
    passed: bool
    findings: tuple = field(default=())
    failed_steps: tuple = field(default=())


def run_round0(repo, *, base: str, head: str, steps: Sequence[Step], run: RunFunction,
               test_paths: Sequence[str], test_excludes: Sequence[str] = (),
               ast_grep: str | None,
               ast_grep_timeout: float = DEFAULT_TIMEOUT, n: int = 1) -> Round0:
    """Run round 0 on ``base..head`` in ``repo``, a checkout of ``head``
    with no tracked change.

    The R5-R33 checks read the changed test files first, before any step
    runs repository code that could rewrite them; then every step runs, in
    order, whatever the ones before it did, so the record holds all of their
    output; then the checkout is checked again, and a step that moved HEAD
    or changed a tracked file fails the round. ``test_paths`` are the
    repo-relative directories (or files) the suite collects tests from, the
    project's ``testpaths`` (``"."`` for the whole repository): a changed
    ``test_*.py`` outside them (a fixture named like a test) is not one of
    the suite's tests and is not checked, and neither is one under
    ``test_excludes`` (what the project's ``collect_ignore`` or
    ``norecursedirs`` keeps out).
    ``ast_grep`` is the ast-grep binary the guard rules need (None: they
    cannot run, which fails the round). ``n`` is the round's number in the
    record."""
    repo = Path(repo).resolve()
    _check_steps(steps)
    scope = _check_test_paths(test_paths, "test_paths")
    excluded = _check_test_paths(test_excludes, "test_excludes", empty=True)
    _check_checkout(repo, base, head)
    started = _now()

    changed = [path for path in _changed_test_files(repo, base, head)
               if _under(path, scope) and not _under(path, excluded)]
    findings, available = _guard_findings(repo, changed, ast_grep, ast_grep_timeout)
    read = _read_tests(repo, base, head, changed)
    every_test_file = _test_files_at(repo, head)

    step_records, failed, junit = [], [], None
    for step in steps:
        outcome = run(step, repo)
        if not isinstance(outcome, StepRun):
            raise Round0Error(f"the run function returned {type(outcome).__name__} for "
                              f"{step.name!r}, not a StepRun")
        step_records.append(_step_record(step, outcome))
        if outcome.exit != 0:
            failed.append(step.name)
        if step.suite:
            junit = outcome.junit_xml

    moved, where = _moved_by_the_steps(repo, head)
    if moved:
        findings.append(_run_finding(where, 1, 1, "a step changed the checkout round 0 ran "
                                     "on, so its results are not head's", moved))
    findings += _run_findings(changed, read, every_test_file, junit)
    for finding in findings:
        finding["fingerprint"] = fingerprint(LENS, finding["path"], finding["claim"])
        finding["status"] = "open"

    round_ = {
        "n": n, "kind": "preflight", "head": head, "started": started, "ended": _now(),
        "lenses": {LENS: {"model": None, "status": "ok" if available else "UNAVAILABLE",
                          "findings": findings}},
        "preflight": {"steps": step_records},
    }
    return Round0(round=round_, passed=not failed and not findings and available,
                  findings=tuple(findings), failed_steps=tuple(failed))


def write_round0(repo, bead: str, result: Round0) -> dict:
    """Append round 0 to the bead's record (started by the caller with
    :func:`record.start`) and, when it failed, end the gate there: the
    verdict ``PREFLIGHT_FAILED``, keyed to the tree round 0 ran on."""
    written = record.append_round(repo, bead, result.round)
    if result.passed:
        return written
    head = result.round["head"]
    try:
        tree = git(repo, "rev-parse", f"{head}^{{tree}}").decode("ascii").strip()
    except GitGuardError as error:
        raise record.RecordError(f"the tree of {head} cannot be read: {error}") from error
    return record.set_verdict(repo, bead, "PREFLIGHT_FAILED", tree_sha=tree, head_sha=head,
                              at=result.round["ended"])


def guard_rules():
    """(rules, principles): the bundled test guard rules, every rule in
    plugin_files.PYTHON_EXAMPLES citing GUARD_PRINCIPLE, and the starter
    principles they resolve in. RuleError if there are none or one does
    not load: round 0 never runs fewer checks than it ships."""
    try:
        principles = parse_principles(
            (plugin_files.TEMPLATES / "principles.md").read_text(encoding="utf-8"))
    except (OSError, PrincipleError) as error:
        raise RuleError(f"the starter principles cannot be read: {error}") from error
    known = {p.id for p in principles.principles}
    rules = []
    for path in sorted(plugin_files.PYTHON_EXAMPLES.glob("*.yaml")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as error:
            raise RuleError(f"{path.name}: cannot be read: {error}") from error
        rule = parse_rule(text, source=path.name, known_principles=known)
        if rule.principle == GUARD_PRINCIPLE:
            rules.append(rule)
    if not rules:
        raise RuleError(f"no rule in {plugin_files.PYTHON_EXAMPLES} cites {GUARD_PRINCIPLE}")
    return rules, principles


# --- the checkout and the steps --------------------------------------------

def _check_steps(steps) -> None:
    if not isinstance(steps, Sequence) or not steps:
        raise Round0Error("round 0 needs its steps, the full suite among them")
    names = set()
    for step in steps:
        if not isinstance(step, Step):
            raise Round0Error(f"a step is a gate.rounds.Step, not {type(step).__name__}")
        if not (isinstance(step.name, str) and step.name.strip()) or step.name in names:
            raise Round0Error(f"step names are distinct non-empty text: {step.name!r}")
        names.add(step.name)
        if not (isinstance(step.argv, tuple) and step.argv
                and all(isinstance(a, str) for a in step.argv)):
            raise Round0Error(f"step {step.name!r}: argv is a non-empty tuple of strings")
        if type(step.timeout) not in (int, float) or not step.timeout > 0:
            raise Round0Error(f"step {step.name!r}: timeout is a positive number of seconds")
    suites = [step.name for step in steps if step.suite]
    if len(suites) != 1:
        raise Round0Error(f"exactly one step is the full suite (suite=True), not {len(suites)}: "
                          "the R5-R33 run checks read its results")


def _check_test_paths(paths, name, empty=False) -> tuple:
    """``paths`` as normalised repo-relative paths, "" for ``"."`` (the
    whole repository)."""
    if isinstance(paths, str) or not isinstance(paths, Sequence) or (not paths and not empty):
        raise Round0Error(f"{name} is a {'' if empty else 'non-empty '}sequence of repo-relative "
                          "paths (directories or files)")
    scope = []
    for root in paths:
        if root == ".":
            scope.append("")
            continue
        parts = root.strip("/").split("/") if isinstance(root, str) else []
        if not parts or root.startswith("/") or any(p in ("", ".", "..") for p in parts):
            raise Round0Error(f"test path {root!r} in {name} is not a plain repo-relative path")
        scope.append("/".join(parts))
    return tuple(scope)


def _under(path: str, roots: tuple) -> bool:
    return any(root == "" or path == root or path.startswith(root + "/") for root in roots)


def _moved_by_the_steps(repo: Path, head: str) -> tuple:
    """(why, path): why the checkout is no longer ``head`` as committed
    after the steps, and the first file changed ("." when HEAD moved or the
    checkout cannot be read); ("", "") when it still is head."""
    try:
        checked_out = git(repo, "rev-parse", "HEAD").decode("ascii").strip()
        dirty = git(repo, "status", "--porcelain", "-z", "--untracked-files=no",
                    "--ignore-submodules=all")
    except GitGuardError as error:
        return f"the checkout cannot be read after the steps: {error}", "."
    if checked_out != head:
        return f"HEAD is {checked_out} after the steps, not {head}", "."
    if dirty:
        names = [entry[3:].decode("utf-8", "replace") for entry in dirty.split(b"\0")
                 if len(entry) > 3]
        return "tracked files changed by the steps: " + ", ".join(names[:10]), names[0]
    return "", ""


def _check_checkout(repo: Path, base: str, head: str) -> None:
    """``repo`` is ``head`` checked out with no tracked change, and both
    ends are commits: round 0 reads the files on disk as ``head``'s."""
    try:
        for name, value in (("base", base), ("head", head)):
            if not (isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value)):
                raise Round0Error(f"{name} must be a full commit id")
            git(repo, "cat-file", "-e", f"{value}^{{commit}}")
        checked_out = git(repo, "rev-parse", "HEAD").decode("ascii").strip()
        dirty = git(repo, "status", "--porcelain", "-z", "--untracked-files=no",
                    "--ignore-submodules=all")
    except GitGuardError as error:
        raise Round0Error(f"{repo} cannot be read as a checkout of {head}: {error}") from error
    if checked_out != head:
        raise Round0Error(f"{repo} has {checked_out} checked out, not head {head}")
    if dirty:
        raise Round0Error(f"{repo} has tracked changes, so its files are not head's")


def _step_record(step: Step, outcome: StepRun) -> dict:
    duration = outcome.duration_s
    if type(duration) not in (int, float) or not math.isfinite(duration) or duration < 0:
        raise Round0Error(f"the run function gave step {step.name!r} a duration_s of "
                          f"{duration!r}, not a finite number of seconds")
    output = outcome.output if isinstance(outcome.output, bytes) else b""
    text = output.decode("utf-8", "replace")
    return {"name": step.name, "argv": list(step.argv),
            "exit": outcome.exit if type(outcome.exit) is int else None,
            "duration_s": round(float(outcome.duration_s), 3),
            "output_tail": text[-record.PREFLIGHT_TAIL_CHARS:],
            "output_bytes": len(output)}


# --- R5-R33: which tests the change touched ---------------------------------

def _changed_test_files(repo: Path, base: str, head: str) -> list:
    """The test files ``base..head`` adds, modifies, renames into or copies,
    as repo-relative POSIX paths; a deleted one has nothing left to run."""
    out = git(repo, "diff", "--name-only", "-z", "-M", "--diff-filter=AMRC", base, head)
    paths = [p.decode("utf-8", "surrogateescape") for p in out.split(b"\0") if p]
    return sorted(p for p in paths if TEST_FILE.fullmatch(p))


def _changed_lines(repo: Path, base: str, head: str, path: str) -> set:
    """The lines of ``path`` at ``head`` that ``base..head`` adds or changes,
    and for a deletion the line it was deleted after (and the one after
    that), so a test that only lost lines still counts as changed."""
    out = git(repo, "diff", "-U0", "--no-color", "--no-ext-diff", base, head, "--", path)
    lines = set()
    for start, count in _HUNK.findall(out):
        first, length = int(start), 1 if count == b"" else int(count)
        lines.update(range(first, first + length) if length else (first, first + 1))
    return lines


def _tests_in(source: str):
    """[(class chain, function name, first line, last line, collected by
    name, xfails first)] for every function named ``test*`` defined in the
    module or in its classes, under any block (``if``, ``try``, ``for``,
    ``with``, ``match`` ...), but not inside another function. The first
    line is the first decorator's.

    "Collected by name" is whether pytest's default rules collect it on its
    name alone: at module level, or in classes each named ``Test*`` or with a
    direct ``*TestCase`` base. Only such a test is reported as left out of
    the run when the run does not name it; any other (a test in a subclass
    of a project base class, a mixin's method) is checked only through what
    the run reports for it. "Xfails first" marks a test whose first
    statement is ``pytest.xfail(...)``: its expected failure means none of
    it ran."""
    tree = ast.parse(source)
    found = []

    def visit(body, chain, by_name):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith(TEST_FUNCTION_PREFIX):
                    first = min([node.lineno] + [d.lineno for d in node.decorator_list])
                    found.append((chain, node.name, first, node.end_lineno, by_name,
                                  _xfails_first(node)))
            elif isinstance(node, ast.ClassDef):
                visit(node.body, (*chain, node.name),
                      by_name and (node.name.startswith(TEST_CLASS_PREFIX)
                                   or any(map(_is_test_case, node.bases))))
            else:
                for block in _blocks(node):
                    visit(block, chain, by_name)

    visit(tree.body, (), True)
    return found


def _blocks(node):
    """The statement lists directly inside a compound statement, whatever
    its kind: bodies, else branches, finally, except handlers and match
    cases."""
    for name in ("body", "orelse", "finalbody"):
        value = getattr(node, name, None)
        if isinstance(value, list):
            yield value
    for name in ("handlers", "cases"):
        for part in getattr(node, name, None) or ():
            yield part.body


def _xfails_first(function) -> bool:
    first = function.body[0] if function.body else None
    if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
            and len(function.body) > 1:
        first = function.body[1]
    call = first.value if isinstance(first, ast.Expr) else None
    return isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) \
        and call.func.attr == "xfail" and getattr(call.func.value, "id", "") == "pytest"


def _is_test_case(base) -> bool:
    """Whether a class base names a ``unittest.TestCase`` (or a subclass
    named for it): pytest collects such a class whatever its own name."""
    name = base.attr if isinstance(base, ast.Attribute) else getattr(base, "id", "")
    return name.endswith("TestCase")


def _read_tests(repo: Path, base: str, head: str, changed: list) -> dict:
    """{path: (tests, changed lines)} for each changed test file, read
    before any step runs, or {path: finding} for one that cannot be read or
    parsed."""
    read = {}
    for path in changed:
        try:
            source = (repo / path).read_text(encoding="utf-8", errors="replace")
            tests = _tests_in(source)
        except OSError as error:
            read[path] = _run_finding(path, 1, 1, f"{path} cannot be read, so its tests "
                                      "cannot be told apart", str(error))
            continue
        except SyntaxError as error:
            read[path] = _run_finding(path, error.lineno or 1, error.lineno or 1,
                                      f"{path} does not parse, so its tests cannot run",
                                      str(error))
            continue
        read[path] = (tests, _changed_lines(repo, base, head, path))
    return read


# --- R5-R33: the guard rules -------------------------------------------------

def _guard_findings(repo: Path, changed: list, ast_grep, timeout):
    """(findings, available): a finding per guard-rule hit in ``changed``,
    and whether every guard rule ran. A rule that could not run is one
    finding saying so, and ``available`` is False."""
    # The lint walk never searches these directories (rules.ALWAYS_EXCLUDED
    # anywhere, rules.ROOT_EXCLUDED at the root), so no rule can check a
    # test file in one; the run checks below still see it.
    searchable = [path for path in changed if not _lint_excluded(path)]
    if not searchable:
        return [], True
    try:
        rules, principles = guard_rules()
        result = run_rules(repo, rules, principles, searchable, ast_grep=ast_grep,
                           ast_grep_timeout=timeout)
    except (RuleError, LintError) as error:
        return [_unavailable(searchable[0], str(error))], False
    findings, seen = [], {}
    for v in result.violations:
        # The message quotes only the match, so two identical hits in one
        # file would share a claim, and so a fingerprint; the count keeps
        # each one its own finding.
        key = (v.path, v.rule_id, v.message)
        seen[key] = seen.get(key, 0) + 1
        again = f" (occurrence {seen[key]} in this file)" if seen[key] > 1 else ""
        findings.append({
            "lens": LENS, "severity": "IMPORTANT", "path": v.path,
            "start_line": v.line, "end_line": v.end_line,
            "claim": f"{v.rule_id}: {v.message}{again}",
            "evidence": v.match, "fix": v.fix, "verification_level": "script",
            "resolves": None, "regression": False,
        })
    if result.skipped:
        findings.append(_unavailable(searchable[0], "; ".join(
            f"rule {skipped.rule_id}: {skipped.reason}" for skipped in result.skipped)))
    return findings, not result.skipped


def _lint_excluded(path: str) -> bool:
    parts = path.split("/")
    return parts[0] in ROOT_EXCLUDED or any(part in ALWAYS_EXCLUDED for part in parts[:-1])


def _unavailable(path: str, reason: str) -> dict:
    return {"lens": LENS, "severity": "CRITICAL", "path": path, "start_line": 1,
            "end_line": 1,
            "claim": "the R5-R33 guard checks could not run over the changed test files, "
                     "so round 0 has not shown them free of the shapes that make a test "
                     "pass without checking anything",
            "evidence": reason, "fix": None, "verification_level": "script",
            "resolves": None, "regression": False}


# --- R5-R33: what the suite run shows ----------------------------------------

def _test_files_at(repo: Path, head: str) -> list:
    """Every test file in ``head``'s tree, the candidates a JUnit case's
    class name is attributed among."""
    out = git(repo, "ls-tree", "-r", "-z", "--name-only", "--full-tree", head)
    paths = [p.decode("utf-8", "surrogateescape") for p in out.split(b"\0") if p]
    return sorted(p for p in paths if TEST_FILE.fullmatch(p))


def _attribute(cases: list, test_files: list):
    """({path: [case, ...]}, {path: [case, ...]}): each case attributed to
    the test file whose dotted module name its class name is, or begins
    (followed by a dot), the longest such name winning; and the cases that
    fit two or more files equally, under each of those files.

    By class name only (Rob, 2026-11-23): pytest names a case for the module
    that collects it, while xunit1's ``file`` names the file that defines
    the test function (an inherited, imported or star-imported test names
    its base module's file), and other JUnit families have no ``file``. The
    longest match keeps a sibling package's cases (tests.test_a.test_inner,
    under tests/test_a/) and a dotted directory's (tests.v1.2.test_x, under
    tests/v1.2/) from being taken for tests/test_a.py or tests/v1.py. A
    module-level entry (pytest's report on collecting the module itself) has
    no class name and is named for the module, so it is attributed by name."""
    dotted = {path: path[:-len(".py")].replace("/", ".") for path in test_files}
    owned, tied = {}, {}
    for case in cases:
        key = case["classname"] or case["name"]
        fits = [path for path, name in dotted.items()
                if key == name or key.startswith(name + ".")]
        if not fits:
            continue
        longest = max(len(dotted[path]) for path in fits)
        best = [path for path in fits if len(dotted[path]) == longest]
        for path in best:
            (owned if len(best) == 1 else tied).setdefault(path, []).append(case)
    return owned, tied


def _run_findings(changed: list, read: dict, test_files: list, junit) -> list:
    """A finding for each changed test module that collected nothing or
    whose results cannot be attributed to it alone, each changed test that
    was skipped or not run, and a suite whose results cannot be read.
    Nothing to check when no test file changed."""
    if not changed:
        return []
    cases = _junit_cases(junit)
    if isinstance(cases, str):
        return [_run_finding(changed[0], 1, 1, f"the full suite's per-test results cannot be "
                             f"read ({cases}), so round 0 cannot show that the changed tests "
                             "ran", "the suite step's JUnit XML")]
    owned, tied = _attribute(cases, test_files)
    if not owned and not tied:
        named = sorted({c["classname"] or c["name"] for c in cases})
        return [_run_finding(changed[0], 1, 1, "the full suite's per-test results cannot be "
                             "read (no test case is named for a test file in the checkout, as "
                             "when pytest's rootdir is not the checkout's root), so round 0 "
                             "cannot show that the changed tests ran",
                             "class names: " + ", ".join(named[:5]))]
    findings = []
    for path in changed:
        module = path[:-len(".py")].replace("/", ".")
        if isinstance(read[path], dict):
            findings.append(read[path])
            continue
        tests, touched = read[path]
        if path in tied:
            findings.append(_run_finding(
                path, 1, 1, f"the full suite's results cannot be attributed to {path} alone "
                "(a test case's name fits it and another test file equally), so round 0 "
                "cannot show whether its tests ran",
                "; ".join(f"{c['classname'] or c['name']}::{c['name']}"
                          for c in tied[path][:5])))
            continue
        in_module = owned.get(path, [])
        # A module-level entry (named for the module, no class) is pytest's
        # report on collecting the module itself: an error, or a skip of the
        # whole module. Only the other entries are tests.
        whole = [c for c in in_module if c["name"] == module and not c["classname"]]
        if not [c for c in in_module if c not in whole]:
            skipped = [c for c in whole if c["kind"] == "skipped"]
            if skipped:
                findings.append(_run_finding(
                    path, 1, 1, f"{path} was skipped as a whole in the full suite's run, so "
                    "none of its tests ran", skipped[0]["message"] or "collection skipped"))
            else:
                findings.append(_run_finding(
                    path, 1, 1, f"{path} collected no test in the full suite's run, so "
                    "nothing in it ran", f"no test case under {module} in the JUnit XML"
                    + (f"; collection error: {whole[0]['message']}" if whole else "")))
            continue
        for chain, name, first, last, by_name, xfails_first in tests:
            if not touched.intersection(range(first, last + 1)):
                continue
            classname = ".".join((module, *chain))
            runs = [c for c in in_module
                    if c["classname"] == classname and c["name"].split("[", 1)[0] == name]
            node = "::".join((path, *chain, name))
            if not runs:
                if not by_name:
                    continue
                findings.append(_run_finding(
                    path, first, last, f"{node} was not in the full suite's run (deselected "
                    "or never collected), so the change to it was not tested",
                    f"no test case {classname}.{name} in the JUnit XML"))
                continue
            skipped = [c for c in runs if c["kind"] == "skipped"
                       or (xfails_first and c["kind"] == "xfailed")]
            if skipped:
                findings.append(_run_finding(
                    path, first, last, f"{node} was skipped in the full suite's run, so the "
                    "change to it was not tested",
                    "; ".join(f"{c['name']}: {c['message']}" for c in skipped[:5])))
    return findings


def _run_finding(path, first, last, claim, evidence) -> dict:
    return {"lens": LENS, "severity": "CRITICAL", "path": path, "start_line": first,
            "end_line": last, "claim": claim, "evidence": evidence or "(none)",
            "fix": None, "verification_level": "reproduced", "resolves": None,
            "regression": False}


def _junit_cases(junit):
    """Every ``testcase`` in ``junit`` as {file, classname, name, kind,
    message}, ``kind`` one of passed, failed, error, skipped, xfailed (an
    expected failure that ran; one marked ``run=False``, reported
    ``[NOTRUN]``, never ran and is ``skipped``); or a string saying why
    there are none to read."""
    if junit is None:
        return "the suite step returned no JUnit XML"
    if not isinstance(junit, bytes):
        return f"the JUnit XML is {type(junit).__name__}, not bytes"
    if len(junit) > MAX_JUNIT_BYTES:
        return f"the JUnit XML is {len(junit)} bytes, over {MAX_JUNIT_BYTES}"
    if b"<!DOCTYPE" in junit or b"<!ENTITY" in junit:
        return "the JUnit XML declares a DOCTYPE or an entity, which pytest never writes"
    try:
        root = ElementTree.fromstring(junit)
    except ElementTree.ParseError as error:
        return f"the JUnit XML does not parse: {error}"
    cases = []
    for case in root.iter("testcase"):
        kind, message = "passed", ""
        for child in case:
            # An xfail ran and failed as expected, so it counts as run;
            # one with run=False (message "[NOTRUN] ...") never ran.
            if child.tag == "skipped" and child.get("type") == "pytest.xfail" \
                    and not child.get("message", "").startswith("[NOTRUN]"):
                kind, message = "xfailed", child.get("message", "")
            elif child.tag == "skipped":
                kind, message = "skipped", " ".join(
                    part for part in (child.get("message", ""), (child.text or "").strip()[:300])
                    if part)
            elif child.tag in ("failure", "error") and kind == "passed":
                kind, message = ("failed" if child.tag == "failure" else "error",
                                 child.get("message", ""))
        cases.append({"file": case.get("file", ""), "classname": case.get("classname", ""),
                      "name": case.get("name", ""), "kind": kind, "message": message})
    if not cases:
        return "the JUnit XML holds no test case at all"
    return cases


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
