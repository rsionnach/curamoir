"""The test guard rules in examples/python/ (cm-8ap.15, spec R5-R33).

Five `curamoir-lint` rules for the shapes in which a test passes without
checking anything: a module-level skip, a path that depends on where the file
sits in the source tree, an early return in a test body, an except that
swallows, and a parametrise list computed at collection with no minimum. The
R5 gate's round 0 runs them over the test files a change touches, with no
model, and every hit blocks.

Each rule carries its own `fires_on` and `silent_on` snippets, which the
runner checks before the rule searches anything. These tests go further, on
each shape's variants and near misses, through the real loader, runner and
ast-grep, and assert exactly which lines are reported: "at least one match"
would pass a rule that also fires on half the file.
"""

import shutil
import subprocess
import textwrap

from curamoir.lint import run_lint
from curamoir.principles import parse_principles
from curamoir.rules import parse_rule

# The shared marker: skipped exactly where test_lint's ast-grep tests are,
# and required (not skipped) in CI.
from tests.repo_root import REPO_ROOT
from tests.test_lint import needs_ast_grep

ROOT = REPO_ROOT
EXAMPLES = ROOT / "examples" / "python"
STARTER = ROOT / "templates" / "principles.md"
PRINCIPLE = "tests-actually-run"

GUARD_RULES = ("no-module-level-skip", "no-layout-relative-path", "no-early-return-in-test",
               "no-bare-except-pass", "parametrize-needs-minimum")


def guard_repo(tmp_path, files):
    """A repository with the starter principles, the five guard rules
    installed flat as the README says, and `files` (path to text)."""
    repo = tmp_path / "repo"
    rules = repo / ".curamoir" / "rules"
    rules.mkdir(parents=True)
    shutil.copyfile(STARTER, repo / ".curamoir" / "principles.md")
    for rule_id in GUARD_RULES:
        shutil.copyfile(EXAMPLES / f"{rule_id}.yaml", rules / f"{rule_id}.yaml")
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(textwrap.dedent(text), encoding="utf-8")
    return repo


def reported(tmp_path, files):
    """{(path, line, rule id)} the guard rules report over `files`; every
    rule must have run (none skipped, so none failed its own snippets)."""
    result = run_lint(guard_repo(tmp_path, files), ast_grep=shutil.which("ast-grep"))
    assert result.skipped == ()
    assert set(result.checked) == set(GUARD_RULES)
    found = [(v.path, v.line, v.rule_id) for v in result.violations]
    # One report per shape: a rule that matched one chain twice (the layout
    # rule's `.parent.parent.parent` holds two `.parent.parent`s) fails here.
    assert len(found) == len(set(found)), sorted(found)
    return set(found)


# --- the rule files ----------------------------------------------------------

def test_the_five_guard_rules_ship_and_cite_tests_actually_run():
    starter = {p.id for p in parse_principles(STARTER.read_text(encoding="utf-8")).principles}
    assert PRINCIPLE in starter
    for rule_id in GUARD_RULES:
        rule = parse_rule((EXAMPLES / f"{rule_id}.yaml").read_text(encoding="utf-8"),
                          source=f"{rule_id}.yaml", known_principles=starter)
        assert rule.id == rule_id
        assert rule.principle == PRINCIPLE
        assert rule.severity == "error"
        # A rule object, so its own snippets are what prove it loads right.
        assert rule.rule_object and rule.fires_on and rule.silent_on
        assert rule.files == ("**/test_*.py", "**/*_test.py")


@needs_ast_grep
def test_each_rule_passes_its_own_snippets_on_the_real_ast_grep(tmp_path):
    # No files at all: the snippet checks are the only searching done.
    assert reported(tmp_path, {}) == set()


@needs_ast_grep
def test_only_test_files_are_checked(tmp_path):
    shapes = """\
        import pytest
        pytestmark = pytest.mark.skip(reason="x")
        def test_x():
            return
    """
    found = reported(tmp_path, {"src/app.py": shapes, "tests/helpers.py": shapes,
                                "tests/test_app.py": shapes, "tests/app_test.py": shapes})

    assert {path for path, _, _ in found} == {"tests/test_app.py", "tests/app_test.py"}


# --- each shape, its variants and its near misses -----------------------------

@needs_ast_grep
def test_module_level_skips(tmp_path):
    found = reported(tmp_path, {"tests/test_skips.py": """\
        import pytest
        pytestmark = pytest.mark.skip(reason="later")
        pytestmark = [pytest.mark.slow, pytest.mark.skipif(True, reason="x")]
        pytestmark: list = [pytest.mark.skipif(True, reason="x")]
        pytest.skip("not here", allow_module_level=True)
        yaml = pytest.importorskip("yaml")
        pytestmark = pytest.mark.slow
        pytestmark = pytest.mark.xfail(reason="known")
        pytestmark = pytest.mark.skip_slow
        pytestmark = pytest.mark.noskipif
        pytest.skip("not module level", allow_module_level=False)

        def test_inside():
            pytest.skip("a skip in a test is the run's to report")
            pytest.importorskip("yaml")

        class TestMarked:
            pytestmark = pytest.mark.skip(reason="a class's own mark")

        if sys.platform == "win32":
            pytest.skip("under a module-level if", allow_module_level=True)
        try:
            import yaml
        except ImportError:
            pytest.skip("under a module-level try", allow_module_level=True)
    """})

    assert found == {("tests/test_skips.py", line, "no-module-level-skip")
                     for line in (2, 3, 4, 5, 6, 21, 25)}


