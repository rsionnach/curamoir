"""The handoff must stay useful on the sessions it exists for.

A handoff earns its keep on the sessions that ended badly, so its degraded
paths are its main paths. Each case here produced a FILE — nothing raised,
nothing was logged — that had quietly stopped carrying the thing it is for.
"""

import os
import stat
import time

from curamoir.handoff import build_handoff, write_handoff
from curamoir.scaffold import init_repo


def initialised(tmp_path):
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    init_repo(tmp_path)
    return tmp_path


def git_repo(tmp_path):
    repo = initialised(tmp_path)
    import subprocess
    for command in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "t@example.com"],
        ["git", "config", "user.name", "T"],
        ["git", "commit", "-q", "--allow-empty", "-m", "first"],
    ):
        subprocess.run(command, cwd=repo, check=True, capture_output=True)
    return repo


def test_a_long_note_does_not_discard_the_rest_of_the_handoff(tmp_path):
    """`last_decision` is capped at 60 words; the developer's own note was
    capped at nothing, and `_cap_words` DROPS any line that does not fit. Past
    about 385 words everything went — branch, changes, the explicit next step,
    and the note itself — leaving a title and a truncation notice."""
    repo = git_repo(tmp_path)

    write_handoff(repo, note="word " * 600, next_step="Run the sweep on src/app.")

    rendered = (repo / ".curamoir" / "handoff" / "latest.md").read_text(encoding="utf-8")
    assert "**branch:**" in rendered, "the structural fields were dropped"
    assert "Intended next step" in rendered, "the most actionable field was dropped"


def test_a_detached_head_is_not_reported_as_a_branch_named_head(tmp_path):
    """Mid-rebase or mid-bisect is exactly the interrupted session a handoff is
    for, and `HEAD` reads as a branch name."""
    import subprocess

    repo = git_repo(tmp_path)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                         capture_output=True, text=True).stdout.strip()
    subprocess.run(["git", "checkout", "-q", "--detach", sha], cwd=repo,
                   check=True, capture_output=True)

    branch = build_handoff(repo)["branch"]

    assert branch != "HEAD"
    assert "detached" in branch.lower()


def test_write_handoff_returns_inside_the_session_end_budget(tmp_path, monkeypatch):
    """hooks.json gives SessionEnd 2s and notes that SessionEnd hooks SHARE a
    1.5s budget, while `build_handoff` made several git calls each allowed 5s.
    A cold network mount or a hung credential helper is slow, not down."""
    directory = tmp_path / "slowbin"
    directory.mkdir()
    shim = directory / "git"
    shim.write_text("#!/bin/sh\nsleep 4\nexit 0\n", encoding="utf-8")
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{directory}:{os.environ['PATH']}")
    repo = initialised(tmp_path)

    started = time.monotonic()
    write_handoff(repo, deadline_seconds=1.5)
    elapsed = time.monotonic() - started

    assert elapsed < 3.0, f"took {elapsed:.1f}s against a 1.5s declared budget"


def test_a_content_free_handoff_does_not_evict_the_last_informative_one(
    tmp_path, monkeypatch
):
    """With git unavailable every write renders the same empty shape, and
    archiving those pruned the last real handoff out of history within ten
    writes — a handful of sessions.

    The first version of this test passed for two wrong reasons, and the fix it
    guarded could be stubbed out entirely with the suite still green:

    1. its fixture never actually removed git from PATH, despite the docstring
       saying so, so every handoff was informative;
    2. it seeded the marker through `--note`, which `record_turn` journals — so
       `_last_decision` replayed the phrase into every LATER handoff, and the
       assertion found it whether or not the original survived.

    Both are fixed here: git is genuinely gone, and the informative handoff is
    seeded by writing the file, so the phrase exists in exactly one place.
    """
    repo = initialised(tmp_path)
    marker = "THE HANDOFF THAT MATTERS"
    latest = repo / ".curamoir" / "handoff" / "latest.md"
    latest.parent.mkdir(parents=True, exist_ok=True)
    latest.write_text(f"# cúramóir handoff\n\n> {marker}\n", encoding="utf-8")

    monkeypatch.setenv("PATH", "")
    for _ in range(12):
        write_handoff(repo, keep_history=10)

    history = repo / ".curamoir" / "handoff" / "history"
    archived = "\n".join(p.read_text(encoding="utf-8") for p in history.iterdir())

    assert marker in archived, (
        "the only informative handoff was pruned away by content-free ones"
    )


