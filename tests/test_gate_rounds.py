"""gate/rounds.py (cm-8ap.15): round 0, the deterministic pre-pass.

Spec R5-R1 (lint, types, the full suite and the provenance mechanical checks
before any model; any failure is PREFLIGHT_FAILED with the tool output as
the record) and R5-R33 (the execution and guard checks over the tests a
change touched). The steps run for real here: the suite is pytest itself,
in a throwaway repository, through a run function like the one the gate job
will supply, so "skipped", "deselected" and "collected nothing" are what
pytest reports, not what a test assumed it would.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from curamoir.gate import record, rounds
from curamoir.gate.rounds import Round0Error, Step, StepRun, run_round0, write_round0
from tests.test_lint import needs_ast_grep

AST_GREP = shutil.which("ast-grep")


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def commit(repo, files, message):
    """Write `files` (path to text, None to delete) and commit them; the
    new HEAD."""
    for name, text in files.items():
        path = repo / name
        if text is None:
            path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text), encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Test")
    commit(root, {"tests/test_old.py": """\
        import pytest

        def test_old():
            assert True

        @pytest.mark.skip(reason="skipped before this change")
        def test_old_skipped():
            pass
    """, "src/app.py": "VALUE = 1\n"}, "base")
    return root


def pytest_run(step, checkout):
    """The run function: argv in the checkout, combined output, and for the
    suite pytest's xunit1 JUnit XML, as the gate job's would return them."""
    argv = list(step.argv)
    junit = None
    if step.suite:
        junit = checkout.parent / f"junit-{time.monotonic_ns()}.xml"
        argv += [f"--junitxml={junit}", "-o", "junit_family=xunit1", f"--rootdir={checkout}"]
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    started = time.monotonic()
    done = subprocess.run(argv, cwd=checkout, env=env, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, timeout=step.timeout)
    xml = junit.read_bytes() if junit is not None and junit.exists() else None
    return StepRun(done.returncode, done.stdout, time.monotonic() - started, xml)


PYTEST = (sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider")
SUITE = Step("suite", PYTEST, 120, suite=True)
LINT = Step("lint", (sys.executable, "-c", "print('lint: clean')"), 60)


def round0(repo, base, *, steps=(LINT, SUITE), run=pytest_run, ast_grep=AST_GREP,
           test_paths=("tests",)):
    return run_round0(repo, base=base, head=_git(repo, "rev-parse", "HEAD"), steps=steps,
                      run=run, test_paths=test_paths, ast_grep=ast_grep)


def claims(result):
    return sorted((f["path"], f["severity"], f["claim"].split(":")[0]) for f in result.findings)


# --- a clean change ----------------------------------------------------------

@needs_ast_grep
def test_a_change_whose_tests_run_passes_round_0(repo):
    base = _git(repo, "rev-parse", "HEAD")
    head = commit(repo, {"tests/test_new.py": """\
        def test_new():
            assert 1 + 1 == 2
    """}, "a good test")

    result = round0(repo, base)

    assert result.passed, result.findings
    assert result.findings == () and result.failed_steps == ()
    assert result.round["kind"] == "preflight" and result.round["head"] == head
    assert result.round["lenses"] == {"provenance": {"model": None, "status": "ok",
                                                     "findings": []}}
    names = [s["name"] for s in result.round["preflight"]["steps"]]
    assert names == ["lint", "suite"]
    assert result.round["preflight"]["steps"][0]["output_tail"] == "lint: clean\n"


# --- the steps ---------------------------------------------------------------

@needs_ast_grep
def test_a_failing_step_fails_round_0_and_every_step_still_runs(repo):
    base = _git(repo, "rev-parse", "HEAD")
    commit(repo, {"src/app.py": "VALUE = 2\n"}, "no test change")
    loud = Step("types", (sys.executable, "-c",
                          "import sys; sys.stdout.write('x' * 5000 + 'END'); sys.exit(3)"), 60)

    result = round0(repo, base, steps=(loud, LINT, SUITE))

    assert not result.passed
    assert result.failed_steps == ("types",)
    types, lint, suite = result.round["preflight"]["steps"]
    assert (types["exit"], lint["exit"], suite["exit"]) == (3, 0, 0)
    assert types["output_bytes"] == 5003
    assert len(types["output_tail"]) == record.PREFLIGHT_TAIL_CHARS
    assert types["output_tail"].endswith("xEND")
    assert types["argv"] == list(loud.argv)
    assert result.findings == ()


@needs_ast_grep
def test_a_red_suite_fails_round_0(repo):
    base = _git(repo, "rev-parse", "HEAD")
    commit(repo, {"tests/test_new.py": "def test_red():\n    assert False\n"}, "red")

    result = round0(repo, base)

    assert result.failed_steps == ("suite",) and not result.passed
    assert result.findings == ()


def test_a_step_killed_or_never_started_has_no_exit(repo):
    base = _git(repo, "rev-parse", "HEAD")

    def run(step, checkout):
        if step.suite:
            return StepRun(None, b"killed after 120s", 120.0, None)
        return StepRun(0, b"", 0.1)

    result = round0(repo, base, run=run, ast_grep=None)

    assert result.round["preflight"]["steps"][1]["exit"] is None
    assert result.failed_steps == ("suite",)


# --- R5-R33: the guard rules over the changed test files ------------------------

@needs_ast_grep
def test_a_guard_rule_hit_in_a_changed_test_file_blocks(repo):
    base = _git(repo, "rev-parse", "HEAD")
    commit(repo, {"tests/test_new.py": """\
        import pytest
        from pathlib import Path

        ROOT = Path(__file__).resolve().parents[2]

        def test_guarded():
            if not ROOT.is_dir():
                return
            assert ROOT
    """}, "guarded test")

    result = round0(repo, base)

    assert not result.passed and result.failed_steps == ()
    assert claims(result) == [("tests/test_new.py", "IMPORTANT", "no-early-return-in-test"),
                              ("tests/test_new.py", "IMPORTANT", "no-layout-relative-path")]
    for finding in result.findings:
        assert finding["lens"] == "provenance" and finding["status"] == "open"
        assert len(finding["fingerprint"]) == 16
        assert finding["verification_level"] == "script"


@needs_ast_grep
def test_an_unchanged_test_file_is_not_checked(repo):
    """test_old.py has a skipped test from before the change; round 0 is
    about what the change touched."""
    base = _git(repo, "rev-parse", "HEAD")
    commit(repo, {"tests/test_new.py": "def test_new():\n    assert True\n"}, "new")

    assert round0(repo, base).passed


def test_the_guard_rules_not_running_blocks_round_0(repo):
    base = _git(repo, "rev-parse", "HEAD")
    commit(repo, {"tests/test_new.py": "def test_new():\n    assert True\n"}, "new")

    result = round0(repo, base, ast_grep=None)

    assert not result.passed
    assert result.round["lenses"]["provenance"]["status"] == "UNAVAILABLE"
    assert [f["severity"] for f in result.findings] == ["CRITICAL"]
    assert "ast-grep is not installed" in result.findings[0]["evidence"]


def test_no_changed_test_file_needs_no_guard_rules(repo):
    base = _git(repo, "rev-parse", "HEAD")
    commit(repo, {"src/app.py": "VALUE = 3\n"}, "source only")

    def run(step, checkout):
        return StepRun(0, b"", 0.0, b"<testsuite><testcase file='tests/test_old.py' "
                                    b"classname='tests.test_old' name='test_old'/></testsuite>")

    assert round0(repo, base, run=run, ast_grep=None).passed


# --- R5-R33: what the suite run shows -------------------------------------------

@needs_ast_grep
def test_a_changed_test_that_is_skipped_blocks_and_an_unchanged_one_does_not(repo):
    base = _git(repo, "rev-parse", "HEAD")
    commit(repo, {"tests/test_old.py": """\
        import pytest

        def test_old():
            assert True

        @pytest.mark.skip(reason="skipped before this change")
        def test_old_skipped():
            pass

        @pytest.mark.skipif(True, reason="not on this checkout")
        def test_added_and_skipped():
            assert True

        @pytest.mark.xfail(reason="a known bug", strict=True)
        def test_added_and_xfailed():
            assert False
    """}, "a skipped test")

    result = round0(repo, base)

    assert [(f["path"], f["severity"], f["claim"]) for f in result.findings] == [
        ("tests/test_old.py", "CRITICAL",
         "tests/test_old.py::test_added_and_skipped was skipped in the "
         "full suite's run, so the change to it was not tested")]
    (finding,) = result.findings
    assert (finding["start_line"], finding["end_line"]) == (10, 12)
    assert "not on this checkout" in finding["evidence"]
    assert finding["verification_level"] == "reproduced"


@needs_ast_grep
def test_an_empty_parametrise_list_is_a_skipped_test(repo):
    base = _git(repo, "rev-parse", "HEAD")
    commit(repo, {"tests/test_cases.py": """\
        import pytest

        CASES = []
        assert CASES == []

        @pytest.mark.parametrize("case", CASES)
        def test_case(case):
            assert case
    """}, "no cases")

    result = round0(repo, base)

    assert [f["claim"].split(" was ")[0] for f in result.findings] == \
        ["tests/test_cases.py::test_case"]
    assert "got empty parameter set" in result.findings[0]["evidence"]


@needs_ast_grep
def test_a_changed_test_left_out_of_the_run_blocks(repo):
    base = _git(repo, "rev-parse", "HEAD")
    commit(repo, {"tests/test_new.py": """\
        class TestThings:
            def test_kept(self):
                assert True

            def test_left_out(self):
                assert True
    """}, "two tests")
    deselecting = Step("suite", (*PYTEST, "-k", "not left_out"), 120, suite=True)

    result = round0(repo, base, steps=(deselecting,))

    assert [f["claim"] for f in result.findings] == [
        "tests/test_new.py::TestThings::test_left_out was not in the full suite's run "
        "(deselected or never collected), so the change to it was not tested"]


@needs_ast_grep
def test_a_changed_module_that_collects_nothing_blocks(repo):
    base = _git(repo, "rev-parse", "HEAD")
    commit(repo, {"tests/test_empty.py": "HELPER = 1\n"}, "no tests in it")

    result = round0(repo, base)

    assert [f["claim"] for f in result.findings] == [
        "tests/test_empty.py collected no test in the full suite's run, so nothing in it ran"]


@needs_ast_grep
def test_a_changed_module_that_fails_to_import_collects_nothing(repo):
    base = _git(repo, "rev-parse", "HEAD")
    commit(repo, {"tests/test_broken.py": "import not_a_module\n\ndef test_x():\n    pass\n"},
           "broken")

    result = round0(repo, base)

    assert result.failed_steps == ("suite",)
    assert [f["claim"].split(" collected")[0] for f in result.findings] == ["tests/test_broken.py"]
    assert "collection failure" in result.findings[0]["evidence"]


@needs_ast_grep
def test_a_test_that_only_lost_lines_counts_as_changed(repo):
    base = commit(repo, {"tests/test_old.py": """\
        import pytest

        @pytest.mark.skip(reason="r")
        def test_shrinking():
            assert 1
            assert 2
    """}, "two asserts")
    commit(repo, {"tests/test_old.py": """\
        import pytest

        @pytest.mark.skip(reason="r")
        def test_shrinking():
            assert 1
    """}, "one assert")

    result = round0(repo, base)

    assert [f["claim"].split(" was ")[0] for f in result.findings] == \
        ["tests/test_old.py::test_shrinking"]


@pytest.mark.parametrize(("junit", "reason"), [
    (None, "returned no JUnit XML"),
    (b"<testsuite", "does not parse"),
    (b"<testsuite/>", "no test case at all"),
    (b"<testsuite><testcase classname='a' name='b'/></testsuite>", "not junit_family=xunit1"),
    (b"<!DOCTYPE x [<!ENTITY a 'b'>]><testsuite/>", "declares a DOCTYPE"),
])
def test_suite_results_that_cannot_be_read_block(repo, junit, reason):
    base = _git(repo, "rev-parse", "HEAD")
    commit(repo, {"tests/test_new.py": "def test_new():\n    assert True\n"}, "new")

    def run(step, checkout):
        return StepRun(0, b"", 0.0, junit)

    result = round0(repo, base, run=run, steps=(SUITE,), ast_grep=None)

    run_findings = [f for f in result.findings if "per-test results" in f["claim"]]
    assert len(run_findings) == 1 and reason in run_findings[0]["claim"]
    assert not result.passed


# --- how round 0 is called ----------------------------------------------------

@pytest.mark.parametrize(("steps", "message"), [
    ((), "needs its steps"),
    ((LINT,), "exactly one step is the full suite"),
    ((SUITE, Step("suite2", PYTEST, 60, suite=True)), "exactly one step is the full suite"),
    ((SUITE, Step("suite", ("x",), 60)), "distinct non-empty"),
    ((SUITE, Step("lint", ["x"], 60)), "non-empty tuple of strings"),
    ((SUITE, Step("lint", ("x",), 0)), "positive number"),
    ([SUITE, "lint"], "is a gate.rounds.Step"),
])
def test_invalid_steps_are_refused_before_anything_runs(repo, steps, message):
    ran = []

    with pytest.raises(Round0Error, match=message):
        run_round0(repo, base=_git(repo, "rev-parse", "HEAD"), head=_git(repo, "rev-parse", "HEAD"),
                   steps=steps, run=lambda step, checkout: ran.append(step), test_paths=("tests",),
                   ast_grep=None)
    assert ran == []


def test_round_0_runs_only_on_a_clean_checkout_of_head(repo):
    base = _git(repo, "rev-parse", "HEAD")
    head = commit(repo, {"src/app.py": "VALUE = 9\n"}, "change")

    def refuse(step, checkout):
        raise AssertionError("nothing may run")

    with pytest.raises(Round0Error, match="not head"):
        run_round0(repo, base=head, head=base, steps=(SUITE,), run=refuse, test_paths=('tests',), ast_grep=None)
    (repo / "src" / "app.py").write_text("VALUE = 10\n", encoding="utf-8")
    with pytest.raises(Round0Error, match="tracked changes"):
        run_round0(repo, base=base, head=head, steps=(SUITE,), run=refuse, test_paths=('tests',), ast_grep=None)
    with pytest.raises(Round0Error, match="full commit id"):
        run_round0(repo, base="main", head=head, steps=(SUITE,), run=refuse, test_paths=('tests',), ast_grep=None)


def test_a_run_function_must_return_a_step_run(repo):
    base = _git(repo, "rev-parse", "HEAD")

    with pytest.raises(Round0Error, match="not a StepRun"):
        round0(repo, base, run=lambda step, checkout: (0, b""), ast_grep=None)


# --- the record ---------------------------------------------------------------

@needs_ast_grep
def test_a_failed_round_0_ends_the_gate_preflight_failed_on_heads_tree(repo):
    base = _git(repo, "rev-parse", "HEAD")
    head = commit(repo, {"tests/test_new.py": "def test_red():\n    assert False\n"}, "red")
    record.start(repo, "cm-x", repo_name="repo", branch="main", base=base, head=head)

    written = write_round0(repo, "cm-x", round0(repo, base))

    assert written["verdict"]["state"] == "PREFLIGHT_FAILED"
    assert written["verdict"]["tree_sha"] == _git(repo, "rev-parse", "HEAD^{tree}")
    assert written["verdict"]["head_sha"] == head
    on_disk = record.load(repo, "cm-x")
    assert on_disk["rounds"][0]["preflight"]["steps"][1]["name"] == "suite"
    assert on_disk["rounds"][0]["lenses"]["provenance"]["model"] is None
    assert record.check_verdict(repo, "cm-x") is not None


@needs_ast_grep
def test_a_passed_round_0_is_recorded_with_no_verdict(repo):
    base = _git(repo, "rev-parse", "HEAD")
    head = commit(repo, {"tests/test_new.py": "def test_new():\n    assert True\n"}, "new")
    record.start(repo, "cm-y", repo_name="repo", branch="main", base=base, head=head)

    written = write_round0(repo, "cm-y", round0(repo, base))

    assert "verdict" not in written and len(written["rounds"]) == 1


def test_the_guard_rules_are_the_bundled_tests_actually_run_rules():
    rules, principles = rounds.guard_rules()

    assert sorted(r.id for r in rules) == sorted(
        p.stem for p in (Path(__file__).resolve().parents[1] / "examples" / "python").glob("*.yaml")
        if "principle: tests-actually-run" in p.read_text(encoding="utf-8"))
    assert len(rules) == 5
    assert principles.by_id("tests-actually-run") is not None


# --- fix round 1 (correctness review) ------------------------------------------

@needs_ast_grep
def test_a_conditional_module_level_skip_added_to_an_existing_module_blocks(repo):
    """The usual form: a skip under a module-level `if`, the tests themselves
    unchanged. Both the guard rule and the run see it."""
    base = _git(repo, "rev-parse", "HEAD")
    commit(repo, {"tests/test_old.py": """\
        import sys

        import pytest

        if sys.platform.startswith("linux") or True:
            pytest.skip("not here", allow_module_level=True)

        def test_old():
            assert True

        @pytest.mark.skip(reason="skipped before this change")
        def test_old_skipped():
            pass
    """}, "skip the module")

    result = round0(repo, base)

    assert sorted((f["severity"], f["claim"].split(",")[0]) for f in result.findings) == [
        ("CRITICAL", "tests/test_old.py was skipped as a whole in the full suite's run"),
        ("IMPORTANT", 'no-module-level-skip: pytest.skip("not here"'),
    ]
    whole = next(f for f in result.findings if f["severity"] == "CRITICAL")
    assert "not here" in whole["evidence"]


@needs_ast_grep
def test_results_whose_paths_are_not_the_checkouts_are_unreadable_not_empty(repo):
    """pytest names files from its rootdir: a suite run with another rootdir
    (here tests/, through a tests/pytest.ini) is not 'collected nothing'."""
    base = _git(repo, "rev-parse", "HEAD")
    commit(repo, {"tests/pytest.ini": "[pytest]\n",
                  "tests/test_new.py": "def test_new():\n    assert True\n"}, "own rootdir")

    def elsewhere(step, checkout):
        junit = checkout.parent / "elsewhere.xml"
        env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
        done = subprocess.run([*step.argv, "tests", f"--junitxml={junit}", "-o",
                               "junit_family=xunit1"], cwd=checkout, env=env,
                              capture_output=True, timeout=120)
        return StepRun(done.returncode, done.stdout, 1.0, junit.read_bytes())

    result = round0(repo, base, steps=(SUITE,), run=elsewhere)

    assert result.failed_steps == ()
    assert [f["claim"].split(" (")[0] for f in result.findings] == [
        "the full suite's per-test results cannot be read"]


@needs_ast_grep
def test_identical_guard_hits_in_one_file_are_separate_findings(repo):
    base = _git(repo, "rev-parse", "HEAD")
    commit(repo, {"tests/test_new.py": """\
        def test_new():
            try:
                assert f()
            except Exception:
                pass
            try:
                assert f()
            except Exception:
                pass
    """}, "two swallows")

    result = round0(repo, base)

    prints = [f["fingerprint"] for f in result.findings]
    assert len(prints) == 2 and len(set(prints)) == 2


def test_guard_rules_that_cannot_run_are_one_finding(repo):
    base = _git(repo, "rev-parse", "HEAD")
    commit(repo, {"tests/test_new.py": "def test_new():\n    assert True\n"}, "new")

    result = round0(repo, base, ast_grep=None)

    guard = [f for f in result.findings if "guard checks could not run" in f["claim"]]
    assert len(guard) == 1 and guard[0]["evidence"].count("ast-grep is not installed") == 5


@needs_ast_grep
def test_a_changed_test_file_that_cannot_be_read_is_a_finding_not_a_crash(repo):
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "tests").mkdir(exist_ok=True)
    (repo / "tests" / "test_link.py").symlink_to("nowhere.py")
    commit(repo, {"tests/test_new.py": "def test_new():\n    assert True\n"}, "a dangling link")

    result = round0(repo, base)

    assert not result.passed
    assert any(f["claim"] == "tests/test_link.py cannot be read, so its tests cannot be told "
               "apart" for f in result.findings)


@needs_ast_grep
def test_a_test_file_the_lint_walk_excludes_is_left_to_the_run_checks(repo):
    base = _git(repo, "rev-parse", "HEAD")
    commit(repo, {"build/test_built.py": "def test_built():\n    return\n"}, "under build/")

    result = round0(repo, base, test_paths=("tests", "build"))

    assert result.round["lenses"]["provenance"]["status"] == "ok"
    assert not [f for f in result.findings if "guard checks" in f["claim"]]
    assert not [f for f in result.findings if f["claim"].startswith("no-early-return")]


# --- fix round 1 (edge-cases review) -------------------------------------------

@needs_ast_grep
def test_a_step_that_rewrites_a_changed_test_file_cannot_hide_it(repo):
    """The checks read head's files before any step runs repository code,
    and a step that changes the checkout fails the round."""
    base = _git(repo, "rev-parse", "HEAD")
    commit(repo, {"tests/test_new.py": """\
        import pytest

        pytest.skip("off", allow_module_level=True)

        def test_new():
            assert True
    """}, "a skipped module")
    rewrite = Step("format", (sys.executable, "-c",
                              "open('tests/test_new.py', 'w').write("
                              "'def test_new():\\n    assert True\\n')"), 60)

    result = round0(repo, base, steps=(rewrite, SUITE))

    claims_ = [f["claim"] for f in result.findings]
    assert any(c.startswith("no-module-level-skip:") for c in claims_)
    moved = [f for f in result.findings if f["claim"].startswith("a step changed the checkout")]
    assert len(moved) == 1 and moved[0]["path"] == "tests/test_new.py"
    assert "tests/test_new.py" in moved[0]["evidence"]
    assert not result.passed


@needs_ast_grep
def test_an_xfail_that_is_never_run_is_a_test_that_did_not_run(repo):
    base = _git(repo, "rev-parse", "HEAD")
    commit(repo, {"tests/test_new.py": """\
        import pytest

        @pytest.mark.xfail(run=False, reason="later")
        def test_never_run():
            assert 1 == 2
    """}, "an xfail that never runs")

    result = round0(repo, base)

    assert [f["claim"] for f in result.findings] == [
        "tests/test_new.py::test_never_run was skipped in the full suite's run, so the change "
        "to it was not tested"]
    assert "[NOTRUN] later" in result.findings[0]["evidence"]


@needs_ast_grep
def test_a_skipped_test_in_a_unittest_class_of_any_name_is_checked(repo):
    base = _git(repo, "rev-parse", "HEAD")
    commit(repo, {"tests/test_new.py": """\
        import unittest

        class Checks(unittest.TestCase):
            @unittest.skip("off")
            def test_skipped(self):
                pass

        class Helper:
            def test_not_collected(self):
                pass
    """}, "a unittest class")

    result = round0(repo, base)

    assert [f["claim"] for f in result.findings] == [
        "tests/test_new.py::Checks::test_skipped was skipped in the full suite's run, so the "
        "change to it was not tested"]


@needs_ast_grep
def test_a_test_defined_under_a_module_level_block_is_checked(repo):
    base = _git(repo, "rev-parse", "HEAD")
    commit(repo, {"tests/test_new.py": """\
        import sys

        import pytest

        if sys.version_info >= (3,):
            @pytest.mark.skip(reason="under an if")
            def test_under_if():
                pass
        try:
            import json
        except ImportError:
            pass
        else:
            @pytest.mark.skip(reason="under a try's else")
            def test_under_else():
                pass
    """}, "tests under blocks")

    result = round0(repo, base)

    assert sorted(f["claim"].split(" was ")[0] for f in result.findings) == [
        "tests/test_new.py::test_under_else", "tests/test_new.py::test_under_if"]


@needs_ast_grep
def test_a_file_named_like_a_test_outside_the_test_paths_is_not_checked(repo):
    """A fixture named test_*.py (this repository has some) is not one of
    the suite's tests, and may hold the guard shapes on purpose."""
    base = _git(repo, "rev-parse", "HEAD")
    commit(repo, {"fixtures/sample/tests/test_planted.py": """\
        import pytest
        pytestmark = pytest.mark.skip(reason="a planted violation")

        def test_planted():
            return
    """, "tests/test_new.py": "def test_new():\n    assert True\n"}, "a fixture")

    assert round0(repo, base).passed


@pytest.mark.parametrize("test_paths", ["tests", (), ("/tests",), ("../x",), ("a//b",), (3,)])
def test_test_paths_must_be_plain_repo_relative_paths(repo, test_paths):
    base = _git(repo, "rev-parse", "HEAD")

    with pytest.raises(Round0Error, match=r"test path|test_paths"):
        round0(repo, base, run=lambda step, checkout: None, ast_grep=None, test_paths=test_paths)


@pytest.mark.parametrize("duration", [None, float("nan"), float("inf"), -1, "1"])
def test_a_run_function_giving_no_real_duration_is_refused(repo, duration):
    base = _git(repo, "rev-parse", "HEAD")

    with pytest.raises(Round0Error, match="not a finite number of seconds"):
        round0(repo, base, run=lambda step, checkout: StepRun(0, b"", duration), ast_grep=None)
