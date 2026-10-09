"""`cadence.json` got no tolerant reader, and `grades.json` one line above it did.

`/curamoir:status`'s stated contract — in `grades.py`'s own docstring — is that
a section whose producing epic has not landed reports ABSENCE rather than
erroring. Epic 10 writes `cadence.json` and has not landed, so every one in
existence is hand-written, which is exactly the position `grades.json` was in
when it got `graded_modules`. Cadence was left indexing straight into
`cadence.items()`, so a plausible hand-written shape reached the slash command
as an AttributeError traceback — the one thing `cli.main` commits to never
showing.

And the flat `{job: when}` assumption was wrong anyway: the spec describes this
file as "per-module review interval and last-run" (§6.1 line 110, E5-R4), so
the shape E5 will write renders a Python repr on the happy path.
"""

import json

import pytest

from curamoir.scaffold import init_repo
from curamoir.status import collect_status, render_status

MALFORMED = [
    pytest.param(["sweep", "review"], id="top_level_is_a_list"),
    pytest.param("sweep", id="top_level_is_a_string"),
    pytest.param(42, id="top_level_is_a_number"),
    pytest.param({"sweep": None}, id="entry_is_null"),
    pytest.param({"sweep": ["a", "b"]}, id="entry_is_a_list"),
]


def initialised(tmp_path, cadence):
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    init_repo(tmp_path)
    state = tmp_path / ".curamoir" / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "cadence.json").write_text(json.dumps(cadence), encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize("cadence", MALFORMED)
def test_status_reports_absence_rather_than_crashing(tmp_path, cadence):
    rendered = render_status(collect_status(initialised(tmp_path, cadence)))

    assert "next jobs:" in rendered


def test_the_per_module_shape_the_spec_describes_renders_readably(tmp_path):
    """§6.1 and E5-R4: per-module interval and last-run. The flat assumption
    rendered `sweep: {'next': '2026-10-12', 'last': '2026-10-05'}` — a Python
    repr in the output of the command whose whole job is a fast way back in."""
    repo = initialised(tmp_path, {
        "src/app": {"interval": "weekly", "last_run": "2026-10-05"},
        "src/web": {"interval": "monthly", "last_run": None},
    })

    rendered = render_status(collect_status(repo))

    assert "{" not in rendered, f"a Python repr reached the output:\n{rendered}"
    assert "src/app" in rendered and "weekly" in rendered
    assert "src/web" in rendered


def test_a_flat_schedule_still_renders(tmp_path):
    """The shape `/curamoir:status` assumed before this. Keeping it readable
    costs nothing and a hand-written file may well use it."""
    rendered = render_status(collect_status(initialised(tmp_path, {"sweep": "weekly"})))

    assert "sweep" in rendered and "weekly" in rendered


def test_an_absent_cadence_file_says_so(tmp_path):
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    init_repo(tmp_path)

    assert "next jobs:" in render_status(collect_status(tmp_path))