def test_the_content_free_gate_is_not_satisfied_by_every_handoff(tmp_path, monkeypatch):
    """The gate's first version tested the rendered markdown for `## Intended
    next step`, which `_next_step` always emits — so it returned True for the
    exact handoff it existed to reject. Asserted on the predicate directly, so
    that the gate cannot quietly become a tautology again."""
    from curamoir.handoff import build_handoff, is_informative

    repo = initialised(tmp_path)
    monkeypatch.setenv("PATH", "")

    assert not is_informative(build_handoff(repo)), (
        "a handoff with no note, no git state and no claimed bead is not informative"
    )


def test_a_handoff_with_any_real_content_is_informative(tmp_path):
    """The gate must not reject a handoff worth keeping."""
    from curamoir.handoff import build_handoff, is_informative

    repo = git_repo(tmp_path)

    assert is_informative(build_handoff(repo))
    assert is_informative(dict(build_handoff(repo), note="a deliberate note"))


def test_a_long_next_step_renders_its_body_not_a_bare_heading(tmp_path):
    """`_cap_words`' skip keeps the short structural fields — and could leave
    `## Intended next step` with nothing under it, which promises the most
    actionable field in the file and delivers nothing."""
    repo = git_repo(tmp_path)

    write_handoff(repo, note="short note", next_step="word " * 600)

    rendered = (repo / ".curamoir" / "handoff" / "latest.md").read_text(encoding="utf-8")
    # Unconditional. Wrapping this in `if "## Intended next step" in rendered`
    # let the bug switch the assertion off: drop the bound and
    # `_without_empty_headings` deletes the heading, so the `if` is false and
    # the test passes vacuously.
    assert "## Intended next step" in rendered
    after = rendered.split("## Intended next step", 1)[1].lstrip("\n")
    body = [line for line in after.splitlines() if line.strip()]
    assert body and not body[0].startswith("##"), "the heading has no body"
    assert not body[0].startswith("_Truncated"), "the heading's body is the notice"


def test_a_handoff_written_in_the_future_is_not_reported_as_negative_age(tmp_path):
    """Clock skew, a restored backup, a synced mount. `brief` clamps; `status`
    printed "-3 day(s) old"."""
    from curamoir.status import collect_status

    repo = initialised(tmp_path)
    write_handoff(repo)
    latest = repo / ".curamoir" / "handoff" / "latest.md"
    future = time.time() + 3 * 86_400
    os.utime(latest, (future, future))

    age = collect_status(repo)["handoff"]["age_days"]

    assert age is None or age >= 0, f"reported an age of {age}"


# --- retention order, and the paths the marker has to cover ---------------

def test_pruning_keeps_the_newest_handoffs_not_the_oldest(tmp_path, monkeypatch):
    """`_prune` sorts by filename, and the unsuffixed first entry of a second
    sorts AFTER its own suffixed successors, because `'-' < '.'`. Zero-padding
    the suffixes fixed `-10` before `-2` and left this inverse, so a
    same-second burst kept the OLDEST entries and discarded the newest.
    """
    repo = initialised(tmp_path)
    monkeypatch.setenv("PATH", "")
    for index in range(12):
        write_handoff(repo, note=f"SESSION {index:02d}", keep_history=3)

    history = repo / ".curamoir" / "handoff" / "history"
    # Read the NOTE line only. A note is journalled, so `_last_decision`
    # replays an earlier one into later handoffs, and counting every mention
    # would conflate "this handoff" with "the one before it".
    sessions = sorted(
        int(line.split("SESSION ")[1][:2])
        for path in history.iterdir()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith("> SESSION ")
    )

    assert len(sessions) == 3, sessions
    assert min(sessions) >= 8, f"kept the oldest handoffs, not the newest: {sessions}"


