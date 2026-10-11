"""E3-R2 (cm-fkd.3): the PR review step verifies a sweep fix's scope, in code.

The acceptance criterion is that a sweep fix which changes a function
signature, adds a dependency, or edits a file with no violation is rejected
at the PR review step, independently of the prompt. So the fixes here are
written by the test, not by a model — the verifier must not care who made
them — and each out-of-scope one must be refused with the right kind and a
location, while the minimal fix of a planted violation must pass.

**Fixture provenance.** The adapter tests run on a git repository made from
a copy of `fixtures/sample-repo` with tests/test_lint.py's regex rules
installed (the same five planted violations tests/test_sweep.py pins), and
the work list comes from the real `scripts/curamoir-lint` over that commit.
Line numbers quoted below are the fixture's, from PLANTED.json: billing.py
15 (`print`), 37 (`raise RuntimeError(...)`), 22 (the discount bug, which is
not a lint violation), handlers.py 22 and 31, and handlers_v2.py's name.
The pure-core tests build their own small inputs inline.
"""

import subprocess
from dataclasses import dataclass

import pytest

from curamoir.fix_scope import (
    ADDED,
    DELETED,
    DEPENDENCY_CHANGE,
    FILE_ADDED,
    FILE_DELETED,
    FILE_NOT_IN_WORK_LIST,
    MODIFIED,
    NEW_DEPENDENCY,
    OUTSIDE_VIOLATION,
    SIGNATURE_CHANGE,
    UNVERIFIABLE,
    WINDOW,
    FileChange,
    Hunk,
    _only_header_statements,
    check_scope,
    compute_hunks,
    is_dependency_file,
    parse_hunks,
    script_package,
    verify_fix,
)
from curamoir.principles import load_principles

BILLING = "src/sample/core/billing.py"
HANDLERS = "src/sample/web/handlers.py"
VARIANT = "src/sample/web/handlers_v2.py"


# --- the adapter, on a real git repository and the real lint ---------------------
#
# `swept` (tests/conftest.py) is the fixture as a git repository at one
# commit, and the work list the real lint gives for it.

def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
                          text=True).stdout


def edit(repo, path, old, new):
    target = repo / path
    text = target.read_text(encoding="utf-8")
    assert text.count(old) == 1, f"{old!r} is not exactly once in {path}"
    target.write_text(text.replace(old, new), encoding="utf-8")


def verdict(repo, work):
    return verify_fix(repo, "HEAD", work, load_principles(repo))


def kinds_at(result):
    return {(b.kind, b.path, b.line) for b in result.breaches}


def test_the_minimal_fix_of_a_planted_violation_is_in_scope(swept):
    """The print on billing.py:15 replaced by the module logger, on the same
    line: what E3-R2 means by a scoped fix."""
    repo, work = swept
    edit(repo, BILLING, 'print("calculating line total")', 'logger.info("calculating line total")')

    result = verdict(repo, work)

    assert result.ok, result.render()
    assert result.paths == (BILLING,)


def test_fixing_every_planted_violation_in_place_is_in_scope(swept):
    """Every line-level plant fixed, including chaining a cause, which edits
    the `except` line above the `raise` (inside WINDOW), and the variant file
    deleted, which is the fix for a violation that is the file's name."""
    repo, work = swept
    edit(repo, BILLING, 'print("calculating line total")', 'logger.info("calculating line total")')
    edit(repo, BILLING, "    except OSError:\n        raise RuntimeError(\"could not load rates\")",
         "    except OSError as error:\n"
         "        raise RuntimeError(f\"could not load rates from {path}\") from error")
    edit(repo, HANDLERS, 'print(f"request failed: {exception}")',
         'logger.error("request failed: %s", exception)')
    edit(repo, HANDLERS, '    except IndexError:\n        raise ValueError("bad token")',
         '    except IndexError as error:\n        raise ValueError(f"bad token: {raw!r}") from error')
    (repo / VARIANT).unlink()

    result = verdict(repo, work)

    assert result.ok, result.render()
    assert result.paths == (BILLING, HANDLERS, VARIANT)


def test_a_signature_change_is_rejected_even_beside_the_violation(swept):
    """Acceptance: `def line_total` is two lines above the print, inside the
    window, so only the signature check can catch this."""
    repo, work = swept
    edit(repo, BILLING, "def line_total(unit_price, quantity):",
         "def line_total(unit_price, quantity, log=None):")
    edit(repo, BILLING, 'print("calculating line total")', 'logger.info("calculating line total")')

    result = verdict(repo, work)

    assert (SIGNATURE_CHANGE, BILLING, 13) in kinds_at(result)
    assert "line_total" in result.render()


