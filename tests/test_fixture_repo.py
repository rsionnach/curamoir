"""E1-FIX: the shared fixture repo, and the manifest that keeps it honest.

§23 asks for "a small Python repo with planted violations, bugs, a broken-able
quickstart, and a few beads, reused by every epic's acceptance tests".

The manifest is the load-bearing part. Without it the fixture drifts: someone
reformats a file, every planted line number moves, and E5's "line ranges within
±5 lines of the plants" criterion starts failing for reasons that have nothing to
do with the reviewer. So each planted item records its path, its line, what it
is, and which epic's criterion needs it — and these tests assert the manifest
still describes the files.

The fixture lives outside tests/ so pytest never collects the deliberately
failing tests it contains.
"""

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "sample-repo"
MANIFEST = FIXTURE / "PLANTED.json"


@pytest.fixture(scope="module")
def manifest():
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def test_the_fixture_exists_outside_the_tests_tree():
    """Inside tests/, pytest would collect the fixture's own failing tests."""
    assert FIXTURE.is_dir()
    assert "tests" not in FIXTURE.relative_to(ROOT).parts[:1]


def test_the_fixture_is_a_plausible_python_repo():
    assert (FIXTURE / "pyproject.toml").is_file()
    assert (FIXTURE / "src" / "sample").is_dir()


def test_detection_finds_at_least_three_modules_so_the_selector_is_exercised():
    """E5-R3 selects modules_per_run=2 plus random_extra=1. With fewer than three
    the selector round-robins, and the fixture would not exercise it."""
    from curamoir.scaffold import detect_modules

    modules = detect_modules(FIXTURE)

    assert len(modules) >= 3, modules


# --- planted lint violations (E2, E3) -------------------------------------

def test_five_violations_across_three_principles_are_planted(manifest):
    """E3's acceptance criterion is exact: "5 planted violations across 3
    principles"."""
    violations = manifest["violations"]

    assert len(violations) == 5
    assert len({v["principle_id"] for v in violations}) == 3


def test_every_planted_violation_is_actually_present_at_its_recorded_line(manifest):
    """The anti-drift check. If this fails, the fixture moved and every epic
    that trusts these line numbers is about to fail for the wrong reason."""
    for violation in manifest["violations"]:
        path = FIXTURE / violation["path"]
        assert path.is_file(), violation
        lines = path.read_text(encoding="utf-8").splitlines()
        line_number = violation["line"]
        assert 1 <= line_number <= len(lines), violation
        assert violation["evidence"] in lines[line_number - 1], (
            f"{violation['path']}:{line_number} no longer contains "
            f"{violation['evidence']!r} — the fixture has drifted"
        )


def test_every_bundled_rule_finding_is_present_at_its_recorded_line(manifest):
    """E2-R6's plants: what the bundled example rules in `examples/` must find.
    The same anti-drift check as for `violations`."""
    findings = manifest["bundled_rule_findings"]

    assert findings
    for finding in findings:
        path = FIXTURE / finding["path"]
        assert path.is_file(), finding
        lines = path.read_text(encoding="utf-8").splitlines()
        assert 1 <= finding["line"] <= len(lines), finding
        assert finding["evidence"] in lines[finding["line"] - 1], (
            f"{finding['path']}:{finding['line']} no longer contains "
            f"{finding['evidence']!r} — the fixture has drifted"
        )


def test_bundled_rule_findings_cite_starter_principles(manifest):
    """They are checked against `templates/principles.md`, not the fixture's
    three-principle subset: the examples are written for a repo `/curamoir:init`
    seeded, and two of them cite principles E3's subset leaves out."""
    from curamoir.principles import parse_principles

    starter = parse_principles(
        (ROOT / "templates" / "principles.md").read_text(encoding="utf-8")
    )
    defined = {p.id for p in starter.principles}

    for finding in manifest["bundled_rule_findings"]:
        assert finding["principle_id"] in defined, finding


def test_every_planted_item_names_the_criterion_that_needs_it(manifest):
    """So a later reader can tell why a plant exists before changing it."""
    for group in ("violations", "bugs", "bundled_rule_findings"):
        for item in manifest[group]:
            assert item["needed_by"], item


# --- planted bugs (E5) ----------------------------------------------------

def test_two_correctness_bugs_and_one_edge_case_gap_are_planted(manifest):
    """E5's criterion: "two planted correctness bugs and one edge-case gap"."""
    bugs = manifest["bugs"]

    assert len([b for b in bugs if b["lens"] == "correctness"]) == 2
    assert len([b for b in bugs if b["lens"] == "edge_cases"]) == 1


def test_every_planted_bug_is_present_at_its_recorded_line(manifest):
    for bug in manifest["bugs"]:
        path = FIXTURE / bug["path"]
        lines = path.read_text(encoding="utf-8").splitlines()
        assert bug["evidence"] in lines[bug["line"] - 1], (
            f"{bug['path']}:{bug['line']} no longer contains {bug['evidence']!r}"
        )


# --- the breakable quickstart (E12) ---------------------------------------

def test_the_quickstart_exists_and_is_executable():
    quickstart = FIXTURE / "scripts" / "quickstart.sh"

    assert quickstart.is_file()
    import os
    assert os.access(quickstart, os.X_OK)