@needs_ast_grep
def test_layout_relative_paths(tmp_path):
    found = reported(tmp_path, {"tests/test_paths.py": """\
        import os
        from pathlib import Path
        ROOT = Path(__file__).resolve().parents[1]
        ECOSYSTEM = Path(__file__).resolve().parents[2] / "opensrm"
        UP = Path(__file__).parent.parent
        UPPER = Path(__file__).parent.parent.parent / "x"
        HERE = Path(__file__).parent
        SAME = Path(__file__).parents[0]
        DATA = HERE / "data"
        OTHER = Path(elsewhere).parents[2]
    """})

    assert found == {("tests/test_paths.py", line, "no-layout-relative-path")
                     for line in (3, 4, 5, 6)}


@needs_ast_grep
def test_early_returns_in_test_bodies(tmp_path):
    found = reported(tmp_path, {"tests/test_returns.py": """\
        def test_guarded(case):
            if not case:
                return
            assert case

        async def test_async():
            return

        class TestInClass:
            def test_method(self):
                return None

            def helper(self):
                return 1

        def test_with_a_nested_helper():
            def helper():
                return 1
            assert helper() == 1

        def test_with_a_lambda():
            assert (lambda: 1)()

        def _not_a_test():
            return 2

        def helper_for_test_x():
            return 3

        def helper():
            test_value = 4
            return test_value

        def testcamel():
            return
    """})

    assert found == {("tests/test_returns.py", line, "no-early-return-in-test")
                     for line in (3, 7, 11, 35)}


@needs_ast_grep
def test_excepts_that_swallow(tmp_path):
    found = reported(tmp_path, {"tests/test_excepts.py": """\
        def test_excepts():
            try:
                assert f()
            except:
                pass
            try:
                assert f()
            except Exception:  # deliberately quiet
                ...
            try:
                assert f()
            except BaseException as error:
                pass
            try:
                assert f()
            except (Exception):
                pass
            try:
                assert f()
            except KeyError:
                pass
            try:
                assert f()
            except Exception:
                log()
            try:
                assert f()
            except:
                raise
            try:
                assert f()
            except Exception:
                pass  # the reason, after the pass
            try:
                assert f()
            except builtins.Exception:
                pass
    """})

    assert found == {("tests/test_excepts.py", line, "no-bare-except-pass")
                     for line in (4, 8, 12, 16, 32, 36)}


@needs_ast_grep
def test_parametrise_lists_computed_at_collection(tmp_path):
    found = reported(tmp_path, {
        "tests/test_computed.py": """\
            import pytest
            from cases import IMPORTED
            LITERAL = ["a", "b"]
            ANNOTATED: tuple = ("x",)
            CASES = sorted(DATA.glob("*.json"))

            @pytest.mark.parametrize("c", _paths(), ids=str)
            def test_call(c): ...

            @pytest.mark.parametrize("c", CASES)
            def test_bound_to_a_call(c): ...

            @pytest.mark.parametrize("c", [p for p in _paths()])
            def test_comprehension(c): ...

            @pytest.mark.parametrize("c", IMPORTED)
            def test_imported(c): ...

            @pytest.mark.parametrize("c", LITERAL)
            def test_bound_to_a_list(c): ...

            @pytest.mark.parametrize("c", ANNOTATED)
            def test_bound_to_a_tuple(c): ...

            @pytest.mark.parametrize(("a", "b"), [(1, 2)], ids=["one"])
            def test_literal(a, b): ...

            @mark.parametrize("c", _paths())
            def test_bare_mark(c): ...

            @pytest.mark.parametrize("c", argvalues=_paths())
            def test_by_keyword(c): ...

            @pytest.mark.parametrize("a,b", "xy")
            def test_string_values(a, b): ...

            @fixtures.parametrize("c", _paths())
            def test_not_a_mark(c): ...
        """,
        "tests/test_with_a_minimum.py": """\
            import pytest
            CASES = sorted(DATA.glob("*.json"))
            assert len(CASES) >= 3

            @pytest.mark.parametrize("c", CASES)
            def test_case(c): ...
        """,
        "tests/test_with_a_truthy_minimum.py": """\
            import pytest
            CASES = sorted(DATA.glob("*.json"))
            assert CASES  # not empty

            @pytest.mark.parametrize("c", CASES)
            def test_case(c): ...
        """,
    })

    assert found == {("tests/test_computed.py", line, "parametrize-needs-minimum")
                     for line in (7, 10, 13, 16, 28)}


@needs_ast_grep
def test_the_early_return_rule_on_a_real_test_file_from_the_corpus(tmp_path):
    """A real early return, from the replay corpus (opensrm-wo62-1,
    checkpoint C, which adds the file whole): the rule reports the `return`
    inside a test and not the ones in helpers nested in tests."""
    path = "tests/test_manifest_switch_blind_spots.py"
    diff = ROOT / "fixtures" / "r5-replay" / "opensrm-wo62-1-checkpointC" / "diff.patch"
    repo = tmp_path / "wo62"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "apply", f"--include={path}", str(diff)], cwd=repo, check=True)
    source = (repo / path).read_text(encoding="utf-8")

    found = reported(tmp_path / "lint", {path: source})

    returns = sorted(line for p, line, rule in found if rule == "no-early-return-in-test")
    assert returns == [381]
    lines = source.splitlines()
    assert lines[380].strip() == "return"
    # Real returns the rule must not report: helpers nested in tests.
    assert [n for n in (440, 442) if lines[n - 1].lstrip().startswith("return")] == [440, 442]