def test_a_signature_change_is_allowed_where_a_principle_permits_it(swept):
    repo, work = swept
    principles = repo / ".curamoir" / "principles.md"
    text = principles.read_text(encoding="utf-8")
    # The first principle in the fixture is structured-logging, whose
    # violation is the print two lines below `def line_total`.
    principles.write_text(text.replace(
        "- **tier:** principle\n- **promoted_on:** null\n",
        "- **tier:** principle\n- **promoted_on:** null\n- **permits:** `signature_change`\n",
        1), encoding="utf-8")
    assert load_principles(repo).principles[0].permits == ("signature_change",)
    git(repo, "commit", "-qam", "permit")
    edit(repo, BILLING, "def line_total(unit_price, quantity):",
         "def line_total(unit_price, quantity, log=None):")

    result = verdict(repo, work)

    first = load_principles(repo).principles[0].id
    assert first == "structured-logging", "the permission landed on another principle"
    assert result.ok, result.render()


def test_a_permission_elsewhere_in_the_file_does_not_cover_this_definition(swept):
    """errors-carry-context permits signature changes and has a violation in
    billing.py, at line 37; `line_total` (lines 13 to 16) is not near it."""
    repo, work = swept
    principles = repo / ".curamoir" / "principles.md"
    text = principles.read_text(encoding="utf-8")
    head, tail = text.split("## errors-carry-context", 1)
    tail = tail.replace("- **promoted_on:** null\n",
                        "- **promoted_on:** null\n- **permits:** `signature_change`\n", 1)
    principles.write_text(head + "## errors-carry-context" + tail, encoding="utf-8")
    git(repo, "commit", "-qam", "permit")
    edit(repo, BILLING, "def line_total(unit_price, quantity):",
         "def line_total(unit_price, quantity, log=None):")

    assert (SIGNATURE_CHANGE, BILLING, 13) in kinds_at(verdict(repo, work))


def test_adding_a_dependency_to_pyproject_and_the_lockfile_is_rejected(swept):
    """Acceptance: a new dependency, declared and locked."""
    repo, work = swept
    edit(repo, "pyproject.toml", 'requires-python = ">=3.12"\n',
         'requires-python = ">=3.12"\ndependencies = ["structlog>=24"]\n')
    (repo / "uv.lock").write_text("version = 1\n", encoding="utf-8")

    found = kinds_at(verdict(repo, work))

    assert (DEPENDENCY_CHANGE, "pyproject.toml", None) in found
    assert (DEPENDENCY_CHANGE, "uv.lock", None) in found
    assert (FILE_ADDED, "uv.lock", None) in found


def test_adding_a_package_json_dependency_is_rejected(swept):
    repo, work = swept
    (repo / "package.json").write_text('{"dependencies": {"pino": "^9"}}\n', encoding="utf-8")

    assert (DEPENDENCY_CHANGE, "package.json", None) in kinds_at(verdict(repo, work))


def test_importing_a_package_the_repo_does_not_use_is_a_new_dependency(swept):
    """No manifest touched, but `structlog` is imported nowhere in the
    repository: a dependency in all but the declaration."""
    repo, work = swept
    edit(repo, BILLING, '    print("calculating line total")',
         '    import structlog\n    structlog.get_logger().info("calculating line total")')

    result = verdict(repo, work)

    assert {b.kind for b in result.breaches} == {NEW_DEPENDENCY}
    assert result.breaches[0].path == BILLING and "structlog" in result.breaches[0].detail


def test_importing_the_standard_library_or_the_repos_own_modules_is_not(swept):
    repo, work = swept
    edit(repo, BILLING, '    print("calculating line total")',
         '    import sys\n    from sample import config\n'
         '    logger.info("calculating line total %s", sys.argv[0])')

    result = verdict(repo, work)

    assert result.ok, result.render()


def test_editing_a_file_with_no_violation_is_rejected(swept):
    """Acceptance: config.py has no planted violation."""
    repo, work = swept
    path = "src/sample/config.py"
    (repo / path).write_text((repo / path).read_text(encoding="utf-8") + "# tidy\n",
                             encoding="utf-8")

    assert kinds_at(verdict(repo, work)) == {(FILE_NOT_IN_WORK_LIST, path, None)}


