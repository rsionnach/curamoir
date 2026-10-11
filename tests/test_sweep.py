"""E3-R1, first clause (cm-fkd.1): the sweep runs `curamoir-lint` first and
treats its output as the work list.

What is tested is the step between "the lint ran" and "an agent edits code":
the sweep acts only on a report it can trust (exit 0 or 1, `complete`, a
schema it reads), and the list it hands on is exactly the lint's violations,
in an order that does not depend on how the lint happened to sort them.

**Fixture provenance.** The planted violations are `fixtures/sample-repo`'s,
from PLANTED.json `violations` (5 across 3 principles: E3's acceptance
fixture). The fixture ships no rules of its own, so the rules are installed
into a copy: the regex test rules from tests/test_lint.py, which that file
already pins to exactly those 5 records. They are not added to the fixture
itself because tests/test_lint.py asserts exact violation sets over copies of
it with its own rules installed, and tests/test_bundled_rules.py swaps in the
starter principles, under which fixture-local rules citing the fixture's
principles would no longer load.
"""

import json
import os
import subprocess
import sys

import pytest

from curamoir import lint
from curamoir.sweep import NO_WORK, WORK, LintUntrusted, work_list
from tests.test_lint import (
    ALL_REGEX_RULES,
    AST_GREP_PRINT,
    NO_UNCHAINED_RAISE,
    NO_VARIANT_MODULE,
    PLANTED,
    ROOT,
    needs_ast_grep,
    sample_repo,
)
from tests.test_lint_report import DOCUMENT, RECORD

PLANTED_VIOLATIONS = sorted(
    (v["path"], v["line"], v["principle_id"]) for v in PLANTED["violations"]
)


def fake_lint(tmp_path, stdout="", stderr="", exit_code=0, sleep=0):
    """An executable standing in for `curamoir-lint`, for the exits and
    reports the real runner cannot be made to produce on demand (a report
    that contradicts its own exit code, say)."""
    script = tmp_path / "bin" / "fake-curamoir-lint"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        f"#!{sys.executable}\n"
        "import sys, time\n"
        f"time.sleep({sleep})\n"
        f"sys.stdout.write({stdout!r})\n"
        f"sys.stderr.write({stderr!r})\n"
        f"sys.exit({exit_code})\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def report(**changes):
    return json.dumps(dict(DOCUMENT, **changes))


# --- acceptance: the work list is the 5 planted violations --------------------

def test_the_work_list_is_exactly_the_planted_violations(tmp_path):
    """E3's acceptance fixture, through the real `scripts/curamoir-lint`:
    5 violations across 3 principles, as a set, so a missed violation and an
    invented one both fail."""
    repo = sample_repo(tmp_path, *ALL_REGEX_RULES)

    result = work_list(repo)

    assert result.outcome == WORK
    assert sorted((i.path, i.line, i.principle_id) for i in result.items) == PLANTED_VIOLATIONS
    assert result.principle_ids == (
        "errors-carry-context", "no-file-variant-proliferation", "structured-logging",
    )


def test_every_severity_enters_the_work_list(tmp_path):
    """The lint's threshold decides its exit code, not what it reports, and
    the spec makes its output — not its failing part — the work list. The
    errors-carry-context rule is a `warning`, below the default `fail_on:
    error`; filtering at the threshold would drop 2 of the 5."""
    repo = sample_repo(tmp_path, *ALL_REGEX_RULES)

    result = work_list(repo)

    assert {i.severity for i in result.items} == {"error", "warning"}
    assert len(result.items) == 5


def test_the_work_list_is_ordered_by_principle_then_location(tmp_path):
    """The later PR body and fix counts are grouped by principle, so the
    order is principle first; the lint's own order (by path) would
    interleave them."""
    repo = sample_repo(tmp_path, *ALL_REGEX_RULES)

    result = work_list(repo)

    keys = [(i.principle_id, i.path, i.line, i.col) for i in result.items]
    assert keys == sorted(keys)
    grouped = result.by_principle()
    assert list(grouped) == list(result.principle_ids)
    assert [len(items) for items in grouped.values()] == [2, 1, 2]
    assert result.paths == (
        "src/sample/core/billing.py", "src/sample/web/handlers.py",
        "src/sample/web/handlers_v2.py",
    )


