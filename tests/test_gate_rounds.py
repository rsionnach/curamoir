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

import pytest

from curamoir.gate import record, rounds
from curamoir.gate.rounds import Round0Error, Step, StepRun, run_round0, write_round0
from tests.repo_root import REPO_ROOT
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
                          "import sys; sys.stdout.buffer.write(b'x' * 4997 + 'é'.encode() + "
                          "b'\\xffEND'); sys.exit(3)"), 60)

    result = round0(repo, base, steps=(loud, LINT, SUITE))

    assert not result.passed
    assert result.failed_steps == ("types",)
    types, lint, suite = result.round["preflight"]["steps"]
    assert (types["exit"], lint["exit"], suite["exit"]) == (3, 0, 0)
    # Bytes, not characters: 4997 + 2 (é) + 1 (an invalid byte) + 3.
    assert types["output_bytes"] == 5003
    assert len(types["output_tail"]) == record.PREFLIGHT_TAIL_CHARS
    assert types["output_tail"].endswith("x\u00e9\ufffdEND")
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
    (b"<testsuite><testcase classname='a' name='b'/></testsuite>",
     "no test case is named for a test file"),
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
        p.stem for p in (REPO_ROOT / "examples" / "python").glob("*.yaml")
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
    # The guard rules refuse a path that is not a regular file: that is
    # their being unable to run, which blocks on its own.
    assert result.round["lenses"]["provenance"]["status"] == "UNAVAILABLE"
    assert [f for f in result.findings if "guard checks could not run" in f["claim"]]


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


# --- fix round 2 (edge-cases review) -------------------------------------------

@needs_ast_grep
def test_skipped_tests_in_subclasses_of_a_base_or_an_alias_are_checked(repo):
    """Which classes pytest collects is the run's to say: a skip it reports
    is matched to its function wherever the class came from."""
    base = _git(repo, "rev-parse", "HEAD")
    commit(repo, {"tests/test_new.py": """\
        import unittest
        from unittest import TestCase as TC

        class Base(unittest.TestCase):
            pass

        class Checks(Base):
            @unittest.skip("via a base")
            def test_via_base(self):
                pass

        class Aliased(TC):
            @unittest.skip("via an alias")
            def test_via_alias(self):
                pass

        class Mixin:
            def test_never_collected(self):
                pass
    """}, "indirect TestCase subclasses")

    result = round0(repo, base)

    assert sorted(f["claim"].split(" was ")[0] for f in result.findings) == [
        "tests/test_new.py::Aliased::test_via_alias", "tests/test_new.py::Checks::test_via_base"]


@needs_ast_grep
def test_tests_under_any_module_level_block_are_checked(repo):
    base = _git(repo, "rev-parse", "HEAD")
    commit(repo, {"tests/test_new.py": """\
        import pytest

        for _ in [0]:
            @pytest.mark.skip(reason="under a for")
            def test_under_for():
                pass

        while True:
            @pytest.mark.skip(reason="under a while")
            def test_under_while():
                pass
            break

        match 1:
            case 1:
                @pytest.mark.skip(reason="under a match")
                def test_under_match():
                    pass

        try:
            pass
        except* ValueError:
            pass
        else:
            @pytest.mark.skip(reason="under a try/except* else")
            def test_under_try_star():
                pass

        try:
            import not_installed_anywhere
        except ImportError:
            @pytest.mark.skip(reason="under an except handler")
            def test_under_except():
                pass
        finally:
            @pytest.mark.skip(reason="under a finally")
            def test_under_finally():
                pass
    """}, "tests under every block")

    result = round0(repo, base)

    assert sorted(f["claim"].split(" was ")[0] for f in result.findings) == [
        f"tests/test_new.py::test_under_{kind}"
        for kind in ("except", "finally", "for", "match", "try_star", "while")]