def test_a_refactor_far_from_the_violation_is_rejected(swept):
    """billing.py is on the work list, but line 22 (the discount bug) is 7
    lines from the print and 15 from the raise: fixing a bug the lint did
    not report is beyond the violation, however welcome."""
    repo, work = swept
    edit(repo, BILLING, "return total * (1 - percent)", "return total * (1 - percent / 100)")

    assert kinds_at(verdict(repo, work)) == {(OUTSIDE_VIOLATION, BILLING, 22)}


def test_adding_a_file_is_rejected(swept):
    repo, work = swept
    (repo / "src" / "sample" / "log.py").write_text("import logging\n", encoding="utf-8")

    assert kinds_at(verdict(repo, work)) == {(FILE_ADDED, "src/sample/log.py", None)}


def test_a_staged_new_file_is_rejected_too(swept):
    repo, work = swept
    (repo / "helpers.py").write_text("x = 1\n", encoding="utf-8")
    git(repo, "add", "helpers.py")

    assert kinds_at(verdict(repo, work)) == {(FILE_ADDED, "helpers.py", None)}


def test_deleting_a_file_whose_violations_are_in_its_text_is_rejected(swept):
    repo, work = swept
    (repo / BILLING).unlink()

    assert kinds_at(verdict(repo, work)) == {(FILE_DELETED, BILLING, None)}


def test_a_rename_is_a_deletion_and_an_addition(swept):
    """Renaming the variant to a new name keeps the variant: the new path was
    never on the work list."""
    repo, work = swept
    git(repo, "mv", VARIANT, "src/sample/web/pagination.py")

    found = kinds_at(verdict(repo, work))

    assert (FILE_ADDED, "src/sample/web/pagination.py", None) in found
    assert not any(kind == FILE_DELETED for kind, _, _ in found)


def test_a_mode_change_cannot_be_verified(swept):
    repo, work = swept
    (repo / BILLING).chmod(0o755)

    found = verdict(repo, work)

    assert {b.kind for b in found.breaches} == {UNVERIFIABLE}


def test_a_fix_that_does_not_parse_cannot_be_verified(swept):
    repo, work = swept
    edit(repo, BILLING, 'print("calculating line total")', 'logger.info("calculating line total"')

    assert {b.kind for b in verdict(repo, work).breaches} == {UNVERIFIABLE}


def test_a_logger_added_to_the_module_header_is_in_scope(swept):
    """The no-print rule's own fix puts `logger = logging.getLogger(...)` at
    module level, far from the print; the header allows imports and
    module-level assignments."""
    repo, work = swept
    edit(repo, BILLING, "TAX_RATE = 0.2\n",
         "TAX_RATE = 0.2\nimport logging as _logging\naudit = _logging.getLogger('audit')\n")
    edit(repo, BILLING, 'print("calculating line total")', 'audit.info("calculating line total")')

    assert verdict(repo, work).ok


@pytest.mark.parametrize("inserted", [
    "TAX_RATE = 0.3\n",
    "from decimal import Decimal as TAX_RATE\n",
    "line_total = None\n",
])
def test_a_header_insertion_that_rebinds_an_existing_name_is_not(swept, inserted):
    """Each would change what a later name means while looking like a new
    import or assignment; inserted after line 10, out of the print's window."""
    repo, work = swept
    edit(repo, BILLING, "TAX_RATE = 0.2\n", "TAX_RATE = 0.2\n" + inserted)

    assert kinds_at(verdict(repo, work)) == {(OUTSIDE_VIOLATION, BILLING, 10)}


@pytest.mark.parametrize("inserted", [
    '_boot = __import__("os").system("true")\n',
    "helper = lambda a: a * 2\n",
    "squares = [n * n for n in range(3)]\n",
    "env = __import__('os').environ\n",
    "label = f\"{__import__('os').getcwd()}\"\n",
    "level: __import__('os').getcwd() = 1\n",
    "audit = logging.getLogger(__import__('os').getcwd())\n",
    "audit = logging.getLogger(name=__name__)\n",
    "audit = logging.root.manager.getLogger('x')\n",
    "audit = TAX_RATE.getLogger('x')\n",
])
def test_a_header_insertion_that_runs_code_at_import_is_not(swept, inserted):
    """S2: a header line runs when the module is imported — including by the
    sweep's own test step — so it may bind a new name only to a literal or a
    logger, never to an arbitrary expression. Each falls back to the window."""
    repo, work = swept
    edit(repo, BILLING, "TAX_RATE = 0.2\n", "TAX_RATE = 0.2\n" + inserted)

    assert kinds_at(verdict(repo, work)) == {(OUTSIDE_VIOLATION, BILLING, 10)}