def test_two_runs_over_the_same_tree_give_the_same_work_list(tmp_path):
    repo = sample_repo(tmp_path, *ALL_REGEX_RULES)

    assert work_list(repo).items == work_list(repo).items


@needs_ast_grep
def test_the_work_list_through_the_real_ast_grep(tmp_path):
    """The structured-logging plants found by an ast-grep rule, run by the real
    binary through the wrapper the sweep calls."""
    repo = sample_repo(tmp_path, AST_GREP_PRINT, NO_UNCHAINED_RAISE, NO_VARIANT_MODULE)

    result = work_list(repo)

    assert sorted((i.path, i.line, i.principle_id) for i in result.items) == PLANTED_VIOLATIONS
    assert result.report.tools["ast-grep"]


# --- nothing to do -----------------------------------------------------------

def test_no_rules_is_no_work(tmp_path):
    """A repo with no rules exits 0 with a `no_rules` note; the sweep has
    nothing from the lint to act on. The note is kept, so whoever records the
    `no_work` can say why."""
    repo = sample_repo(tmp_path)

    result = work_list(repo)

    assert result.outcome == NO_WORK
    assert result.items == ()
    assert [n.kind for n in result.report.notes] == ["no_rules"]


def test_rules_that_find_nothing_is_no_work(tmp_path):
    clean_rule = NO_VARIANT_MODULE.replace("_(v[0-9]+|new|old)", "_(v99)")
    repo = sample_repo(tmp_path, clean_rule)

    result = work_list(repo)

    assert result.outcome == NO_WORK
    assert result.report.rules_checked == ("no-variant-module",)


# --- an untrustworthy work list is never acted on ------------------------------

def test_exit_2_from_the_real_runner_stops_the_sweep(tmp_path):
    """A config that does not validate is a 2: the gate's verdict is unknown,
    so the sweep must not guess at it."""
    repo = sample_repo(tmp_path, *ALL_REGEX_RULES, config="rules:\n  fail_on: fatal\n")

    with pytest.raises(LintUntrusted) as raised:
        work_list(repo)

    assert raised.value.exit_code == 2
    assert "fail_on" in str(raised.value)


def test_exit_2_with_a_partial_report_stops_the_sweep_and_names_the_skipped_rules(tmp_path):
    """A partial report's findings are real, but what the skipped rule did not
    search has not been shown clean; the sweep stops rather than fixing half."""
    skipped = [{"rule_id": "no-print", "reason": "ast-grep is not installed"}]
    lint_ = fake_lint(tmp_path, stdout=report(complete=False, rules_skipped=skipped),
                      stderr="curamoir-lint: no-print skipped\n", exit_code=2)

    with pytest.raises(LintUntrusted, match="no-print"):
        work_list(tmp_path, lint=lint_)


def test_an_incomplete_report_stops_the_sweep_whatever_the_exit_code(tmp_path):
    skipped = [{"rule_id": "no-print", "reason": "ast-grep is not installed"}]
    lint_ = fake_lint(tmp_path, stdout=report(complete=False, rules_skipped=skipped),
                      exit_code=1)

    with pytest.raises(LintUntrusted, match="incomplete"):
        work_list(tmp_path, lint=lint_)


@pytest.mark.parametrize("code", [3, 127, -9])
def test_an_exit_code_outside_the_contract_stops_the_sweep(tmp_path, code):
    lint_ = fake_lint(tmp_path, stdout=report(), exit_code=code % 256)

    with pytest.raises(LintUntrusted):
        work_list(tmp_path, lint=lint_)


def test_a_report_that_the_reader_refuses_stops_the_sweep(tmp_path):
    lint_ = fake_lint(tmp_path, stdout=report(schema_version=2), exit_code=1)

    with pytest.raises(LintUntrusted, match="schema_version"):
        work_list(tmp_path, lint=lint_)


def test_output_that_is_not_json_stops_the_sweep(tmp_path):
    lint_ = fake_lint(tmp_path, stdout="src/a.py:1:1: error ...\n", exit_code=1)

    with pytest.raises(LintUntrusted):
        work_list(tmp_path, lint=lint_)