def test_the_archive_name_orders_chronologically_as_text():
    """`_prune` compares names, so the names have to sort the way time does."""
    from curamoir.handoff import _archive_name

    names = [_archive_name("2026-10-09T08-50-18", index) for index in range(3)]

    assert names == sorted(names), names


def test_a_handoff_that_could_not_be_built_is_marked_content_free(tmp_path, monkeypatch):
    """`write_handoff`'s except branch builds its string directly and never
    calls `render_handoff`, so the one path that exists for things going wrong
    produced an unmarked — therefore "informative" — handoff, and the eviction
    came back on it."""
    import curamoir.handoff as handoff_module
    from curamoir.handoff import _CONTENT_FREE_MARKER

    repo = initialised(tmp_path)
    latest = repo / ".curamoir" / "handoff" / "latest.md"
    latest.parent.mkdir(parents=True, exist_ok=True)
    latest.write_text("# cúramóir handoff\n\n> THE ONE THAT MATTERS\n", encoding="utf-8")

    def explode(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(handoff_module, "build_handoff", explode)
    for _ in range(12):
        write_handoff(repo, keep_history=10)

    assert _CONTENT_FREE_MARKER in latest.read_text(encoding="utf-8")
    archived = "\n".join(
        p.read_text(encoding="utf-8")
        for p in (repo / ".curamoir" / "handoff" / "history").iterdir()
    )
    assert "THE ONE THAT MATTERS" in archived


def test_the_marker_is_emitted_only_for_a_content_free_handoff(tmp_path, monkeypatch):
    """Both directions, because the marker IS the on-disk half of the gate and
    nothing else asserts on it."""
    from curamoir.handoff import _CONTENT_FREE_MARKER, build_handoff, render_handoff

    repo = git_repo(tmp_path)
    informative = render_handoff(dict(build_handoff(repo), note="a real note"))
    assert _CONTENT_FREE_MARKER not in informative

    second = tmp_path / "b"
    second.mkdir()
    monkeypatch.setenv("PATH", "")
    assert _CONTENT_FREE_MARKER in render_handoff(build_handoff(initialised(second)))


def test_a_marked_file_is_not_archived_and_an_unmarked_one_is(tmp_path):
    """`_file_is_informative` is what reads the marker next session."""
    from curamoir.handoff import _CONTENT_FREE_MARKER, _file_is_informative

    marked = tmp_path / "marked.md"
    marked.write_text(f"# handoff\n\n{_CONTENT_FREE_MARKER}\n", encoding="utf-8")
    legacy = tmp_path / "legacy.md"
    legacy.write_text("# handoff\n\n- **branch:** main\n", encoding="utf-8")

    assert not _file_is_informative(marked)
    assert _file_is_informative(legacy), "a handoff written before the marker existed"


def test_a_heading_whose_body_does_not_fit_is_dropped_entirely():
    """`_without_empty_headings` tested directly, because it is no longer
    reachable end to end — and that is the honest reason rather than a
    convenience.

    Both section bodies are bounded now (the note and the next step at render,
    the last decision at 60 words by `_first_sentences`), so no input makes
    `_cap_words` skip a body while keeping its heading. The helper is the net
    that catches the next unbounded field someone adds, so it is pinned here as
    a unit: an end-to-end test for it would have to be written against a bug
    that no longer exists, and stubbing the helper to identity left every other
    test in this file green.
    """
    from curamoir.handoff import _without_empty_headings

    kept = _without_empty_headings([
        "# cúramóir handoff",
        "",
        "- **branch:** main",
        "",
        "## Intended next step",      # body skipped by _cap_words
        "",
        "## Last decision",
        "",
        "Chose the thread-based timeout.",
    ])

    assert "## Intended next step" not in kept, "a heading with no body survived"
    assert "## Last decision" in kept, "a heading WITH a body was dropped"
    assert kept[-1] == "Chose the thread-based timeout."


def test_a_heading_with_a_body_is_always_kept():
    """The net must not eat real sections."""
    from curamoir.handoff import _without_empty_headings

    lines = ["## Intended next step", "", "Run the sweep.", "", "## Last decision", "", "x"]

    assert _without_empty_headings(lines) == lines