@pytest.mark.parametrize("inserted", [
    "RETRIES = 3\n",
    "GREETING = 'hi'\n",
    "TIMEOUT: float = -1.5\n",
    "DEBUG = None\n",
    "LEVELS = ('info', 'debug')\n",
    "__all__ = ['line_total']\n",
    "audit: logging.Logger = logging.getLogger('audit')\n",
    "audit = logging.getLogger()\n",
])
def test_a_header_insertion_of_a_literal_or_a_logger_is_in_scope(swept, inserted):
    repo, work = swept
    edit(repo, BILLING, "TAX_RATE = 0.2\n", "TAX_RATE = 0.2\n" + inserted)

    assert verdict(repo, work).ok


def test_the_no_print_fix_in_a_module_without_a_logger_is_in_scope():
    """examples/python/no-print.yaml's own fix, verbatim, into a module that
    has neither the import nor the logger: both land in the header."""
    old = '"""m."""\nimport os\n\n\n\n\n\n\n\n\ndef f():\n    print(os.sep)\n'
    new = old.replace("import os\n", "import os\nimport logging\n\nlogger = logging.getLogger(__name__)\n")
    new = new.replace("print(os.sep)", "logger.info(os.sep)")

    assert check_scope([modified("m.py", old, new)], [V("m.py", 12)]).ok


def test_a_logger_from_a_module_merely_named_like_logging_is_not():
    """getLogger is trusted only on the standard `logging` module, under any
    alias; `import os as log` then `log.getLogger(...)` is someone else's call."""
    old = '"""m."""\nimport os as log\n'
    assert not _only_header_statements(["audit = log.getLogger(__name__)"], True,
                                       __import__("ast").parse(old))


@pytest.mark.parametrize("line,ok", [
    ('import { readFile } from "node:fs";', True),
    ("import fs from 'fs'", True),
    ('import "./polyfill.js";', True),
    ('const fs = require("fs");', True),
    ("const { join } = require('path')", True),
    ("// a comment", True),
    ('const run = require("child_process").execSync("touch /tmp/x");', False),
    ('import("./boot.js").then(m => m.go());', False),
    ("const logger = makeLogger();", False),
    ('require("./polyfill.js");', True),
])
def test_a_script_header_insertion_is_only_a_plain_import(line, ok):
    """The same rule for JS/TS, by line, without a parser: a static import or a
    `const x = require("…")` and nothing chained after it."""
    assert _only_header_statements([line], False) is ok


def test_repeating_an_import_the_module_already_has_is_harmless(swept):
    repo, work = swept
    edit(repo, BILLING, "TAX_RATE = 0.2\n", "TAX_RATE = 0.2\nimport logging\n")

    assert verdict(repo, work).ok


def test_a_function_added_to_the_module_header_is_not(swept):
    """After the import on line 6: well outside the print's window (12-18),
    so the header rule decides, and a function is not an import or an
    assignment. It is also a new definition, so a signature change."""
    repo, work = swept
    edit(repo, BILLING, "import logging\n",
         "import logging\n\n\ndef helper():\n    return 1\n")

    result = verdict(repo, work)

    assert OUTSIDE_VIOLATION in result.kinds()
    assert SIGNATURE_CHANGE in result.kinds()


def test_no_change_is_in_scope_and_changes_nothing(swept):
    repo, work = swept

    result = verdict(repo, work)

    assert result.ok and result.paths == ()


def test_the_verdict_renders_every_breach_with_its_location(swept):
    repo, work = swept
    edit(repo, BILLING, "return total * (1 - percent)", "return total * (1 - percent / 100)")
    (repo / "new.py").write_text("x = 1\n", encoding="utf-8")

    result = verdict(repo, work)
    text = result.render()

    assert text.startswith("out of scope: 2 breach(es)")
    assert f"{OUTSIDE_VIOLATION} {BILLING}:22" in text
    assert f"{FILE_ADDED} new.py" in text
    assert result.as_dict()["breaches"][0]["kind"] in (FILE_ADDED, OUTSIDE_VIOLATION)
    assert result.kinds() == (FILE_ADDED, OUTSIDE_VIOLATION)


# --- the pure core -------------------------------------------------------------------