@needs_ast_grep
def test_an_imperative_xfail_first_is_a_test_that_did_not_run(repo):
    base = _git(repo, "rev-parse", "HEAD")
    commit(repo, {"tests/test_new.py": """\
        import pytest

        def test_xfails_first():
            \"\"\"Documented.\"\"\"
            pytest.xfail("later")
            assert 1 == 2

        @pytest.mark.xfail(reason="a known bug", strict=True)
        def test_xfails_after_running():
            assert 1 == 2
    """}, "an imperative xfail")

    result = round0(repo, base)

    assert [f["claim"].split(" was ")[0] for f in result.findings] == [
        "tests/test_new.py::test_xfails_first"]


@needs_ast_grep
def test_excluded_test_paths_and_the_whole_repository(repo):
    base = _git(repo, "rev-parse", "HEAD")
    commit(repo, {"tests/conftest.py": "collect_ignore = ['fixtures']\n",
                  "tests/fixtures/test_sample_data.py": "def test_sample():\n    return\n",
                  "tests/test_new.py": "def test_new():\n    assert True\n"}, "a fixture")

    blocked = round0(repo, base)
    excluded = run_round0(repo, base=base, head=_git(repo, "rev-parse", "HEAD"),
                          steps=(LINT, SUITE), run=pytest_run, test_paths=(".",),
                          test_excludes=("tests/fixtures",), ast_grep=AST_GREP)

    assert any(f["path"] == "tests/fixtures/test_sample_data.py" for f in blocked.findings)
    assert excluded.passed, excluded.findings
    # "." is the whole repository, not nothing: a shape outside tests/ is seen.
    commit(repo, {"other/test_elsewhere.py": "def test_elsewhere():\n    return\n"}, "elsewhere")
    whole = run_round0(repo, base=base, head=_git(repo, "rev-parse", "HEAD"),
                       steps=(LINT, SUITE), run=pytest_run, test_paths=(".",),
                       test_excludes=("tests/fixtures",), ast_grep=AST_GREP)
    assert [f["claim"].split(":")[0] for f in whole.findings
            if f["path"] == "other/test_elsewhere.py"] == ["no-early-return-in-test"]


@needs_ast_grep
def test_a_step_that_moves_head_fails_the_round(repo):
    base = _git(repo, "rev-parse", "HEAD")
    commit(repo, {"tests/test_new.py": "def test_new():\n    assert True\n"}, "new")
    move = Step("move", ("git", "commit", "-q", "--allow-empty", "-m", "moved"), 60)

    result = round0(repo, base, steps=(move, SUITE))

    moved = [f for f in result.findings if f["claim"].startswith("a step changed the checkout")]
    assert len(moved) == 1 and moved[0]["path"] == "."
    assert "after the steps, not" in moved[0]["evidence"]


# --- fix round 1 (provenance review) -------------------------------------------

@needs_ast_grep
def test_a_changed_test_file_named_with_the_test_suffix_is_checked(repo):
    base = _git(repo, "rev-parse", "HEAD")
    commit(repo, {"tests/thing_test.py": """\
        import pytest

        @pytest.mark.skip(reason="off")
        def test_thing():
            pass
    """}, "a *_test.py file")
    suite = Step("suite", (*PYTEST, "tests/thing_test.py", "tests/test_old.py"), 120, suite=True)

    result = round0(repo, base, steps=(suite,))

    assert [f["claim"].split(" was ")[0] for f in result.findings] == [
        "tests/thing_test.py::test_thing"]


@needs_ast_grep
def test_a_renamed_test_file_is_checked_under_its_new_name(repo):
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "mv", "tests/test_old.py", "tests/test_renamed.py")
    head = commit(repo, {"tests/test_renamed.py": """\
        import pytest

        def test_old():
            assert True

        @pytest.mark.skip(reason="skipped before this change")
        def test_old_skipped():
            pass

        @pytest.mark.skip(reason="added in the rename")
        def test_added_in_the_rename():
            pass
    """}, "rename")
    status = _git(repo, "diff", "--name-status", "-M", base, head)
    assert status.startswith("R"), status

    result = round0(repo, base)

    # A rename has no old lines to keep, so every test in it counts as changed.
    assert sorted(f["claim"].split(" was ")[0] for f in result.findings) == [
        "tests/test_renamed.py::test_added_in_the_rename",
        "tests/test_renamed.py::test_old_skipped"]


