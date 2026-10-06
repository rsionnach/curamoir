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


def test_every_planted_item_names_the_criterion_that_needs_it(manifest):
    """So a later reader can tell why a plant exists before changing it."""
    for group in ("violations", "bugs"):
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
    byte-identical output across runs and platforms"."""
    grades_input = FIXTURE / "expected" / "grades-input.json"

    assert grades_input.is_file()
    payload = json.loads(grades_input.read_text(encoding="utf-8"))
    assert payload["modules"], "grades input names no modules"


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
