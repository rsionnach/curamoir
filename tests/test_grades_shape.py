"""A hand-written grades.json crashed both /curamoir:status and /curamoir:brief.

`_read_json` tolerates malformed JSON, but nothing checked the SHAPE of what
parsed: three call sites went straight to `grades["modules"].items()` and
`data.get("grade")`. Epic 7 has not landed, so hand-written is the only kind of
grades.json that exists today — and `/curamoir:status`'s whole contract is that
a section whose epic has not landed reports absence rather than erroring. Every
other malformed input here is already handled: torn ledger lines, a corrupt
lock, bad beads JSONL.
"""

import json

import pytest

from curamoir.brief import write_brief
from curamoir.scaffold import init_repo
from curamoir.status import collect_status, render_status

MALFORMED = [
    pytest.param({"modules": [{"src/a": "B"}]}, id="modules_is_a_list"),
    pytest.param(["src/a"], id="top_level_is_a_list"),
    pytest.param({"modules": {"src/a": "B"}}, id="grade_is_a_bare_string"),
    pytest.param("a string", id="top_level_is_a_string"),
    pytest.param({"modules": None}, id="modules_is_null"),
    pytest.param({"modules": {"src/a": None}}, id="entry_is_null"),
]


def initialised(tmp_path, grades):
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    init_repo(tmp_path)
    state = tmp_path / ".curamoir" / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "grades.json").write_text(json.dumps(grades), encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize("grades", MALFORMED)
def test_status_reports_absence_rather_than_crashing(tmp_path, grades):
    repo = initialised(tmp_path, grades)

    rendered = render_status(collect_status(repo))

    assert "grades:" in rendered


@pytest.mark.parametrize("grades", MALFORMED)
def test_the_brief_still_renders(tmp_path, grades):
    repo = initialised(tmp_path, grades)

    path = write_brief(repo)

    assert path is not None
    assert "cúramóir" in path.read_text(encoding="utf-8")


def test_a_well_formed_grades_file_is_still_summarised(tmp_path):
    """The guard must not swallow the real shape it exists to read."""
    repo = initialised(tmp_path, {"modules": {"src/a": {"grade": "B"}}})

    assert "src/a B" in render_status(collect_status(repo))