@dataclass(frozen=True)
class V:
    path: str
    line: int
    end_line: int = None
    col: int = 5
    end_col: int = 10
    principle_id: str = "p"

    def __post_init__(self):
        if self.end_line is None:
            object.__setattr__(self, "end_line", self.line)


def modified(path, old, new):
    return FileChange(path, MODIFIED, old, new)


def numbered(count, prefix="x"):
    return "".join(f"{prefix}{n} = {n}\n" for n in range(1, count + 1))


@pytest.mark.parametrize("line,ok", [(10, True), (10 + WINDOW, True), (10 + WINDOW + 1, False),
                                     (10 - WINDOW, True), (10 - WINDOW - 1, False)])
def test_the_window_is_exactly_WINDOW_lines_either_side(line, ok):
    old = numbered(30)
    new = old.replace(f"x{line} = {line}\n", f"x{line} = {line} + 0\n")

    result = check_scope([modified("m.py", old, new)], [V("m.py", 10)])

    assert result.ok is ok, result.render()


def test_a_multi_line_violation_is_measured_from_both_ends():
    old = numbered(30)
    new = old.replace("x16 = 16\n", "x16 = 0\n")

    assert check_scope([modified("m.py", old, new)], [V("m.py", 10, end_line=13)]).ok


def test_an_end_at_column_1_is_the_line_before_it():
    """end_col is exclusive: a match ending at 14:1 ends on line 13."""
    old = numbered(30)
    new = old.replace("x17 = 17\n", "x17 = 0\n")

    tight = V("m.py", 10, end_line=14, end_col=1)
    assert not check_scope([modified("m.py", old, new)], [tight]).ok
    assert check_scope([modified("m.py", old, new)], [V("m.py", 10, end_line=14, end_col=2)]).ok


def test_each_breach_names_the_base_line_of_its_hunk():
    old = numbered(40)
    new = old.replace("x30 = 30\n", "x30 = 0\n").replace("x2 = 2\n", "")

    found = check_scope([modified("m.py", old, new)], [V("m.py", 15)]).breaches

    assert [(b.kind, b.line) for b in found] == [(OUTSIDE_VIOLATION, 2), (OUTSIDE_VIOLATION, 30)]


def test_a_dependency_file_on_the_work_list_is_still_refused():
    """Even with a violation in it: the whole file is out of bounds."""
    change = modified("pyproject.toml", 'name = "a"\n', 'name = "b"\n')

    found = check_scope([change], [V("pyproject.toml", 1)]).breaches

    assert [b.kind for b in found] == [DEPENDENCY_CHANGE]


@pytest.mark.parametrize("path", ["pyproject.toml", "sub/package.json", "uv.lock", "poetry.lock",
                                  "requirements.txt", "requirements-dev.txt",
                                  "requirements/base.in", "constraints.txt", "yarn.lock",
                                  "pnpm-lock.yaml", "package-lock.json", "Pipfile.lock",
                                  "setup.cfg", "setup.py"])
def test_dependency_files_are_recognised_by_name(path):
    assert is_dependency_file(path)


@pytest.mark.parametrize("path", ["src/requirements.py", "docs/package.md", "lock.py"])
def test_other_files_are_not_dependency_files(path):
    assert not is_dependency_file(path)


def test_deleting_a_file_level_violation_is_the_fix():
    file_level = V("a_v2.py", 1, end_line=1, col=1, end_col=1)

    assert check_scope([FileChange("a_v2.py", DELETED, "x = 1\n", None)], [file_level]).ok


def test_an_added_file_is_refused_even_if_a_violation_names_it():
    """Cannot happen with a work list from the base commit, but the rule is
    the rule: an added path's text was not linted."""
    found = check_scope([FileChange("n.py", ADDED, None, "x = 1\n")], [V("n.py", 1)])

    assert [b.kind for b in found.breaches] == [FILE_ADDED]


@pytest.mark.parametrize("before,after,name", [
    ("def f(a):\n    return a\n", "def f(a, b=1):\n    return a\n", "f"),
    ("def f(a=1):\n    return a\n", "def f(a=2):\n    return a\n", "f"),
    ("def f(a: int):\n    return a\n", "def f(a: str):\n    return a\n", "f"),
    ("def f(a) -> int:\n    return a\n", "def f(a) -> str:\n    return a\n", "f"),
    ("def f(a):\n    return a\n", "async def f(a):\n    return a\n", "f"),
    ("def f(a):\n    return a\n", "@cache\ndef f(a):\n    return a\n", "f"),
    ("class C(A):\n    pass\n", "class C(B):\n    pass\n", "C"),
    ("class C:\n    def m(self):\n        pass\n", "class C:\n    def m(self, x):\n        pass\n",
     "C.m"),
    ("def f(a):\n    return a\n", "def g(a):\n    return a\n", "f"),
])
def test_python_signature_changes_are_seen(before, after, name):
    found = check_scope([modified("m.py", before, after)], [V("m.py", 1)]).breaches

    assert any(b.kind == SIGNATURE_CHANGE and f"`{name}`" in b.detail for b in found), found