@needs_ast_grep
def test_a_test_left_out_of_a_unittest_class_of_any_name_blocks(repo):
    base = _git(repo, "rev-parse", "HEAD")
    commit(repo, {"tests/test_new.py": """\
        import unittest

        class Checks(unittest.TestCase):
            def test_kept(self):
                self.assertTrue(True)

            def test_left_out(self):
                self.assertTrue(True)
    """}, "a unittest class")
    deselecting = Step("suite", (*PYTEST, "-k", "not left_out"), 120, suite=True)

    result = round0(repo, base, steps=(deselecting,))

    assert [f["claim"].split(" was ")[0] for f in result.findings] == [
        "tests/test_new.py::Checks::test_left_out"]


def test_junit_xml_over_the_size_cap_is_refused(repo, monkeypatch):
    base = _git(repo, "rev-parse", "HEAD")
    commit(repo, {"tests/test_new.py": "def test_new():\n    assert True\n"}, "new")
    xml = (b"<testsuite><testcase file='tests/test_new.py' classname='tests.test_new' "
           b"name='test_new'/></testsuite>")
    monkeypatch.setattr(rounds, "MAX_JUNIT_BYTES", len(xml) - 1)

    result = round0(repo, base, steps=(SUITE,), ast_grep=None,
                    run=lambda step, checkout: StepRun(0, b"", 0.0, xml))

    claims_ = [f["claim"] for f in result.findings if "per-test results" in f["claim"]]
    assert len(claims_) == 1 and f"bytes, over {len(xml) - 1}" in claims_[0]


# --- final correctness (after provenance) --------------------------------------

@needs_ast_grep
def test_a_module_whose_tests_are_all_inherited_from_another_file_has_tests(repo):
    """xunit1's `file` is the file that defines the test function: a class
    here re-running another file's tests names that other file. The run's
    cases are this module's by their class, so it did collect tests."""
    commit(repo, {"tests/__init__.py": "", "tests/test_base.py": """\
        class TestBase:
            backend = "a"

            def test_x(self):
                assert self.backend
    """}, "a shared suite")
    base = _git(repo, "rev-parse", "HEAD")
    commit(repo, {"tests/test_b.py": """\
        from tests.test_base import TestBase


        class TestB(TestBase):
            backend = "b"
    """}, "the suite against another backend")

    result = round0(repo, base)

    assert result.passed, result.findings


@needs_ast_grep
def test_a_sibling_package_of_a_changed_module_is_not_its_tests(repo):
    """tests/test_a/ is not tests/test_a.py: a case under the package, whose
    class the module does not define, does not count as the module's."""
    base = _git(repo, "rev-parse", "HEAD")
    commit(repo, {"tests/test_a.py": "HELPER = 1\n",
                  "tests/test_a/__init__.py": "",
                  "tests/test_a/test_inner.py": "def test_inner():\n    assert True\n"},
           "a module beside a package of the same name")
    xml = (b"<testsuite><testcase file='tests/test_a/test_inner.py' "
           b"classname='tests.test_a.test_inner' name='test_inner'/>"
           b"<testcase file='tests/test_old.py' classname='tests.test_old' "
           b"name='test_old'/></testsuite>")

    result = round0(repo, base, steps=(SUITE,),
                    run=lambda step, checkout: StepRun(0, b"", 0.0, xml))

    assert [f["claim"] for f in result.findings if f["path"] == "tests/test_a.py"] == [
        "tests/test_a.py collected no test in the full suite's run, so nothing in it ran"]