def test_the_quickstart_passes_as_shipped():
    """E12's criterion needs it green first, so that breaking it is a change."""
    import subprocess

    result = subprocess.run(
        ["bash", "scripts/quickstart.sh"], cwd=FIXTURE,
        capture_output=True, text=True, timeout=60,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_breaking_the_quickstart_is_a_single_documented_edit(manifest):
    how = manifest["quickstart"]

    assert how["break_by"], "no documented way to break the quickstart"
    target = FIXTURE / how["path"]
    assert how["evidence"] in target.read_text(encoding="utf-8")


# --- grades input (E7) ----------------------------------------------------

def test_a_fixed_grades_input_is_shipped_for_the_determinism_test():
    """E7's criterion: "given a fixed grades input fixture, the script produces
    byte-identical output across runs and platforms".

    It lives under `input/`, not `expected/`. Filed as "expected" it invited the
    next reader — reasonably — to treat it as an expected OUTPUT and "correct" it
    to match whatever `curamoir-grade` emits, which would make E7-R1's
    determinism test self-confirming.

    The assertion was `assert payload["modules"]`, bare truthiness that passes
    for `{"modules": {"x": {}}}`. It now checks the five factors E7-R1 names.
    """
    grades_input = FIXTURE / "input" / "grades-input.json"
    assert grades_input.is_file()
    assert not (FIXTURE / "expected").exists(), "an input fixture is back under expected/"

    payload = json.loads(grades_input.read_text(encoding="utf-8"))
    modules = payload["modules"]
    assert modules, "grades input names no modules"

    # docs/curamoir-spec.md E7-R1: "open findings weighted by severity, open lint
    # violations, sweep-fix density over the window, days since last review, and
    # (if available) test pass state for the module".
    required = ("open_findings", "open_violations", "sweep_fixes_in_window",
                "days_since_review", "tests_passing")
    for name, factors in modules.items():
        for factor in required:
            assert factor in factors, f"{name}: missing E7-R1 factor {factor!r}"
        for finding in factors["open_findings"]:
            assert finding["severity"] in ("P0", "P1", "P2", "P3"), finding


def test_the_grades_input_names_the_modules_detection_actually_finds():
    """A grades fixture naming modules that do not exist would let E7-R1 be
    written against a shape the detector never produces."""
    from curamoir.scaffold import detect_modules

    payload = json.loads((FIXTURE / "input" / "grades-input.json").read_text(encoding="utf-8"))
    detected = set(detect_modules(FIXTURE))

    assert set(payload["modules"]) == detected, (
        f"grades input names {sorted(payload['modules'])}, "
        f"detection finds {sorted(detected)}"
    )


# --- beads (E8, E9) -------------------------------------------------------

def test_beads_are_shipped_with_both_ready_and_in_progress_work(manifest):
    """E8-R1's handoff must name an in-progress bead; E9-R2 section 6 must pick a
    ready one estimated at 90 minutes or less."""
    beads = json.loads((FIXTURE / ".beads" / "issues.jsonl").read_text(encoding="utf-8").splitlines()[0])
    all_beads = [
        json.loads(line)
        for line in (FIXTURE / ".beads" / "issues.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert beads
    statuses = {b["status"] for b in all_beads}
    assert "in_progress" in statuses
    assert "open" in statuses
    assert any(b.get("estimate") and b["estimate"] <= 90 for b in all_beads), (
        "no ready bead under 90 minutes, so the brief has nothing to suggest"
    )


# --- the test command (E3-R4) --------------------------------------------

def test_the_fixture_declares_a_test_command_that_passes(manifest):
    """E3-R4 runs the project's test command before opening a PR, so the fixture
    needs one that is green on arrival."""
    import subprocess

    command = manifest["test_command"]
    assert command

    result = subprocess.run(
        command, cwd=FIXTURE, shell=True, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# --- every planted violation must name a DEFINED principle ----------------

def test_every_planted_principle_id_resolves_to_a_defined_principle(manifest):
    """Until the fixture defined these, the ids resolved to nothing: the
    manifest asserted a reading of three rules that were named in the spec's
    example list but never defined anywhere.

    The consequence was directional. When E2-R2 authors the real starter set,
    the pressure would run the wrong way — the rule written to match the
    fixture rather than the fixture checked against the rule — and E3's
    acceptance criterion would then be self-confirming."""
    principles = (FIXTURE / ".curamoir" / "principles.md").read_text(encoding="utf-8")
    defined = {
        line.split("`")[1]
        for line in principles.splitlines()
        if line.strip().startswith("- **id:**") and "`" in line
    }

    assert defined, "the fixture defines no principles at all"
    for violation in manifest["violations"]:
        assert violation["principle_id"] in defined, (
            f"{violation['principle_id']!r} is tagged on a planted violation but "
            f"is not defined. Defined: {sorted(defined)}"
        )


def test_each_defined_principle_carries_every_field_E2_R1_requires(manifest):
    """E2-R1 fixes the structure: id, title, rule, why, good, bad, tier,
    promoted_on. A fixture principle missing a field would let E2-R1's parser
    be written against an incomplete shape."""
    text = (FIXTURE / ".curamoir" / "principles.md").read_text(encoding="utf-8")
    sections = text.split("\n## ")[1:]

    assert len(sections) == 3, f"expected three principles, found {len(sections)}"
    for section in sections:
        name = section.splitlines()[0]
        for field in ("id", "title", "rule", "why", "good", "bad", "tier", "promoted_on"):
            assert f"**{field}:**" in section, f"{name}: missing {field!r}"


def test_each_principle_cites_where_its_concept_came_from(manifest):
    """So a later reader can tell a spec-derived concept from an invented one."""
    text = (FIXTURE / ".curamoir" / "principles.md").read_text(encoding="utf-8")

    for section in text.split("\n## ")[1:]:
        assert "**source:**" in section, section.splitlines()[0]
        assert "curamoir-spec.md" in section, section.splitlines()[0]