def test_a_body_change_is_not_a_signature_change():
    before = "def f(a):\n    print(a)\n    return a\n"
    after = "def f(a):\n    logger.info(a)\n    return a\n"

    assert check_scope([modified("m.py", before, after)], [V("m.py", 2)]).ok


def test_a_definition_under_a_module_level_if_is_compared_too():
    before = "if X:\n    def f(a):\n        return a\n"
    after = "if X:\n    def f(a, b):\n        return a\n"

    found = check_scope([modified("m.py", before, after)], [V("m.py", 3)]).breaches

    assert [b.kind for b in found] == [SIGNATURE_CHANGE]


@pytest.mark.parametrize("before,after", [
    ("export function f(a: string): void {\n  g();\n}\n",
     "export function f(a: string, b: number): void {\n  g();\n}\n"),
    ("const f = (a) => {\n  g();\n};\n", "const f = (a, b) => {\n  g();\n};\n"),
    ("class C {\n  m(a: string): void {\n    g();\n  }\n}\n",
     "class C {\n  m(a: string, b: string): void {\n    g();\n  }\n}\n"),
    ("export interface I {\n  a: string;\n}\n", "export interface J {\n  a: string;\n}\n"),
])
def test_a_changed_js_ts_declaration_line_is_a_signature_change(before, after):
    found = check_scope([modified("m.ts", before, after)], [V("m.ts", 1)]).breaches

    assert [b.kind for b in found] == [SIGNATURE_CHANGE]


def test_a_changed_js_ts_statement_is_not():
    before = 'export function f(m: string): void {\n  console.log(`x ${m}`);\n}\n'
    after = 'export function f(m: string): void {\n  logger.info(`x ${m}`);\n}\n'

    assert check_scope([modified("m.ts", before, after)], [V("m.ts", 2)]).ok


def test_a_new_js_ts_package_import_is_a_new_dependency_and_a_relative_one_is_not():
    before = "export function f(m: string): void {\n  console.log(m);\n}\n"
    local = "import { logger } from './log';\n" + before.replace("console.log", "logger.info")
    package = "import pino from 'pino';\n" + before.replace("console.log", "pino().info")

    assert check_scope([modified("m.ts", before, local)], [V("m.ts", 2)]).ok
    found = check_scope([modified("m.ts", before, package)], [V("m.ts", 2)]).breaches
    assert [b.kind for b in found] == [NEW_DEPENDENCY]
    assert check_scope([modified("m.ts", before, package)], [V("m.ts", 2)],
                       known_modules=frozenset({"pino"})).ok


@pytest.mark.parametrize("specifier,package", [
    ("pino", "pino"), ("lodash/fp", "lodash"), ("@scope/pkg/sub", "@scope/pkg"),
    ("node:fs/promises", "fs"), ("./log", None), ("../x", None), ("/abs", None),
])
def test_script_package_names(specifier, package):
    assert script_package(specifier) == package


def test_parse_hunks_reads_git_minus_U0_headers():
    diff = (
        "diff --git a/m.py b/m.py\n--- a/m.py\n+++ b/m.py\n"
        "@@ -3 +3 @@ def f():\n-a\n+b\n"
        "@@ -10,0 +11,2 @@\n+c\n+d\n"
        "@@ -20,3 +22,0 @@\n-e\n-f\n-g\n"
    )

    assert parse_hunks(diff) == (Hunk(3, 1, 3, 1), Hunk(10, 0, 11, 2), Hunk(20, 3, 22, 0))


def test_compute_hunks_agrees_with_git_on_the_simple_cases():
    old = "a\nb\nc\nd\n"

    assert compute_hunks(old, "a\nB\nc\nd\n") == (Hunk(2, 1, 2, 1),)
    assert compute_hunks(old, "a\nb\nx\nc\nd\n") == (Hunk(2, 0, 3, 1),)
    assert compute_hunks(old, "a\nd\n") == (Hunk(2, 2, 1, 0),)