@pytest.mark.parametrize("exit_code, violations", [(0, [RECORD]), (1, [])])
def test_an_exit_code_that_contradicts_the_report_stops_the_sweep(tmp_path, exit_code,
                                                                   violations):
    """Exit 1 means a violation at or above `fail_on`, exit 0 means none. A
    report that disagrees with its own exit code was not produced by the
    runner the sweep thinks it ran."""
    lint_ = fake_lint(tmp_path, stdout=report(violations=violations), exit_code=exit_code)

    with pytest.raises(LintUntrusted, match="exit"):
        work_list(tmp_path, lint=lint_)


def test_a_runner_that_cannot_be_started_stops_the_sweep(tmp_path):
    with pytest.raises(LintUntrusted):
        work_list(tmp_path, lint=tmp_path / "no-such-lint")


def test_a_runner_that_hangs_stops_the_sweep(tmp_path):
    """`--timeout` bounds the runner itself; this is the backstop for a
    runner that does not honour it."""
    lint_ = fake_lint(tmp_path, stdout=report(), sleep=30)

    with pytest.raises(LintUntrusted, match="did not finish"):
        work_list(tmp_path, lint=lint_, timeout=1, grace=1)


# --- the deterministic helper the skill calls ---------------------------------

def run_cli(*args):
    environment = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    return subprocess.run(
        [sys.executable, "-m", "curamoir.cli", *args],
        capture_output=True, text=True, env=environment, timeout=120,
    )


def test_the_cli_prints_the_work_list_grouped_by_principle(tmp_path):
    repo = sample_repo(tmp_path, *ALL_REGEX_RULES)

    result = run_cli("sweep-work-list", "--repo", str(repo))

    assert result.returncode == 0, result.stderr
    printed = json.loads(result.stdout)
    assert printed["outcome"] == WORK
    assert printed["count"] == 5
    assert [p["principle_id"] for p in printed["principles"]] == [
        "errors-carry-context", "no-file-variant-proliferation", "structured-logging",
    ]
    found = sorted(
        (v["path"], v["line"], p["principle_id"])
        for p in printed["principles"] for v in p["violations"]
    )
    assert found == PLANTED_VIOLATIONS
    assert all(p["principle_rule"] and p["why_link"] for p in printed["principles"])


def test_the_cli_says_no_work(tmp_path):
    result = run_cli("sweep-work-list", "--repo", str(sample_repo(tmp_path)))

    assert result.returncode == 0, result.stderr
    printed = json.loads(result.stdout)
    assert printed["outcome"] == NO_WORK
    assert printed["principles"] == []
    assert printed["notes"] == ["no_rules"]


def test_the_cli_exits_2_with_nothing_on_stdout_when_the_lint_cannot_be_trusted(tmp_path):
    """Nothing on stdout, so an agent cannot mistake a partial list for the
    work list."""
    repo = sample_repo(tmp_path, *ALL_REGEX_RULES, config="rules:\n  fail_on: fatal\n")

    result = run_cli("sweep-work-list", "--repo", str(repo))

    assert result.returncode == lint.EXIT_ERROR
    assert result.stdout == ""
    assert "do not edit anything" in result.stderr


# --- the skill ---------------------------------------------------------------

SKILL = ROOT / "skills" / "sweep" / "SKILL.md"


def test_the_skill_calls_the_helper_and_states_the_scope_limits():
    """The skill is prompt text, so what it must say is pinned: the command it
    runs exists (tested above), the E3-R2 limits are stated (the spec says the
    prompt MUST state them), and it cannot fire on its own."""
    text = SKILL.read_text(encoding="utf-8")
    frontmatter = text.split("---", 2)[1]

    assert "name: sweep" in frontmatter
    assert "disable-model-invocation: true" in frontmatter
    assert "scripts/curamoir sweep-work-list" in text
    assert ("only files with violations; no refactors beyond the violation; no new "
            "dependencies; no signature changes without an explicit principle "
            "permitting it.") in " ".join(text.split())
    assert NO_WORK in text