@needs_ast_grep
def test_a_module_whose_tests_are_imported_from_another_file_has_tests(repo):
    """An imported test function runs under the importing module
    (classname tests.test_c), and an imported class under the module's name
    for it (tests.test_d.Shared): both are the module's, by name."""
    commit(repo, {"tests/__init__.py": "", "tests/test_base.py": """\
        def test_shared():
            assert True


        class TestBase:
            def test_x(self):
                assert True
    """}, "a shared suite")
    base = _git(repo, "rev-parse", "HEAD")
    commit(repo, {
        "tests/test_c.py": "from tests.test_base import test_shared  # noqa: F401\n",
        "tests/test_d.py": "from tests.test_base import TestBase as TestShared  # noqa: F401\n",
    }, "modules that import their tests")

    result = round0(repo, base)

    assert result.passed, result.findings



# --- attribution by class name (Rob, 2026-11-23) --------------------------------
#
# A JUnit case belongs to the test file whose dotted module name its class
# name is or begins, the longest winning; never to the file JUnit's `file`
# names (the defining file). The four shapes a module gets tests in, and the
# two collisions the longest match resolves.

@needs_ast_grep
def test_star_imported_and_bound_tests_are_the_importing_modules(repo):
    commit(repo, {"tests/__init__.py": "", "tests/test_base.py": """\
        class TestBase:
            def test_x(self):
                assert True
    """}, "a shared suite")
    base = _git(repo, "rev-parse", "HEAD")
    commit(repo, {
        "tests/test_star.py": "from tests.test_base import *  # noqa: F403\n",
        "tests/test_for.py": """\
            from tests import test_base

            for TestLoop in [test_base.TestBase]:
                pass
        """,
        "tests/test_with.py": """\
            import contextlib

            from tests import test_base

            with contextlib.nullcontext(test_base.TestBase) as TestWith:
                pass
        """,
    }, "modules whose tests arrive by star import and by binding")

    result = round0(repo, base)

    assert result.passed, result.findings


def _stub(xml):
    return lambda step, checkout: StepRun(0, b"", 0.0, xml)


def test_a_dotted_directorys_cases_are_not_the_shorter_modules(repo):
    """tests/test_v1.2/test_x.py reads as tests.test_v1.2.test_x, which begins
    with tests.test_v1, the name of tests/test_v1.py: the longer name wins,
    so the dotted directory's case is not taken for tests/test_v1.py."""
    base = _git(repo, "rev-parse", "HEAD")
    commit(repo, {"tests/test_v1.py": "HELPER = 1\n",
                  "tests/test_v1.2/test_x.py": "def test_x():\n    assert True\n"},
           "a dotted directory")
    xml = (b"<testsuite><testcase classname='tests.test_v1.2.test_x' name='test_x'/>"
           b"<testcase classname='tests.test_old' name='test_old'/></testsuite>")

    result = round0(repo, base, steps=(SUITE,), run=_stub(xml), ast_grep=None)

    run = [f for f in result.findings if "guard checks" not in f["claim"]]
    assert [f["claim"] for f in run] == [
        "tests/test_v1.py collected no test in the full suite's run, so nothing in it ran"]


def test_attribution_ignores_the_file_attribute(repo):
    """An inherited test's case names its base module's file; attributed by
    class name, it is the subclassing module's."""
    base = _git(repo, "rev-parse", "HEAD")
    commit(repo, {"tests/test_b.py": "from tests.test_old import test_old  # noqa: F401\n"},
           "an imported test")
    xml = (b"<testsuite><testcase classname='tests.test_b' name='test_old' "
           b"file='tests/test_old.py'/></testsuite>")

    result = round0(repo, base, steps=(SUITE,), run=_stub(xml), ast_grep=None)

    assert [f for f in result.findings if "guard checks" not in f["claim"]] == []


def test_a_case_two_files_fit_equally_cannot_be_attributed(repo):
    """tests/test_v1.test_x.py and tests/test_v1/test_x.py are both
    tests.test_v1.test_x: a case under that name is neither file's alone,
    which blocks, and is never read as "ran" or "collected no test"."""
    base = _git(repo, "rev-parse", "HEAD")
    commit(repo, {"tests/test_v1.test_x.py": "def test_x():\n    assert True\n",
                  "tests/test_v1/test_x.py": "def test_x():\n    assert True\n"},
           "two files with one dotted name")
    xml = (b"<testsuite><testcase classname='tests.test_v1.test_x' name='test_x'/>"
           b"</testsuite>")

    result = round0(repo, base, steps=(SUITE,), run=_stub(xml), ast_grep=None)

    run = sorted((f["path"], f["claim"].split(" (")[0]) for f in result.findings
                 if "guard checks" not in f["claim"])
    assert run == [
        ("tests/test_v1.test_x.py",
         "the full suite's results cannot be attributed to tests/test_v1.test_x.py alone"),
        ("tests/test_v1/test_x.py",
         "the full suite's results cannot be attributed to tests/test_v1/test_x.py alone")]
    assert not result.passed


def test_a_prefix_match_stops_at_a_dot(repo):
    """tests.test_abc.helpers begins with the characters of tests.test_a but
    not with tests.test_a followed by a dot: it is not tests/test_a.py's
    case (a doctest or custom python_files module, not a test file)."""
    base = _git(repo, "rev-parse", "HEAD")
    commit(repo, {"tests/test_a.py": "HELPER = 1\n"}, "a module with no tests")
    xml = (b"<testsuite><testcase classname='tests.test_abc.helpers' name='f'/>"
           b"<testcase classname='tests.test_old' name='test_old'/></testsuite>")

    result = round0(repo, base, steps=(SUITE,), run=_stub(xml), ast_grep=None)

    assert [f["claim"] for f in result.findings if f["path"] == "tests/test_a.py"
            and "guard checks" not in f["claim"]] == [
        "tests/test_a.py collected no test in the full suite's run, so nothing in it ran"]


def pytest_run_default_family(step, checkout):
    """The run function the module docstring documents: --junitxml and
    --rootdir, with pytest's default JUnit family (not xunit1)."""
    junit = checkout.parent / f"junit-{time.monotonic_ns()}.xml"
    argv = [*step.argv, f"--junitxml={junit}", f"--rootdir={checkout}"]
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    done = subprocess.run(argv, cwd=checkout, env=env, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, timeout=step.timeout)
    return StepRun(done.returncode, done.stdout, 1.0, junit.read_bytes())


@needs_ast_grep
def test_round_0_reads_pytests_default_junit_family(repo):
    """No `file` attribute in the default family: attribution by class name
    still finds an inherited module's tests, a skipped changed test and an
    empty changed module."""
    commit(repo, {"tests/__init__.py": "", "tests/test_base.py": """\
        class TestBase:
            def test_x(self):
                assert True
    """}, "a shared suite")
    base = _git(repo, "rev-parse", "HEAD")
    commit(repo, {
        "tests/test_b.py": "from tests.test_base import TestBase\n\n\n"
                           "class TestB(TestBase):\n    pass\n",
        "tests/test_skip.py": "import pytest\n\n\n@pytest.mark.skip(reason='off')\n"
                              "def test_off():\n    pass\n",
        "tests/test_empty.py": "HELPER = 1\n",
    }, "three modules")

    result = round0(repo, base, steps=(SUITE,), run=pytest_run_default_family)

    assert sorted(f["claim"] for f in result.findings) == [
        "tests/test_empty.py collected no test in the full suite's run, so nothing in it ran",
        "tests/test_skip.py::test_off was skipped in the full suite's run, so the change to "
        "it was not tested"]
